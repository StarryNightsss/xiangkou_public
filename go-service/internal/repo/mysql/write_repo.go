package mysql

import (
	"context"
	"database/sql"
	"fmt"
)

// InsertBlog 写入探店笔记，返回自增 id。
func (r *ShopRepo) InsertBlog(ctx context.Context, shopID, userID int64, title, images, content string) (int64, error) {
	result, err := r.db.ExecContext(ctx,
		`INSERT INTO tb_blog (shop_id, user_id, title, images, content, liked, comments, create_time, update_time)
		 VALUES (?, ?, ?, ?, ?, 0, 0, NOW(), NOW())`,
		shopID, userID, title, images, content)
	if err != nil {
		return 0, fmt.Errorf("insert blog: %w", err)
	}
	return result.LastInsertId()
}

// AddBlogLiked 点赞数增减；返回是否命中一行。
func (r *ShopRepo) AddBlogLiked(ctx context.Context, blogID int64, delta int) (bool, error) {
	result, err := r.db.ExecContext(ctx, "UPDATE tb_blog SET liked = liked + ? WHERE id = ?", delta, blogID)
	if err != nil {
		return false, fmt.Errorf("update blog liked: %w", err)
	}
	affected, err := result.RowsAffected()
	return affected > 0, err
}

func (r *ShopRepo) BlogExists(ctx context.Context, blogID int64) (bool, error) {
	var count int
	err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM tb_blog WHERE id = ?", blogID).Scan(&count)
	return count > 0, err
}

// InsertComment 写评论并把笔记评论数 +1。
func (r *ShopRepo) InsertComment(ctx context.Context, blogID, userID, parentID int64, content string) (int64, error) {
	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback() }()
	result, err := tx.ExecContext(ctx,
		`INSERT INTO tb_blog_comments (user_id, blog_id, parent_id, answer_id, content, liked, status, create_time, update_time)
		 VALUES (?, ?, ?, 0, ?, 0, 0, NOW(), NOW())`, userID, blogID, parentID, content)
	if err != nil {
		return 0, fmt.Errorf("insert comment: %w", err)
	}
	if _, err := tx.ExecContext(ctx, "UPDATE tb_blog SET comments = COALESCE(comments,0) + 1 WHERE id = ?", blogID); err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return result.LastInsertId()
}

// InsertFollow 关注；重复关注靠唯一约束或先查后插保证幂等。
func (r *ShopRepo) InsertFollow(ctx context.Context, userID, followUserID int64) error {
	var count int
	if err := r.db.QueryRowContext(ctx,
		"SELECT COUNT(*) FROM tb_follow WHERE user_id = ? AND follow_user_id = ?", userID, followUserID).Scan(&count); err != nil {
		return err
	}
	if count > 0 {
		return nil
	}
	_, err := r.db.ExecContext(ctx,
		"INSERT INTO tb_follow (user_id, follow_user_id, create_time) VALUES (?, ?, NOW())", userID, followUserID)
	return err
}

func (r *ShopRepo) DeleteFollow(ctx context.Context, userID, followUserID int64) error {
	_, err := r.db.ExecContext(ctx,
		"DELETE FROM tb_follow WHERE user_id = ? AND follow_user_id = ?", userID, followUserID)
	return err
}

func (r *ShopRepo) FollowerIDs(ctx context.Context, authorID int64) ([]int64, error) {
	rows, err := r.db.QueryContext(ctx, "SELECT user_id FROM tb_follow WHERE follow_user_id = ?", authorID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := make([]int64, 0, 8)
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func (r *ShopRepo) BlogShopID(ctx context.Context, blogID int64) (sql.NullInt64, error) {
	var shopID sql.NullInt64
	err := r.db.QueryRowContext(ctx, "SELECT shop_id FROM tb_blog WHERE id = ?", blogID).Scan(&shopID)
	return shopID, err
}
