package jobs

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/domain"
)

type fakeRepo struct {
	stocks       []domain.SeckillStock
	users        map[int64][]int64
	updatedStock map[int64]int
}

func (f *fakeRepo) SeckillVouchers(context.Context) ([]domain.SeckillStock, error) {
	return f.stocks, nil
}

func (f *fakeRepo) SeckillOrderUsers(_ context.Context, voucherID int64) ([]int64, error) {
	return f.users[voucherID], nil
}

func (f *fakeRepo) UpdateSeckillStock(_ context.Context, voucherID int64, stock int) error {
	if f.updatedStock == nil {
		f.updatedStock = map[int64]int{}
	}
	f.updatedStock[voucherID] = stock
	return nil
}

func setup(t *testing.T, repo *fakeRepo) (*Reconciler, *miniredis.Miniredis) {
	t.Helper()
	server := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	return NewReconciler(rdb, repo, time.Minute), server
}

// Redis key 存在时以实时扣减的 Redis 库存为准回写 DB，而不是拿 DB 旧值覆盖 Redis。
func TestReconcileWritesRedisBackToDB(t *testing.T) {
	repo := &fakeRepo{stocks: []domain.SeckillStock{{VoucherID: 1002, Stock: 3}}}
	reconciler, server := setup(t, repo)
	server.Set("seckill:stock:1002", "1")

	if err := reconciler.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if got := repo.updatedStock[1002]; got != 1 {
		t.Fatalf("db stock not updated from redis: got %d, want 1", got)
	}
	if got, _ := server.Get("seckill:stock:1002"); got != "1" {
		t.Fatalf("redis stock overwritten: %q", got)
	}
}

// Redis key 不存在（重启或丢失）时从 DB 恢复 Redis 库存。
func TestReconcileInitializesMissingStock(t *testing.T) {
	reconciler, server := setup(t, &fakeRepo{
		stocks: []domain.SeckillStock{{VoucherID: 2001, Stock: 7}},
	})
	if err := reconciler.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if got, _ := server.Get("seckill:stock:2001"); got != "7" {
		t.Fatalf("stock not initialized: %q", got)
	}
}

func TestReconcileRemovesOrphanMember(t *testing.T) {
	reconciler, server := setup(t, &fakeRepo{
		stocks: []domain.SeckillStock{{VoucherID: 1002, Stock: 0}},
		users:  map[int64][]int64{1002: {}},
	})
	server.SAdd("seckill:order:1002", "9")

	if err := reconciler.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	members, _ := server.SMembers("seckill:order:1002")
	if len(members) != 0 {
		t.Fatalf("orphan member not removed: %v", members)
	}
}

func TestReconcileRestoresMissingMember(t *testing.T) {
	reconciler, server := setup(t, &fakeRepo{
		stocks: []domain.SeckillStock{{VoucherID: 1002, Stock: 0}},
		users:  map[int64][]int64{1002: {8}},
	})
	if err := reconciler.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	members, _ := server.SMembers("seckill:order:1002")
	if len(members) != 1 || members[0] != "8" {
		t.Fatalf("missing member not restored: %v", members)
	}
}
