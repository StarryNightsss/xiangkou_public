// Package jobs 的后台任务：秒杀 Stream 消费者 + 库存/一人一单对账。
package jobs

import (
	"context"
	"errors"
	"log/slog"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/domain"
)

type ReconcileRepo interface {
	SeckillVouchers(ctx context.Context) ([]domain.SeckillStock, error)
	SeckillOrderUsers(ctx context.Context, voucherID int64) ([]int64, error)
	UpdateSeckillStock(ctx context.Context, voucherID int64, stock int) error
}

// Reconciler 让 Redis 的库存与「一人一单」集合与数据库双向收敛：
//   - Redis key 存在且库存与 DB 不一致：以 Redis 实时扣减为准回写 DB（下单走 Lua 扣 Redis，消费者异步落库）
//   - Redis key 不存在（重启或丢失）：从 DB 恢复 Redis 库存
//   - Redis 有、DB 无：说明订单没落库，移除集合成员让用户可以重试
//   - DB 有、Redis 无：补上集合成员，防止重复下单
type Reconciler struct {
	rdb      *redis.Client
	repo     ReconcileRepo
	interval time.Duration
}

func NewReconciler(rdb *redis.Client, repo ReconcileRepo, interval time.Duration) *Reconciler {
	if interval <= 0 {
		interval = 5 * time.Minute
	}
	return &Reconciler{rdb: rdb, repo: repo, interval: interval}
}

func (r *Reconciler) Run(ctx context.Context) {
	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := r.ReconcileOnce(ctx); err != nil {
				slog.Error("seckill reconcile failed", "err", err)
			}
		}
	}
}

// ReconcileOnce 执行一轮对账，返回修正的条数。
func (r *Reconciler) ReconcileOnce(ctx context.Context) error {
	vouchers, err := r.repo.SeckillVouchers(ctx)
	if err != nil {
		return err
	}
	for _, voucher := range vouchers {
		stockKey := "seckill:stock:" + strconv.FormatInt(voucher.VoucherID, 10)
		orderKey := "seckill:order:" + strconv.FormatInt(voucher.VoucherID, 10)

		// 库存双向对账：Redis key 存在则以 Redis 实时扣减为准回写 DB；
		// key 不存在（Redis 重启/丢失）则从 DB 恢复 Redis。
		current, err := r.rdb.Get(ctx, stockKey).Int()
		if errors.Is(err, redis.Nil) {
			_ = r.rdb.Set(ctx, stockKey, voucher.Stock, 0).Err()
		} else if err == nil {
			if current != voucher.Stock {
				slog.Warn("seckill stock synced from redis", "voucher", voucher.VoucherID,
					"redis", current, "db", voucher.Stock)
				_ = r.repo.UpdateSeckillStock(ctx, voucher.VoucherID, current)
			}
		} else {
			return err
		}

		users, err := r.repo.SeckillOrderUsers(ctx, voucher.VoucherID)
		if err != nil {
			return err
		}
		dbUsers := make(map[string]bool, len(users))
		for _, userID := range users {
			dbUsers[strconv.FormatInt(userID, 10)] = true
		}
		redisUsers, err := r.rdb.SMembers(ctx, orderKey).Result()
		if err != nil {
			return err
		}
		redisSet := make(map[string]bool, len(redisUsers))
		for _, value := range redisUsers {
			redisSet[value] = true
			if !dbUsers[value] {
				slog.Warn("seckill orphan member removed", "voucher", voucher.VoucherID, "user", value)
				_ = r.rdb.SRem(ctx, orderKey, value).Err()
			}
		}
		for userID := range dbUsers {
			if !redisSet[userID] {
				slog.Warn("seckill missing member restored", "voucher", voucher.VoucherID, "user", userID)
				_ = r.rdb.SAdd(ctx, orderKey, userID).Err()
			}
		}
	}
	return nil
}
