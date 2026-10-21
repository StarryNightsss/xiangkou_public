package mysql

import (
	"context"
	"fmt"

	"xiangkou/go-service/internal/domain"
)

// InsertVoucher 插入普通券或秒杀券；seckill 为 true 时同时写库存与时段。
func (r *ShopRepo) InsertVoucher(ctx context.Context, v domain.VoucherBrief, seckill bool, stock int, begin, end string) (int64, error) {
	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback() }()
	result, err := tx.ExecContext(ctx,
		`INSERT INTO tb_voucher (shop_id, title, sub_title, rules, pay_value, actual_value, type, status, create_time, update_time)
		 VALUES (?, ?, ?, ?, ?, ?, ?, 1, NOW(), NOW())`,
		v.ShopID, v.Title, "", v.Rules, v.PayValue, v.ActualValue, v.Type)
	if err != nil {
		return 0, fmt.Errorf("insert voucher: %w", err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, err
	}
	if seckill {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO tb_seckill_voucher (voucher_id, stock, begin_time, end_time, create_time, update_time)
			 VALUES (?, ?, ?, ?, NOW(), NOW())`, id, stock, begin, end); err != nil {
			return 0, fmt.Errorf("insert seckill voucher: %w", err)
		}
	}
	return id, tx.Commit()
}

// DelistVoucher 下架：status 置 0。
func (r *ShopRepo) DelistVoucher(ctx context.Context, id int64) (bool, error) {
	result, err := r.db.ExecContext(ctx, "UPDATE tb_voucher SET status = 0 WHERE id = ?", id)
	if err != nil {
		return false, err
	}
	affected, _ := result.RowsAffected()
	return affected > 0, nil
}
