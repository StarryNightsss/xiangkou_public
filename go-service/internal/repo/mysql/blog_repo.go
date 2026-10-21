package mysql

import (
	"context"
	"fmt"

	"xiangkou/go-service/internal/domain"
)

const blogColumns = `b.id, b.shop_id, b.user_id, b.title, COALESCE(b.images,''), COALESCE(b.content,''),
                     COALESCE(b.liked,0), COALESCE(b.comments,0), b.create_time, b.update_time`

func scanBlogRow(row interface{ Scan(...any) error }, withAuthor bool) (domain.Blog, error) {
	var blog domain.Blog
	var name, icon *string
	if withAuthor {
		err := row.Scan(&blog.ID, &blog.ShopID, &blog.UserID, &blog.Title, &blog.Images, &blog.Content,
			&blog.Liked, &blog.Comments, &blog.CreateTime, &blog.UpdateTime, &name, &icon)
		blog.Name, blog.Icon = name, icon
		return blog, err
	}
	err := row.Scan(&blog.ID, &blog.ShopID, &blog.UserID, &blog.Title, &blog.Images, &blog.Content,
		&blog.Liked, &blog.Comments, &blog.CreateTime, &blog.UpdateTime)
	return blog, err
}

func (r *ShopRepo) ListHotBlogs(ctx context.Context, page, size int) ([]domain.Blog, error) {
	query := `SELECT ` + blogColumns + `, u.nick_name, u.icon
	          FROM tb_blog b LEFT JOIN tb_user u ON u.id = b.user_id
	          ORDER BY b.liked DESC, b.id ASC LIMIT ? OFFSET ?`
	rows, err := r.db.QueryContext(ctx, query, size, (page-1)*size)
	if err != nil {
		return nil, fmt.Errorf("list hot blogs: %w", err)
	}
	defer rows.Close()
	blogs := make([]domain.Blog, 0, size)
	for rows.Next() {
		blog, err := scanBlogRow(rows, true)
		if err != nil {
			return nil, err
		}
		blogs = append(blogs, blog)
	}
	return blogs, rows.Err()
}

func (r *ShopRepo) GetBlog(ctx context.Context, id int64) (*domain.Blog, error) {
	query := `SELECT ` + blogColumns + `, u.nick_name, u.icon
	          FROM tb_blog b LEFT JOIN tb_user u ON u.id = b.user_id WHERE b.id = ?`
	row := r.db.QueryRowContext(ctx, query, id)
	blog, err := scanBlogRow(row, true)
	if err != nil {
		if isNoRows(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("get blog %d: %w", id, err)
	}
	return &blog, nil
}

func (r *ShopRepo) ListBlogsBy(ctx context.Context, column string, value int64, page, size int) ([]domain.Blog, int64, error) {
	query := `SELECT ` + blogColumns + ` FROM tb_blog b WHERE b.` + column + ` = ?
	          ORDER BY b.create_time DESC, b.id DESC LIMIT ? OFFSET ?`
	rows, err := r.db.QueryContext(ctx, query, value, size, (page-1)*size)
	if err != nil {
		return nil, 0, fmt.Errorf("list blogs by %s: %w", column, err)
	}
	defer rows.Close()
	blogs := make([]domain.Blog, 0, size)
	for rows.Next() {
		blog, err := scanBlogRow(rows, false)
		if err != nil {
			return nil, 0, err
		}
		blogs = append(blogs, blog)
	}
	var total int64
	if err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM tb_blog WHERE "+column+" = ?", value).Scan(&total); err != nil {
		return nil, 0, err
	}
	return blogs, total, rows.Err()
}

// SearchBlogs 按店铺 + 关键词（标题或正文）检索探店笔记。
func (r *ShopRepo) SearchBlogs(ctx context.Context, shopID int64, keyword string, current int) ([]domain.Blog, int64, error) {
	query := `SELECT ` + blogColumns + ` FROM tb_blog b
	          WHERE b.shop_id = ? AND (b.title LIKE ? OR b.content LIKE ?)
	          ORDER BY b.create_time DESC, b.id DESC LIMIT ? OFFSET ?`
	pattern := "%" + keyword + "%"
	rows, err := r.db.QueryContext(ctx, query, shopID, pattern, pattern, 5, (current-1)*5)
	if err != nil {
		return nil, 0, fmt.Errorf("search blogs: %w", err)
	}
	defer rows.Close()
	blogs := make([]domain.Blog, 0, 8)
	for rows.Next() {
		blog, err := scanBlogRow(rows, false)
		if err != nil {
			return nil, 0, err
		}
		blogs = append(blogs, blog)
	}
	var total int64
	if err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM tb_blog WHERE shop_id = ? AND (title LIKE ? OR content LIKE ?)",
		shopID, pattern, pattern).Scan(&total); err != nil {
		return nil, 0, err
	}
	return blogs, total, rows.Err()
}

func (r *ShopRepo) ListComments(ctx context.Context, blogID int64) ([]domain.Comment, error) {
	query := `SELECT c.id, c.user_id, c.blog_id, c.parent_id, c.content, COALESCE(c.liked,0),
	                 COALESCE(u.nick_name,''), COALESCE(u.icon,''), c.create_time
	          FROM tb_blog_comments c LEFT JOIN tb_user u ON u.id = c.user_id
	          WHERE c.blog_id = ? AND c.status <> 2
	          ORDER BY c.create_time DESC, c.id DESC`
	rows, err := r.db.QueryContext(ctx, query, blogID)
	if err != nil {
		return nil, fmt.Errorf("list comments: %w", err)
	}
	defer rows.Close()
	comments := make([]domain.Comment, 0, 8)
	for rows.Next() {
		var item domain.Comment
		if err := rows.Scan(&item.ID, &item.UserID, &item.BlogID, &item.ParentID, &item.Content,
			&item.Liked, &item.NickName, &item.Icon, &item.CreateTime); err != nil {
			return nil, err
		}
		comments = append(comments, item)
	}
	return comments, rows.Err()
}

func (r *ShopRepo) IsFollowing(ctx context.Context, userID, authorID int64) (bool, error) {
	var count int64
	err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM tb_follow WHERE user_id = ? AND follow_user_id = ?",
		userID, authorID).Scan(&count)
	if err != nil {
		return false, fmt.Errorf("check follow: %w", err)
	}
	return count > 0, nil
}

// BatchShops 按 id 批量取详情，最多 40 个，与既有接口的限制一致。
func (r *ShopRepo) BatchShops(ctx context.Context, ids []int64) ([]domain.Shop, error) {
	if len(ids) == 0 {
		return []domain.Shop{}, nil
	}
	if len(ids) > 40 {
		ids = ids[:40]
	}
	placeholders := ""
	args := make([]any, 0, len(ids))
	for index, id := range ids {
		if index > 0 {
			placeholders += ","
		}
		placeholders += "?"
		args = append(args, id)
	}
	rows, err := r.db.QueryContext(ctx, "SELECT "+shopColumns+" FROM tb_shop WHERE id IN ("+placeholders+")", args...)
	if err != nil {
		return nil, fmt.Errorf("batch shops: %w", err)
	}
	defer rows.Close()
	shops := make([]domain.Shop, 0, len(ids))
	for rows.Next() {
		shop, err := scanShop(rows)
		if err != nil {
			return nil, err
		}
		shops = append(shops, shop)
	}
	return shops, rows.Err()
}

func isNoRows(err error) bool {
	return err != nil && err.Error() == "sql: no rows in result set"
}
