// Package redislock 基于 bsm/redislock 的薄封装：
//   - 获取失败返回 ErrBusy（对应 Java 的 tryLock 立即失败）
//   - 可选 Wait：在超时内轮询重试
//   - 看门狗：每 lease/3 调用库的 Refresh 续期，续期失败自动停
//
// 可重入不做：本项目调用点都是「单次进入临界区」，重入会掩盖真正的并发问题。
package redislock

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/bsm/redislock"
	"github.com/redis/go-redis/v9"
)

var ErrBusy = redislock.ErrNotObtained

type Options struct {
	Lease time.Duration // 默认 30s
	Wait  time.Duration // 默认 0：立即失败
}

type Client struct {
	inner *redislock.Client
}

func New(rdb *redis.Client) *Client {
	return &Client{inner: redislock.New(rdb)}
}

type Lock struct {
	lock  *redislock.Lock
	lease time.Duration
	stop  chan struct{}
	done  chan struct{}
	once  sync.Once
}

// Acquire 获取锁；busy 时返回 ErrBusy。
func (c *Client) Acquire(ctx context.Context, key string, opts Options) (*Lock, error) {
	lease := opts.Lease
	if lease <= 0 {
		lease = 30 * time.Second
	}
	deadline := time.Now().Add(opts.Wait)
	for {
		obtained, err := c.inner.Obtain(ctx, key, lease, nil)
		if err == nil {
			lock := &Lock{lock: obtained, lease: lease, stop: make(chan struct{}), done: make(chan struct{})}
			go lock.watchdog()
			return lock, nil
		}
		if !errors.Is(err, redislock.ErrNotObtained) {
			return nil, err
		}
		if opts.Wait <= 0 || time.Now().After(deadline) {
			return nil, ErrBusy
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
}

// watchdog 定期续期；续期失败说明锁已不属于自己，停止即可，由业务超时兜底。
func (l *Lock) watchdog() {
	defer close(l.done)
	interval := l.lease / 3
	if interval < 100*time.Millisecond {
		interval = 100 * time.Millisecond
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-l.stop:
			return
		case <-ticker.C:
			if err := l.lock.Refresh(context.Background(), l.lease, nil); err != nil {
				return
			}
		}
	}
}

// Release 停止续期后释放；重复调用安全。
func (l *Lock) Release(ctx context.Context) {
	if l == nil {
		return
	}
	l.once.Do(func() {
		close(l.stop)
		select {
		case <-l.done:
		case <-time.After(time.Second):
		}
		_ = l.lock.Release(context.WithoutCancel(ctx))
	})
}
