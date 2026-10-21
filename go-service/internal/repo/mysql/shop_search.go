package mysql

import (
	"context"
	"fmt"
	"strings"

	"xiangkou/go-service/internal/domain"
)

const shopColumns = `id, name, type_id, COALESCE(images,''), COALESCE(area,''), COALESCE(address,''),
                     COALESCE(x,0), COALESCE(y,0), COALESCE(avg_price,0), COALESCE(sold,0),
                     COALESCE(comments,0), COALESCE(score,0), COALESCE(open_hours,''),
                     create_time, update_time`

func scanShop(row interface{ Scan(...any) error }) (domain.Shop, error) {
	var shop domain.Shop
	err := row.Scan(&shop.ID, &shop.Name, &shop.TypeID, &shop.Images, &shop.Area, &shop.Address,
		&shop.X, &shop.Y, &shop.AvgPrice, &shop.Sold, &shop.Comments, &shop.Score,
		&shop.OpenHours, &shop.CreateTime, &shop.UpdateTime)
	return shop, err
}

// Search 按类型/关键词/区域/预算/评分/有无券筛选，评分或价格排序，SQL 分页。
func (r *ShopRepo) Search(ctx context.Context, c domain.SearchCriteria) ([]domain.Shop, int64, error) {
	where, args := shopWhere(c)
	order := "score DESC, avg_price ASC, id ASC"
	if c.Sort == "price" {
		order = "avg_price ASC, score DESC, id ASC"
	}
	var total int64
	if err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM tb_shop WHERE "+where, args...).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("count shops: %w", err)
	}
	query := "SELECT " + shopColumns + " FROM tb_shop WHERE " + where + " ORDER BY " + order + " LIMIT ? OFFSET ?"
	rows, err := r.db.QueryContext(ctx, query, append(args, c.Size, (c.Page-1)*c.Size)...)
	if err != nil {
		return nil, 0, fmt.Errorf("search shops: %w", err)
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

func shopWhere(c domain.SearchCriteria) (string, []any) {
	clauses := []string{"type_id = ?"}
	args := []any{c.TypeID}
	if keyword := strings.TrimSpace(c.Keyword); keyword != "" {
		clauses = append(clauses, "name LIKE ?")
		args = append(args, "%"+keyword+"%")
	}
	if area := normalizeArea(c.Area); area != "" {
		clauses = append(clauses, "(area LIKE ? OR address LIKE ?)")
		args = append(args, "%"+area+"%", "%"+area+"%")
	}
	if c.MaxPrice != nil {
		clauses = append(clauses, "avg_price <= ?")
		args = append(args, *c.MaxPrice)
	}
	if c.MinScore != nil {
		clauses = append(clauses, "score >= ?")
		args = append(args, *c.MinScore)
	}
	if c.HasVoucher {
		clauses = append(clauses, `EXISTS (SELECT 1 FROM tb_voucher v
			LEFT JOIN tb_seckill_voucher sv ON sv.voucher_id = v.id
			WHERE v.shop_id = tb_shop.id AND v.status = 1
			AND (v.type = 0 OR (v.type = 1 AND sv.stock > 0
			AND sv.begin_time <= NOW() AND sv.end_time >= NOW())))`)
	}
	return strings.Join(clauses, " AND "), args
}

// normalizeArea 把「南山区」这类行政区说法归一成商圈前缀。
func normalizeArea(area string) string {
	area = strings.TrimSpace(area)
	for _, suffix := range []string{"区", "市"} {
		if strings.HasSuffix(area, suffix) && len([]rune(area)) > 1 {
			return strings.TrimSuffix(area, suffix)
		}
	}
	return area
}

// ListVouchers 按店铺取在架券，秒杀券带库存与时段。
func (r *ShopRepo) ListVouchers(ctx context.Context, shopID int64) ([]domain.Voucher, error) {
	const query = `SELECT v.id, v.shop_id, v.title, COALESCE(v.sub_title,''), COALESCE(v.rules,''),
	                      v.pay_value, v.actual_value, v.type, sv.stock, sv.begin_time, sv.end_time
	               FROM tb_voucher v
	               LEFT JOIN tb_seckill_voucher sv ON v.id = sv.voucher_id
	               WHERE v.shop_id = ? AND v.status = 1`
	rows, err := r.db.QueryContext(ctx, query, shopID)
	if err != nil {
		return nil, fmt.Errorf("list vouchers: %w", err)
	}
	defer rows.Close()
	vouchers := make([]domain.Voucher, 0, 8)
	for rows.Next() {
		var item domain.Voucher
		if err := rows.Scan(&item.ID, &item.ShopID, &item.Title, &item.SubTitle, &item.Rules,
			&item.PayValue, &item.ActualValue, &item.Type, &item.Stock, &item.BeginTime, &item.EndTime); err != nil {
			return nil, err
		}
		vouchers = append(vouchers, item)
	}
	return vouchers, rows.Err()
}
