package mysql

import (
	"context"
	"fmt"

	"xiangkou/go-service/internal/domain"
)

// Catalog 对应 /shop/catalog：type_id 可选、按名称模糊、score/price 两种排序。
func (r *ShopRepo) Catalog(ctx context.Context, c domain.SearchCriteria) ([]domain.Shop, int64, error) {
	where := "1=1"
	args := make([]any, 0, 4)
	if c.TypeID > 0 {
		where += " AND type_id = ?"
		args = append(args, c.TypeID)
	}
	if c.Keyword != "" {
		where += " AND name LIKE ?"
		args = append(args, "%"+c.Keyword+"%")
	}
	order := "score DESC, id ASC"
	if c.Sort == "price" {
		order = "avg_price ASC, id ASC"
	}
	var total int64
	if err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM tb_shop WHERE "+where, args...).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("count catalog: %w", err)
	}
	rows, err := r.db.QueryContext(ctx,
		"SELECT "+shopColumns+" FROM tb_shop WHERE "+where+" ORDER BY "+order+" LIMIT ? OFFSET ?",
		append(args, c.Size, (c.Page-1)*c.Size)...)
	if err != nil {
		return nil, 0, fmt.Errorf("catalog: %w", err)
	}
	defer rows.Close()
	shops := make([]domain.Shop, 0, c.Size)
	for rows.Next() {
		shop, err := scanShop(rows)
		if err != nil {
			return nil, 0, err
		}
		shops = append(shops, shop)
	}
	return shops, total, rows.Err()
}
