// Package cache 提供 cache-aside + singleflight 回源 + 空值缓存 + TTL 抖动。
package cache

import (
	"context"
	"encoding/json"
	"errors"
	"math/rand"
	"time"

	"github.com/redis/go-redis/v9"
	"xiangkou/go-service/internal/infra/redislock"
	"golang.org/x/sync/singleflight"
)

var ErrNotFound = errors.New("cache: not found")

const nullMarker = "__null__"

type Store struct {
	rdb   *redis.Client
	ttl   time.Duration
	group singleflight.Group
	locks *redislock.Client
}

func New(rdb *redis.Client, ttl time.Duration, locks *redislock.Client) *Store {
	return &Store{rdb: rdb, ttl: ttl, locks: locks}
}

// GetOrLoad 命中直接返回；未命中时用 singleflight 合并回源，避免击穿。
// load 返回 (nil, nil) 表示数据不存在，会写入短 TTL 的空值标记防止穿透。
func (s *Store) GetOrLoad(ctx context.Context, key string, dest any, load func(context.Context) (any, error)) error {
	if raw, err := s.rdb.Get(ctx, key).Bytes(); err == nil {
		return decode(raw, dest)
	} else if !errors.Is(err, redis.Nil) {
		return err
	}
	value, err, _ := s.group.Do(key, func() (any, error) {
		// 双检：可能已经有并发请求回源完成。
		if raw, err := s.rdb.Get(ctx, key).Bytes(); err == nil {
			return raw, nil
		}
		// 跨实例互斥：与旧实现的重建锁一致，避免多实例同时打库；
		// 拿不到锁时等一会再读缓存，仍没有则直接回源（避免请求被锁拖死）。
		if lock, lockErr := s.locks.Acquire(ctx, "lock:"+key,
			redislock.Options{Lease: 10 * time.Second, Wait: 500 * time.Millisecond}); lockErr == nil {
			defer lock.Release(ctx)
			if raw, err := s.rdb.Get(ctx, key).Bytes(); err == nil {
				return raw, nil
			}
			loaded, err := load(ctx)
		if err != nil {
			return nil, err
		}
		if loaded == nil {
			_ = s.rdb.Set(ctx, key, nullMarker, time.Minute).Err()
			return nil, ErrNotFound
		}
			raw, err := json.Marshal(loaded)
			if err != nil {
				return nil, err
			}
			_ = s.rdb.Set(ctx, key, raw, jitter(s.ttl)).Err()
			return raw, nil
		}
		loaded, err := load(ctx)
		if err != nil {
			return nil, err
		}
		if loaded == nil {
			_ = s.rdb.Set(ctx, key, nullMarker, time.Minute).Err()
			return nil, ErrNotFound
		}
		raw, err := json.Marshal(loaded)
		if err != nil {
			return nil, err
		}
		_ = s.rdb.Set(ctx, key, raw, jitter(s.ttl)).Err()
		return raw, nil
	})
	if err != nil {
		return err
	}
	raw, ok := value.([]byte)
	if !ok {
		return errors.New("cache: unexpected value")
	}
	return decode(raw, dest)
}

func decode(raw []byte, dest any) error {
	if string(raw) == nullMarker {
		return ErrNotFound
	}
	return json.Unmarshal(raw, dest)
}

// jitter 在 TTL 上叠加 ±10% 随机量，避免一批 key 同时过期造成雪崩。
func jitter(ttl time.Duration) time.Duration {
	if ttl <= 0 {
		return time.Minute
	}
	delta := float64(ttl) * 0.1
	return ttl + time.Duration((rand.Float64()*2-1)*delta)
}
