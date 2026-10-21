// Package app 是用例层：编排缓存与数据访问，不关心 HTTP 细节。
package app

import (
	"context"
	"errors"
	"fmt"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/platform/errs"
	"xiangkou/go-service/internal/repo/cache"
)

type ShopReader interface {
	Get(ctx context.Context, id int64) (*domain.Shop, error)
	ListTypes(ctx context.Context) ([]domain.ShopType, error)
	ListAreas(ctx context.Context) ([]string, error)
	Search(ctx context.Context, criteria domain.SearchCriteria) ([]domain.Shop, int64, error)
	ListVouchers(ctx context.Context, shopID int64) ([]domain.Voucher, error)
	Catalog(ctx context.Context, criteria domain.SearchCriteria) ([]domain.Shop, int64, error)
	BatchShops(ctx context.Context, ids []int64) ([]domain.Shop, error)
	ListHotBlogs(ctx context.Context, page, size int) ([]domain.Blog, error)
	GetBlog(ctx context.Context, id int64) (*domain.Blog, error)
	ListBlogsBy(ctx context.Context, column string, value int64, page, size int) ([]domain.Blog, int64, error)
	SearchBlogs(ctx context.Context, shopID int64, keyword string, current int) ([]domain.Blog, int64, error)
	ListComments(ctx context.Context, blogID int64) ([]domain.Comment, error)
	IsFollowing(ctx context.Context, userID, authorID int64) (bool, error)
	ListBlogsByIDs(ctx context.Context, ids []int64) ([]domain.Blog, error)
}

type Catalog struct {
	shops ShopReader
	cache *cache.Store
	rdb   *redis.Client
}

func NewCatalog(shops ShopReader, store *cache.Store, rdb *redis.Client) *Catalog {
	return &Catalog{shops: shops, cache: store, rdb: rdb}
}

// GetShop 走缓存三件套：命中直接返回；未命中 singleflight 合并回源；不存在写空值标记。
func (c *Catalog) GetShop(ctx context.Context, id int64) (*domain.Shop, error) {
	if id <= 0 {
		return nil, errs.ErrBadRequest
	}
	var shop domain.Shop
	err := c.cache.GetOrLoad(ctx, fmt.Sprintf("cache:shop:%d", id), &shop, func(ctx context.Context) (any, error) {
		loaded, err := c.shops.Get(ctx, id)
		if err != nil {
			return nil, err
		}
		if loaded == nil {
			return nil, nil
		}
		return loaded, nil
	})
	if errors.Is(err, cache.ErrNotFound) {
		return nil, errs.New(1003, 200, "店铺不存在")
	}
	if err != nil {
		return nil, err
	}
	return &shop, nil
}

func (c *Catalog) ListTypes(ctx context.Context) ([]domain.ShopType, error) {
	var types []domain.ShopType
	err := c.cache.GetOrLoad(ctx, "cache:shoptype:all", &types, func(ctx context.Context) (any, error) {
		return c.shops.ListTypes(ctx)
	})
	if err != nil {
		return nil, err
	}
	return types, nil
}

func (c *Catalog) ListAreas(ctx context.Context) ([]string, error) {
	return c.shops.ListAreas(ctx)
}

// Batch 批量取店铺，最多 40 个。
func (c *Catalog) Batch(ctx context.Context, ids []int64) ([]domain.Shop, error) {
	return c.shops.BatchShops(ctx, ids)
}
