package app

import (
	"context"
	"fmt"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/platform/errs"
)

// ScrollResult 与既有接口的 ScrollResult 对齐：list + minTime + offset。
type ScrollResult struct {
	List    []domain.Blog `json:"list"`
	MinTime int64         `json:"minTime"`
	Offset  int           `json:"offset"`
}

// MyBlogs 当前用户发布的笔记（按创建时间倒序分页）。
func (c *Community) MyBlogs(ctx context.Context, userID int64, page int) ([]domain.Blog, error) {
	if userID == 0 {
		return nil, errs.ErrUnauthorized
	}
	return c.BlogsByUser(ctx, userID, page)
}

// Feed 关注流：从收件箱 ZSet 倒序取 2 条，按分数（时间戳）计算下一页的 max 与偏移。
func (c *Community) Feed(ctx context.Context, userID int64, max int64, offset int) (*ScrollResult, error) {
	if userID == 0 {
		return nil, errs.ErrUnauthorized
	}
	if max <= 0 {
		max = 1 << 62
	}
	key := fmt.Sprintf("feed:%d", userID)
	tuples, err := c.rdb.ZRevRangeByScoreWithScores(ctx, key, &redis.ZRangeBy{
		Min: "0", Max: fmt.Sprintf("%d", max), Offset: int64(offset), Count: 2,
	}).Result()
	if err != nil {
		return nil, err
	}
	if len(tuples) == 0 {
		// 与 既有服务 一致：没有更多数据时返回 success=true 且不带 data。
		return nil, nil
	}
	ids := make([]int64, 0, len(tuples))
	minTime := int64(0)
	nextOffset := 1
	for _, tuple := range tuples {
		text, _ := tuple.Member.(string)
		var id int64
		fmt.Sscanf(text, "%d", &id)
		ids = append(ids, id)
		score := int64(tuple.Score)
		if score == minTime {
			nextOffset++
		} else {
			minTime = score
			nextOffset = 1
		}
	}
	blogs, err := c.shops.ListBlogsByIDs(ctx, ids)
	if err != nil {
		return nil, err
	}
	c.fillAuthorState(ctx, blogs, userID, true)
	return &ScrollResult{List: blogs, MinTime: minTime, Offset: nextOffset}, nil
}
