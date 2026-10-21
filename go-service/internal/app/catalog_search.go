package app

import (
	"context"
	"strconv"
	"strings"
	"time"

	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/platform/errs"
)

// Search 校验与既有接口保持一致：非法参数返回 success=false + 对应文案，不走 400。
func (c *Catalog) Search(ctx context.Context, criteria domain.SearchCriteria) ([]domain.Shop, int64, error) {
	if criteria.TypeID <= 0 {
		return nil, 0, errs.New(1000, 200, "typeId 必须是正整数")
	}
	switch criteria.Sort {
	case "", "score", "price":
	default:
		return nil, 0, errs.New(1000, 200, "sort 只支持 score、price")
	}
	if criteria.MinScore != nil && (*criteria.MinScore < 0 || *criteria.MinScore > 50) {
		return nil, 0, errs.New(1000, 200, "minScore 必须为 0~50")
	}
	if criteria.Page < 1 || criteria.Size < 1 {
		return nil, 0, errs.New(1000, 200, "分页参数无效")
	}
	if criteria.Sort == "" {
		criteria.Sort = "score"
	}
	if strings.TrimSpace(criteria.Keyword) == "" {
		criteria.Keyword = ""
	}
	return c.shops.Search(ctx, criteria)
}

// ListVouchers 计算 orderable：普通券始终可下单；秒杀券实时读 Redis 库存并校验时段。
func (c *Catalog) ListVouchers(ctx context.Context, shopID int64) ([]domain.Voucher, error) {
	if shopID <= 0 {
		return nil, errs.New(1000, 200, "shopId 必须是正整数")
	}
	vouchers, err := c.shops.ListVouchers(ctx, shopID)
	if err != nil {
		return nil, err
	}
	now := time.Now()
	for index := range vouchers {
		item := &vouchers[index]
		if item.Type == 0 {
			item.Orderable = true
			continue
		}
		// 秒杀券：实时读 Redis 库存（Lua 同步扣减的权威值），覆盖 DB 初始库存。
		stockKey := "seckill:stock:" + strconv.FormatInt(item.ID, 10)
		if realtime, redisErr := c.rdb.Get(ctx, stockKey).Int(); redisErr == nil {
			item.Stock = &realtime
		}
		item.Orderable = item.Stock != nil && *item.Stock > 0 &&
			item.BeginTime != nil && item.EndTime != nil &&
			!now.Before(*item.BeginTime) && !now.After(*item.EndTime)
	}
	return vouchers, nil
}
