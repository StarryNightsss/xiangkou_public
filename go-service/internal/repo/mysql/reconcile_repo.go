package mysql

import (
	"context"
	"fmt"

	"xiangkou/go-service/internal/domain"
)

// SeckillVouchers 返回所有秒杀券的数据库库存（对账的基准）。
func (r *ShopRepo) SeckillVouchers(ctx context.Context) ([]domain.SeckillStock, error) {
	rows, err := r.db.QueryContext(ctx, "SELECT voucher_id, stock FROM tb_seckill_voucher")
	if err != nil {
		return nil, fmt.Errorf("query seckill vouchers: %w", err)
	}
	defer rows.Close()
	items := make([]domain.SeckillStock, 0, 8)
	for rows.Next() {
		var item domain.SeckillStock
		if err := rows.Scan(&item.VoucherID, &item.Stock); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

// UpdateSeckillStock 对账时把 Redis 实时库存写回 DB（Redis 是实时扣减权威）。
func (r *ShopRepo) UpdateSeckillStock(ctx context.Context, voucherID int64, stock int) error {
	_, err := r.db.ExecContext(ctx,
		"UPDATE tb_seckill_voucher SET stock = ? WHERE voucher_id = ?", stock, voucherID)
	if err != nil {
		return fmt.Errorf("update seckill stock: %w", err)
	}
	return nil
}

// SeckillOrderUsers 返回某张券下未取消订单的用户 id，用于和 Redis 的「一人一单」集合比对。
func (r *ShopRepo) SeckillOrderUsers(ctx context.Context, voucherID int64) ([]int64, error) {
	rows, err := r.db.QueryContext(ctx,
		"SELECT DISTINCT user_id FROM tb_voucher_order WHERE voucher_id = ? AND status <> 4", voucherID)
	if err != nil {
		return nil, fmt.Errorf("query seckill order users: %w", err)
	}
	defer rows.Close()
	users := make([]int64, 0, 16)
	for rows.Next() {
		var userID int64
		if err := rows.Scan(&userID); err != nil {
			return nil, err
		}
		users = append(users, userID)
	}
	return users, rows.Err()
}
