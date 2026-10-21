// Package jobs 放后台任务：目前是秒杀订单的 Stream 消费者。
package jobs

import (
	"context"
	"errors"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/app"
	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/infra/redislock"
)

const (
	orderStream = "stream.orders"
	orderGroup  = "g1"
	dlqStream   = "stream.orders.dlq"
	maxRetries  = 3
)

// SeckillConsumer 消费 stream.orders：写订单成功才 ACK，失败保留在 pending 里重试，超过上限转死信。
type SeckillConsumer struct {
	rdb    *redis.Client
	orders *app.Orders
	repo   app.OrderRepo
	locks  *redislock.Client
}

func NewSeckillConsumer(rdb *redis.Client, orders *app.Orders, repo app.OrderRepo) *SeckillConsumer {
	return &SeckillConsumer{rdb: rdb, orders: orders, repo: repo, locks: redislock.New(rdb)}
}

// Run 阻塞消费，直到 ctx 取消。
func (c *SeckillConsumer) Run(ctx context.Context) {
	c.ensureGroup(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		messages, err := c.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group: orderGroup, Consumer: "c1", Streams: []string{orderStream, ">"},
			Count: 1, Block: 2 * time.Second,
		}).Result()
		if errors.Is(err, redis.Nil) {
			continue
		}
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.Error("seckill consumer read failed", "err", err)
			time.Sleep(time.Second)
			continue
		}
		for _, stream := range messages {
			for _, message := range stream.Messages {
				c.handle(ctx, message, true)
			}
		}
	}
}

func (c *SeckillConsumer) ensureGroup(ctx context.Context) {
	err := c.rdb.XGroupCreateMkStream(ctx, orderStream, orderGroup, "$").Err()
	if err != nil && !strings.Contains(err.Error(), "BUSYGROUP") {
		slog.Error("create consumer group failed", "err", err)
	}
}

func (c *SeckillConsumer) handle(ctx context.Context, message redis.XMessage, fresh bool) {
	userID, _ := strconv.ParseInt(asString(message.Values["userId"]), 10, 64)
	voucherID, _ := strconv.ParseInt(asString(message.Values["voucherId"]), 10, 64)
	orderID, _ := strconv.ParseInt(asString(message.Values["id"]), 10, 64)
	if userID == 0 || voucherID == 0 || orderID == 0 {
		c.deadLetter(ctx, message, "malformed message")
		return
	}
	status := 1
	if voucher, _ := c.repo.GetVoucherBrief(ctx, voucherID); voucher != nil && voucher.PayValue == 0 {
		status = 2
	}
	// 与旧实现一致：同一用户的落库加锁，重复消息串行处理。
	lock, lockErr := c.locks.Acquire(ctx, "lock:order:"+strconv.FormatInt(userID, 10),
		redislock.Options{Lease: 30 * time.Second})
	if lockErr != nil {
		c.retryLater(ctx, message, lockErr)
		return
	}
	defer lock.Release(ctx)
	err := c.repo.CreateSeckillOrder(ctx, domain.Order{ID: orderID, UserID: userID, VoucherID: voucherID, Status: status})
	if err == nil {
		_ = c.rdb.XAck(ctx, orderStream, orderGroup, message.ID).Err()
		return
	}
	// 唯一索引兜底：订单已存在说明这条消息是重复投递，直接 ACK，不再重试。
	if errors.Is(err, domain.ErrDuplicateOrder) {
		_ = c.rdb.XAck(ctx, orderStream, orderGroup, message.ID).Err()
		slog.Info("seckill duplicate message acked", "id", message.ID, "order", orderID)
		return
	}
	c.retryLater(ctx, message, err)
}

// retryLater 保留 pending 以便重试；累计 3 次仍失败则进死信并 ACK，避免阻塞队列。
func (c *SeckillConsumer) retryLater(ctx context.Context, message redis.XMessage, cause error) {
	attempts, _ := c.rdb.HIncrBy(ctx, "seckill:retry:"+message.ID, "n", 1).Result()
	if attempts >= maxRetries {
		c.deadLetter(ctx, message, cause.Error())
		return
	}
	slog.Warn("seckill order retry", "id", message.ID, "attempts", attempts, "err", cause)
}

func (c *SeckillConsumer) deadLetter(ctx context.Context, message redis.XMessage, reason string) {
	values := map[string]any{"originalId": message.ID, "reason": reason, "at": time.Now().Unix()}
	for key, value := range message.Values {
		values[key] = value
	}
	_ = c.rdb.XAdd(ctx, &redis.XAddArgs{Stream: dlqStream, Values: values}).Err()
	_ = c.rdb.XAck(ctx, orderStream, orderGroup, message.ID).Err()
	_ = c.rdb.Del(ctx, "seckill:retry:"+message.ID).Err()
	slog.Error("seckill message moved to dlq", "id", message.ID, "reason", reason)
}

func asString(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case []byte:
		return string(typed)
	default:
		return ""
	}
}
