package redislock

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func newClient(t *testing.T) (*Client, *miniredis.Miniredis) {
	t.Helper()
	server := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	return New(rdb), server
}

func TestBusyWhenHeld(t *testing.T) {
	client, _ := newClient(t)
	ctx := context.Background()

	first, err := client.Acquire(ctx, "lock:order:1", Options{Lease: time.Second})
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}
	if _, err := client.Acquire(ctx, "lock:order:1", Options{Lease: time.Second}); !errors.Is(err, ErrBusy) {
		t.Fatalf("second acquire should be busy, got %v", err)
	}
	first.Release(ctx)
}

func TestWaitSucceedsAfterRelease(t *testing.T) {
	client, _ := newClient(t)
	ctx := context.Background()

	first, err := client.Acquire(ctx, "lock:cache:5", Options{Lease: time.Second})
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}
	go func() {
		time.Sleep(120 * time.Millisecond)
		first.Release(ctx)
	}()
	second, err := client.Acquire(ctx, "lock:cache:5", Options{Lease: time.Second, Wait: 2 * time.Second})
	if err != nil {
		t.Fatalf("acquire with wait: %v", err)
	}
	second.Release(ctx)
}

func TestReleaseRemovesKey(t *testing.T) {
	client, server := newClient(t)
	ctx := context.Background()

	lock, err := client.Acquire(ctx, "lock:release", Options{Lease: time.Second})
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	lock.Release(ctx)
	if len(server.Keys()) != 0 {
		t.Fatalf("lock key should be removed, got %v", server.Keys())
	}
}

func TestWatchdogRenewsLease(t *testing.T) {
	client, server := newClient(t)
	ctx := context.Background()

	lock, err := client.Acquire(ctx, "lock:watchdog", Options{Lease: 300 * time.Millisecond})
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	defer lock.Release(ctx)

	time.Sleep(500 * time.Millisecond) // 超过 lease，若未续期锁已消失
	if !server.Exists("lock:watchdog") {
		t.Fatal("watchdog did not renew the lease")
	}
}
