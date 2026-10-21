package app

import (
	"context"
	"strconv"
	"strings"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/platform/errs"
)

const (
	blogPageSize     = 10
	maxPage          = 10000
	maxCatalogSize   = 20
	defaultCatSize   = 12
	maxKeywordLength = 50
)

// Community 负责社区只读：热门、详情、按店铺/用户、笔记检索、评论、关注状态。
type Community struct {
	shops ShopReader
	rdb   *redis.Client
}

func NewCommunity(shops ShopReader, rdb *redis.Client) *Community {
	return &Community{shops: shops, rdb: rdb}
}

func (c *Community) HotBlogs(ctx context.Context, page int, userID int64) ([]domain.Blog, error) {
	if page < 1 || page > maxPage {
		return nil, errs.New(1000, 200, "分页参数无效")
	}
	blogs, err := c.shops.ListHotBlogs(ctx, page, blogPageSize)
	if err != nil {
		return nil, err
	}
	c.fillAuthorState(ctx, blogs, userID, true)
	return blogs, nil
}

func (c *Community) Blog(ctx context.Context, id int64, userID int64) (*domain.Blog, error) {
	if id <= 0 {
		return nil, errs.ErrBadRequest
	}
	blog, err := c.shops.GetBlog(ctx, id)
	if err != nil {
		return nil, err
	}
	if blog == nil {
		return nil, errs.New(1003, 200, "笔记不存在！")
	}
	blogs := []domain.Blog{*blog}
	c.fillAuthorState(ctx, blogs, userID, true)
	return &blogs[0], nil
}

func (c *Community) BlogsByShop(ctx context.Context, shopID int64, page int) ([]domain.Blog, int64, error) {
	if shopID <= 0 || page < 1 || page > maxPage {
		return nil, 0, errs.New(1000, 200, "查询参数无效")
	}
	return c.shops.ListBlogsBy(ctx, "shop_id", shopID, page, blogPageSize)
}

func (c *Community) BlogsByUser(ctx context.Context, userID int64, page int) ([]domain.Blog, error) {
	if userID <= 0 || page < 1 || page > maxPage {
		return nil, errs.New(1000, 200, "查询参数无效")
	}
	blogs, _, err := c.shops.ListBlogsBy(ctx, "user_id", userID, page, blogPageSize)
	return blogs, err
}

func (c *Community) SearchBlogs(ctx context.Context, shopID int64, keyword string, current int) ([]domain.Blog, int64, error) {
	trimmed := strings.TrimSpace(keyword)
	length := len([]rune(trimmed))
	if shopID <= 0 || current < 1 || current > 100000 || length < 2 || length > 40 {
		return nil, 0, errs.New(1000, 200, "shopId、keyword 或 current 不合法")
	}
	return c.shops.SearchBlogs(ctx, shopID, trimmed, current)
}

func (c *Community) Comments(ctx context.Context, blogID int64) ([]domain.Comment, error) {
	if blogID <= 0 {
		return nil, errs.ErrBadRequest
	}
	return c.shops.ListComments(ctx, blogID)
}

func (c *Community) IsFollowing(ctx context.Context, userID, authorID int64) (bool, error) {
	if userID == 0 {
		return false, errs.ErrUnauthorized
	}
	return c.shops.IsFollowing(ctx, userID, authorID)
}

// Catalog 复刻 /shop/catalog：参数校验、默认 size 12、最多 20。
func (c *Catalog) Catalog(ctx context.Context, keyword string, typeID *int64, page, size int, sort string) ([]domain.Shop, int64, error) {
	if page < 1 || page > maxPage || size < 1 || size > maxCatalogSize || len([]rune(keyword)) > maxKeywordLength ||
		(typeID != nil && *typeID <= 0) || (sort != "score" && sort != "price") {
		return nil, 0, errs.New(1000, 200, "店铺查询参数无效")
	}
	criteria := domain.SearchCriteria{
		Keyword: keyword, Sort: sort, Page: page, Size: size,
	}
	if typeID != nil {
		criteria.TypeID = *typeID
	}
	return c.shops.Catalog(ctx, criteria)
}

func (c *Community) fillAuthorState(ctx context.Context, blogs []domain.Blog, userID int64, withAuthor bool) {
	for index := range blogs {
		if withAuthor && blogs[index].Name == nil {
			empty := ""
			blogs[index].Name, blogs[index].Icon = &empty, &empty
		}
		liked := false
		if userID > 0 {
			score, err := c.rdb.ZScore(ctx, "blog:liked:"+strconv.FormatInt(blogs[index].ID, 10),
				strconv.FormatInt(userID, 10)).Result()
			if err == nil && score > 0 {
				liked = true
			}
		}
		blogs[index].IsLike = &liked
	}
}
