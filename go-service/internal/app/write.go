package app

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/platform/errs"
)

type WriteReader interface {
	InsertBlog(ctx context.Context, shopID, userID int64, title, images, content string) (int64, error)
	AddBlogLiked(ctx context.Context, blogID int64, delta int) (bool, error)
	BlogExists(ctx context.Context, blogID int64) (bool, error)
	InsertComment(ctx context.Context, blogID, userID, parentID int64, content string) (int64, error)
	InsertFollow(ctx context.Context, userID, followUserID int64) error
	DeleteFollow(ctx context.Context, userID, followUserID int64) error
	FollowerIDs(ctx context.Context, authorID int64) ([]int64, error)
	ShopExists(ctx context.Context, shopID int64) (bool, error)
	UserExists(ctx context.Context, userID int64) (bool, error)
}

const maxCommentContent = 500

// Writer 负责写路径：发笔记、点赞、评论、关注。
type Writer struct {
	repo WriteReader
	rdb  *redis.Client
}

func NewWriter(repo WriteReader, rdb *redis.Client) *Writer {
	return &Writer{repo: repo, rdb: rdb}
}

// SaveBlog 发布笔记，并把笔记 id 推进每个粉丝的收件箱 ZSet。
func (w *Writer) SaveBlog(ctx context.Context, userID, shopID int64, title, images, content string) (int64, error) {
	if userID == 0 {
		return 0, errs.ErrUnauthorized
	}
	title = strings.TrimSpace(title)
	content = strings.TrimSpace(content)
	if shopID <= 0 || title == "" || content == "" {
		return 0, errs.New(1000, 200, "笔记内容不能为空")
	}
	// 校验店铺存在，避免笔记指向不存在的店铺。
	shopExists, err := w.repo.ShopExists(ctx, shopID)
	if err != nil {
		return 0, err
	}
	if !shopExists {
		return 0, errs.New(1003, 200, "店铺不存在！")
	}
	id, err := w.repo.InsertBlog(ctx, shopID, userID, title, images, content)
	if err != nil {
		return 0, err
	}
	followers, err := w.repo.FollowerIDs(ctx, userID)
	if err != nil {
		return 0, err
	}
	score := float64(time.Now().UnixMilli())
	for _, follower := range followers {
		_ = w.rdb.ZAdd(ctx, "feed:"+strconv.FormatInt(follower, 10),
			redis.Z{Score: score, Member: strconv.FormatInt(id, 10)}).Err()
	}
	return id, nil
}

// LikeBlog 点赞/取消：先看 ZSet 里有没有当前用户，再同步数据库计数。
func (w *Writer) LikeBlog(ctx context.Context, userID, blogID int64) error {
	if userID == 0 {
		return errs.ErrUnauthorized
	}
	exists, err := w.repo.BlogExists(ctx, blogID)
	if err != nil {
		return err
	}
	if !exists {
		return errs.New(1003, 200, "笔记不存在！")
	}
	key := "blog:liked:" + strconv.FormatInt(blogID, 10)
	member := strconv.FormatInt(userID, 10)
	_, err = w.rdb.ZScore(ctx, key, member).Result()
	if err == redis.Nil {
		if affected, _ := w.repo.AddBlogLiked(ctx, blogID, 1); affected {
			_ = w.rdb.ZAdd(ctx, key, redis.Z{Score: float64(time.Now().UnixMilli()), Member: member}).Err()
		}
		return nil
	}
	if err != nil {
		return err
	}
	if affected, _ := w.repo.AddBlogLiked(ctx, blogID, -1); affected {
		_ = w.rdb.ZRem(ctx, key, member).Err()
	}
	return nil
}

func (w *Writer) AddComment(ctx context.Context, userID, blogID, parentID int64, content string) (int64, error) {
	if userID == 0 {
		return 0, errs.New(1000, 401, "请先登录")
	}
	content = strings.TrimSpace(content)
	if content == "" {
		return 0, errs.New(1000, 200, "评论内容不能为空")
	}
	if len([]rune(content)) > maxCommentContent {
		return 0, errs.New(1000, 200, fmt.Sprintf("评论最多 %d 字", maxCommentContent))
	}
	exists, err := w.repo.BlogExists(ctx, blogID)
	if err != nil {
		return 0, err
	}
	if !exists {
		return 0, errs.New(1003, 200, "笔记不存在！")
	}
	return w.repo.InsertComment(ctx, blogID, userID, parentID, content)
}

// Follow 关注/取关：数据库为准，Redis Set 只做旁路。
func (w *Writer) Follow(ctx context.Context, userID, targetID int64, follow bool) error {
	if userID == 0 {
		return errs.ErrUnauthorized
	}
	if targetID <= 0 || targetID == userID {
		return errs.New(1000, 200, "关注的用户不合法")
	}
	// 关注前校验目标用户存在，避免留下无效关注关系。
	targetExists, err := w.repo.UserExists(ctx, targetID)
	if err != nil {
		return err
	}
	if !targetExists {
		return errs.New(1003, 200, "用户不存在！")
	}
	key := "follows:" + strconv.FormatInt(userID, 10)
	if follow {
		if err := w.repo.InsertFollow(ctx, userID, targetID); err != nil {
			return err
		}
		_ = w.rdb.SAdd(ctx, key, strconv.FormatInt(targetID, 10)).Err()
		return nil
	}
	if err := w.repo.DeleteFollow(ctx, userID, targetID); err != nil {
		return err
	}
	_ = w.rdb.SRem(ctx, key, strconv.FormatInt(targetID, 10)).Err()
	return nil
}
