package mysql

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"xiangkou/go-service/internal/domain"
)

func (r *ShopRepo) GetVoucherBrief(ctx context.Context, id int64) (*domain.VoucherBrief, error) {
	const query = `SELECT id, shop_id, title, COALESCE(rules,''), pay_value, actual_value, type, COALESCE(status,0)
	               FROM tb_voucher WHERE id = ?`
	var item domain.VoucherBrief
	err := r.db.QueryRowContext(ctx, query, id).Scan(&item.ID, &item.ShopID, &item.Title, &item.Rules,
		&item.PayValue, &item.ActualValue, &item.Type, &item.Status)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("get voucher: %w", err)
	}
	return &item, nil
}

// GetSeckill 读秒杀券库存与可购时段；ok=false 表示没有秒杀记录。
func (r *ShopRepo) GetSeckill(ctx context.Context, id int64) (stock int, begin, end time.Time, ok bool, err error) {
	err = r.db.QueryRowContext(ctx,
		"SELECT stock, begin_time, end_time FROM tb_seckill_voucher WHERE voucher_id = ?", id).
		Scan(&stock, &begin, &end)
	if err == sql.ErrNoRows {
		return 0, time.Time{}, time.Time{}, false, nil
	}
	if err != nil {
		return 0, time.Time{}, time.Time{}, false, err
	}
	return stock, begin, end, true, nil
}

// CreateSeckillOrder 写秒杀订单；重复单返回 error。
// 库存权威在 Redis（Lua 同步扣减），DB stock 由对账任务从 Redis 同步，这里只写订单不碰库存。
func (r *ShopRepo) CreateSeckillOrder(ctx context.Context, o domain.Order) error {
	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	var existing int
	if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM tb_voucher_order WHERE user_id = ? AND voucher_id = ?",
		o.UserID, o.VoucherID).Scan(&existing); err != nil {
		return err
	}
	if existing > 0 {
		return fmt.Errorf("duplicate order user=%d voucher=%d", o.UserID, o.VoucherID)
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO tb_voucher_order (id, user_id, voucher_id, pay_type, status, create_time, update_time)
		 VALUES (?, ?, ?, 1, ?, NOW(), NOW())`, o.ID, o.UserID, o.VoucherID, o.Status); err != nil {
		return asDuplicate(err)
	}
	return tx.Commit()
}

func (r *ShopRepo) InsertOrder(ctx context.Context, o domain.Order) error {
	_, err := r.db.ExecContext(ctx,
		`INSERT INTO tb_voucher_order (id, user_id, voucher_id, pay_type, status, create_time, update_time)
		 VALUES (?, ?, ?, 1, ?, NOW(), NOW())`, o.ID, o.UserID, o.VoucherID, o.Status)
	return asDuplicate(err)
}

func scanOrderRow(row interface{ Scan(...any) error }) (domain.Order, error) {
	var o domain.Order
	var payTime, useTime, refundTime sql.NullTime
	err := row.Scan(&o.ID, &o.UserID, &o.VoucherID, &o.PayType, &o.Status, &o.CreateTime,
		&payTime, &useTime, &refundTime, &o.UpdateTime)
	if payTime.Valid {
		o.PayTime = &payTime.Time
	}
	if useTime.Valid {
		o.UseTime = &useTime.Time
	}
	if refundTime.Valid {
		o.RefundTime = &refundTime.Time
	}
	return o, err
}

const orderColumns = `id, user_id, voucher_id, COALESCE(pay_type,1), status, create_time,
                      pay_time, use_time, refund_time, update_time`

func (r *ShopRepo) GetOrder(ctx context.Context, id int64) (*domain.Order, error) {
	o, err := scanOrderRow(r.db.QueryRowContext(ctx, "SELECT "+orderColumns+" FROM tb_voucher_order WHERE id = ?", id))
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("get order: %w", err)
	}
	return &o, nil
}

func (r *ShopRepo) ListOrders(ctx context.Context, userID int64, page int, heldOnly bool) ([]domain.Order, int64, error) {
	const size = 20
	where := "user_id = ?"
	args := []any{userID}
	if heldOnly {
		where += " AND status = 2"
	}
	var total int64
	if err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM tb_voucher_order WHERE "+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	rows, err := r.db.QueryContext(ctx,
		"SELECT "+orderColumns+" FROM tb_voucher_order WHERE "+where+" ORDER BY create_time DESC, id DESC LIMIT ? OFFSET ?",
		append(args, size, (page-1)*size)...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	orders := make([]domain.Order, 0, size)
	for rows.Next() {
		o, err := scanOrderRow(rows)
		if err != nil {
			return nil, 0, err
		}
		orders = append(orders, o)
	}
	return orders, total, rows.Err()
}

// LatestOrder 取用户某张券最近一单；excludeCancelled 对应 既有服务 的 status <> 4。
func (r *ShopRepo) LatestOrder(ctx context.Context, userID, voucherID int64, excludeCancelled bool) (*domain.Order, error) {
	query := "SELECT " + orderColumns + " FROM tb_voucher_order WHERE user_id = ? AND voucher_id = ?"
	if excludeCancelled {
		query += " AND status <> 4"
	}
	query += " ORDER BY create_time DESC LIMIT 1"
	o, err := scanOrderRow(r.db.QueryRowContext(ctx, query, userID, voucherID))
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &o, nil
}

// UpdateOrderStatus 带前置状态的条件更新，返回是否命中。
func (r *ShopRepo) UpdateOrderStatus(ctx context.Context, id, userID int64, from, to int, setPayTime bool) (bool, error) {
	query := "UPDATE tb_voucher_order SET status = ?"
	if setPayTime {
		query += ", pay_time = NOW()"
	}
	query += " WHERE id = ? AND user_id = ? AND status = ?"
	result, err := r.db.ExecContext(ctx, query, to, id, userID, from)
	if err != nil {
		return false, err
	}
	affected, _ := result.RowsAffected()
	return affected > 0, nil
}
