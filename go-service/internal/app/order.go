package app

import (
	"context"
	"errors"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/infra/redislock"
	"xiangkou/go-service/internal/platform/errs"
)

// 与 既有服务 resources/seckill.lua 完全一致：判断库存与一人一单，扣减并投递 Stream。
const seckillScript = `
local voucherId = ARGV[1]
local userId = ARGV[2]
local orderId = ARGV[3]
local stockKey = 'seckill:stock:' .. voucherId
local orderKey = 'seckill:order:' .. voucherId
if tonumber(redis.call('get', stockKey)) <= 0 then return 1 end
if redis.call('sismember', orderKey, userId) == 1 then return 2 end
redis.call('incrby', stockKey, -1)
redis.call('sadd', orderKey, userId)
redis.call('xadd', 'stream.orders', '*', 'userId', userId, 'voucherId', voucherId, 'id', orderId)
return 0
`

type OrderRepo interface {
	GetVoucherBrief(ctx context.Context, id int64) (*domain.VoucherBrief, error)
	GetSeckill(ctx context.Context, id int64) (stock int, begin, end time.Time, ok bool, err error)
	CreateSeckillOrder(ctx context.Context, o domain.Order) error
	InsertOrder(ctx context.Context, o domain.Order) error
	GetOrder(ctx context.Context, id int64) (*domain.Order, error)
	ListOrders(ctx context.Context, userID int64, page int, heldOnly bool) ([]domain.Order, int64, error)
	LatestOrder(ctx context.Context, userID, voucherID int64, excludeCancelled bool) (*domain.Order, error)
	UpdateOrderStatus(ctx context.Context, id, userID int64, from, to int, setPayTime bool) (bool, error)
}

type Orders struct {
	repo        OrderRepo
	rdb         *redis.Client
	locks       *redislock.Client
	mockPayment bool
}

func NewOrders(repo OrderRepo, rdb *redis.Client, locks *redislock.Client, mockPayment bool) *Orders {
	return &Orders{repo: repo, rdb: rdb, locks: locks, mockPayment: mockPayment}
}

// nextOrderID 对齐 既有服务 RedisIdWorker：时间戳(秒) << 32 再拼 Redis 自增。
func (o *Orders) nextOrderID(ctx context.Context) (int64, error) {
	const epoch = 1640995200 // 2022-01-01 00:00:00
	sequence, err := o.rdb.Incr(ctx, "icr:order").Result()
	if err != nil {
		return 0, err
	}
	return (time.Now().Unix()-epoch)<<32 | (sequence & 0xFFFFFFFF), nil
}

func (o *Orders) Seckill(ctx context.Context, userID, voucherID int64) (int64, error) {
	if userID == 0 {
		return 0, errs.ErrUnauthorized
	}
	voucher, err := o.repo.GetVoucherBrief(ctx, voucherID)
	if err != nil {
		return 0, err
	}
	if voucher == nil || voucher.Status != 1 || voucher.Type != 1 {
		return 0, errs.New(1000, 200, "秒杀券不存在或已下架")
	}
	stock, begin, end, ok, err := o.repo.GetSeckill(ctx, voucherID)
	if err != nil {
		return 0, err
	}
	now := time.Now()
	if !ok || now.Before(begin) || now.After(end) {
		return 0, errs.New(1000, 200, "秒杀券不在可购买时段")
	}
	// 演示库可能没有 Redis 库存键，首次下单按数据库库存补齐。
	stockKey := "seckill:stock:" + strconv.FormatInt(voucherID, 10)
	if err := o.rdb.SetNX(ctx, stockKey, stock, 0).Err(); err != nil {
		return 0, err
	}
	orderID, err := o.nextOrderID(ctx)
	if err != nil {
		return 0, err
	}
	result, err := o.rdb.Eval(ctx, seckillScript, nil,
		strconv.FormatInt(voucherID, 10), strconv.FormatInt(userID, 10), strconv.FormatInt(orderID, 10)).Int()
	if err != nil {
		return 0, err
	}
	switch result {
	case 1:
		return 0, errs.New(1000, 200, "库存不足")
	case 2:
		return 0, errs.New(1000, 200, "不能重复下单")
	}
	return orderID, nil
}

func (o *Orders) OrderOrdinary(ctx context.Context, userID, voucherID int64) (int64, error) {
	if userID == 0 {
		return 0, errs.ErrUnauthorized
	}
	// 与旧实现一致：同一用户的普通下单串行化，避免并发点两次各建一单。
	lock, err := o.locks.Acquire(ctx, "lock:order:ordinary:"+strconv.FormatInt(userID, 10),
		redislock.Options{Lease: 10 * time.Second})
	if err != nil {
		if errors.Is(err, redislock.ErrBusy) {
			return 0, errs.New(1000, 200, "订单正在处理，请稍后查询")
		}
		return 0, err
	}
	defer lock.Release(ctx)
	voucher, err := o.repo.GetVoucherBrief(ctx, voucherID)
	if err != nil {
		return 0, err
	}
	if voucher == nil || voucher.Status != 1 || voucher.Type != 0 {
		return 0, errs.New(1000, 200, "普通券不存在或已下架")
	}
	existing, err := o.repo.LatestOrder(ctx, userID, voucherID, true)
	if err != nil {
		return 0, err
	}
	if existing != nil {
		return existing.ID, nil
	}
	orderID, err := o.nextOrderID(ctx)
	if err != nil {
		return 0, err
	}
	status := 1
	if voucher.PayValue == 0 {
		status = 2
	}
	if err := o.repo.InsertOrder(ctx, domain.Order{ID: orderID, UserID: userID, VoucherID: voucherID, Status: status}); err != nil {
		// 唯一索引兜底：并发下另一个请求已建单，直接返回已有订单。
		if errors.Is(err, domain.ErrDuplicateOrder) {
			if latest, findErr := o.repo.LatestOrder(ctx, userID, voucherID, true); findErr == nil && latest != nil {
				return latest.ID, nil
			}
		}
		return 0, errs.New(1000, 200, "创建订单失败")
	}
	return orderID, nil
}

func (o *Orders) PayMock(ctx context.Context, userID, orderID, expectedCents int64) (string, error) {
	if !o.mockPayment {
		return "", errs.New(1000, 200, "本地模拟支付未启用")
	}
	order, err := o.repo.GetOrder(ctx, orderID)
	if err != nil {
		return "", err
	}
	if order == nil || order.UserID != userID {
		return "", errs.New(1000, 200, "订单不存在")
	}
	voucher, err := o.repo.GetVoucherBrief(ctx, order.VoucherID)
	if err != nil {
		return "", err
	}
	if voucher == nil || voucher.PayValue <= 0 || voucher.PayValue != expectedCents {
		return "", errs.New(1000, 200, "支付金额不匹配，请重新查询")
	}
	if order.Status == 2 {
		return strconv.FormatInt(orderID, 10), nil
	}
	if order.Status != 1 {
		return "", errs.New(1000, 200, "订单不是待支付状态")
	}
	changed, err := o.repo.UpdateOrderStatus(ctx, orderID, userID, 1, 2, true)
	if err != nil {
		return "", err
	}
	if !changed {
		latest, _ := o.repo.GetOrder(ctx, orderID)
		if latest == nil || latest.UserID != userID || latest.Status != 2 {
			return "", errs.New(1000, 200, "订单状态已变化，请刷新后重试")
		}
	}
	return strconv.FormatInt(orderID, 10), nil
}

func (o *Orders) Cancel(ctx context.Context, userID, orderID int64) (int64, error) {
	order, err := o.repo.GetOrder(ctx, orderID)
	if err != nil {
		return 0, err
	}
	if order == nil || order.UserID != userID {
		return 0, errs.New(1000, 200, "订单不存在")
	}
	voucher, err := o.repo.GetVoucherBrief(ctx, order.VoucherID)
	if err != nil {
		return 0, err
	}
	if voucher == nil || voucher.Type != 0 || voucher.PayValue <= 0 {
		return 0, errs.New(1000, 200, "仅支持取消未支付的普通券订单")
	}
	if order.Status == 4 {
		return orderID, nil
	}
	if order.Status != 1 {
		return 0, errs.New(1000, 200, "订单不是待支付状态")
	}
	changed, err := o.repo.UpdateOrderStatus(ctx, orderID, userID, 1, 4, false)
	if err != nil {
		return 0, err
	}
	if !changed {
		return 0, errs.New(1000, 200, "订单状态已变化，请刷新后重试")
	}
	return orderID, nil
}

func (o *Orders) Complete(ctx context.Context, userID, orderID int64) (int64, error) {
	order, err := o.repo.GetOrder(ctx, orderID)
	if err != nil {
		return 0, err
	}
	if order == nil || order.UserID != userID {
		return 0, errs.New(1000, 200, "订单不存在")
	}
	if order.Status == 3 {
		return orderID, nil
	}
	if order.Status != 2 {
		return 0, errs.New(1000, 200, "订单不是已支付状态")
	}
	changed, err := o.repo.UpdateOrderStatus(ctx, orderID, userID, 2, 3, false)
	if err != nil {
		return 0, err
	}
	if !changed {
		return 0, errs.New(1000, 200, "订单状态已变化，请刷新后重试")
	}
	return orderID, nil
}

// Detail 订单详情：订单字段 + 券信息。
func (o *Orders) Detail(ctx context.Context, userID, orderID int64) (map[string]any, error) {
	order, err := o.repo.GetOrder(ctx, orderID)
	if err != nil {
		return nil, err
	}
	if order == nil || order.UserID != userID {
		return nil, errs.New(1000, 200, "订单不存在")
	}
	detail := map[string]any{
		"id": order.ID, "userId": order.UserID, "voucherId": order.VoucherID, "payType": order.PayType,
		"status": order.Status, "createTime": order.CreateTime.Format("2006-01-02T15:04:05"),
		"payTime": nil, "useTime": nil, "refundTime": nil,
		"updateTime": order.UpdateTime.Format("2006-01-02T15:04:05"),
	}
	if order.PayTime != nil {
		detail["payTime"] = order.PayTime.Format("2006-01-02T15:04:05")
	}
	if order.UseTime != nil {
		detail["useTime"] = order.UseTime.Format("2006-01-02T15:04:05")
	}
	if order.RefundTime != nil {
		detail["refundTime"] = order.RefundTime.Format("2006-01-02T15:04:05")
	}
	if voucher, _ := o.repo.GetVoucherBrief(ctx, order.VoucherID); voucher != nil {
		detail["voucherType"] = voucher.Type
		detail["payValue"] = voucher.PayValue
		detail["shopId"] = voucher.ShopID
		detail["voucherTitle"] = voucher.Title
	}
	return detail, nil
}

// MyVoucherOrder 最近一单（不限状态），没有则返回 nil。
func (o *Orders) MyVoucherOrder(ctx context.Context, userID, voucherID int64) (*domain.Order, error) {
	if userID == 0 {
		return nil, errs.ErrUnauthorized
	}
	return o.repo.LatestOrder(ctx, userID, voucherID, false)
}

// MyOrders 我的订单列表，键名与既有接口的 Map 一致（snake_case）。
func (o *Orders) MyOrders(ctx context.Context, userID int64, page int, heldOnly bool) ([]map[string]any, int64, error) {
	if userID == 0 {
		return nil, 0, errs.ErrUnauthorized
	}
	if page < 1 || page > 10000 {
		return nil, 0, errs.New(1000, 200, "分页参数无效")
	}
	orders, total, err := o.repo.ListOrders(ctx, userID, page, heldOnly)
	if err != nil {
		return nil, 0, err
	}
	items := make([]map[string]any, 0, len(orders))
	for _, order := range orders {
		item := map[string]any{
			"order_id":   strconv.FormatInt(order.ID, 10),
			"voucher_id": order.VoucherID,
			"status":     order.Status,
			"created_at": order.CreateTime.Format("2006-01-02T15:04:05"),
		}
		if voucher, _ := o.repo.GetVoucherBrief(ctx, order.VoucherID); voucher != nil {
			item["shop_id"] = voucher.ShopID
			item["title"] = voucher.Title
			item["pay_cents"] = voucher.PayValue
			item["voucher_type"] = voucher.Type
			item["rules"] = voucher.Rules
		}
		items = append(items, item)
	}
	return items, total, nil
}
