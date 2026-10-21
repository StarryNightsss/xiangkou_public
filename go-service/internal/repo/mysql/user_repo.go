package mysql

import (
	"context"
	"database/sql"
	"fmt"

	"xiangkou/go-service/internal/domain"
)

// FindUserByEmail 登录时按邮箱找用户；不存在返回 (nil, nil)。
func (r *ShopRepo) FindUserByEmail(ctx context.Context, email string) (*domain.User, error) {
	const query = `SELECT id, email, COALESCE(nick_name,''), COALESCE(icon,'') FROM tb_user WHERE email = ? LIMIT 1`
	var user domain.User
	err := r.db.QueryRowContext(ctx, query, email).Scan(&user.ID, &user.Email, &user.NickName, &user.Icon)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("find user: %w", err)
	}
	return &user, nil
}

func (r *ShopRepo) CreateUser(ctx context.Context, email, nickName string) (*domain.User, error) {
	result, err := r.db.ExecContext(ctx,
		"INSERT INTO tb_user (email, nick_name, icon, create_time, update_time) VALUES (?, ?, '', NOW(), NOW())",
		email, nickName)
	if err != nil {
		return nil, fmt.Errorf("create user: %w", err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		return nil, err
	}
	return &domain.User{ID: id, Email: email, NickName: nickName}, nil
}

func (r *ShopRepo) GetUser(ctx context.Context, id int64) (*domain.User, error) {
	const query = `SELECT id, email, COALESCE(nick_name,''), COALESCE(icon,'') FROM tb_user WHERE id = ?`
	var user domain.User
	err := r.db.QueryRowContext(ctx, query, id).Scan(&user.ID, &user.Email, &user.NickName, &user.Icon)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("get user: %w", err)
	}
	return &user, nil
}

func (r *ShopRepo) GetUserInfo(ctx context.Context, userID int64) (*domain.UserInfo, error) {
	const query = `SELECT user_id, city, introduce, COALESCE(fans,0), COALESCE(followee,0),
	                      gender, birthday, COALESCE(credits,0), level
	               FROM tb_user_info WHERE user_id = ?`
	var info domain.UserInfo
	var city, introduce sql.NullString
	var gender, level sql.NullBool
	var birthday sql.NullTime
	err := r.db.QueryRowContext(ctx, query, userID).Scan(&info.UserID, &city, &introduce,
		&info.Fans, &info.Followee, &gender, &birthday, &info.Credits, &level)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("get user info: %w", err)
	}
	if city.Valid {
		info.City = &city.String
	}
	if introduce.Valid {
		info.Introduce = &introduce.String
	}
	if gender.Valid {
		info.Gender = &gender.Bool
	}
	if level.Valid {
		info.Level = &level.Bool
	}
	if birthday.Valid {
		info.Birthday = &birthday.Time
	}
	return &info, nil
}

// ListBlogsByIDs 按传入顺序返回笔记，用于关注流（ZSet 已排好序）。
func (r *ShopRepo) ListBlogsByIDs(ctx context.Context, ids []int64) ([]domain.Blog, error) {
	if len(ids) == 0 {
		return []domain.Blog{}, nil
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
	query := `SELECT ` + blogColumns + `, u.nick_name, u.icon
	          FROM tb_blog b LEFT JOIN tb_user u ON u.id = b.user_id
	          WHERE b.id IN (` + placeholders + `)`
	rows, err := r.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("list blogs by ids: %w", err)
	}
	defer rows.Close()
	byID := make(map[int64]domain.Blog, len(ids))
	for rows.Next() {
		blog, err := scanBlogRow(rows, true)
		if err != nil {
			return nil, err
		}
		byID[blog.ID] = blog
	}
	ordered := make([]domain.Blog, 0, len(ids))
	for _, id := range ids {
		if blog, ok := byID[id]; ok {
			ordered = append(ordered, blog)
		}
	}
	return ordered, rows.Err()
}

// UserExists 关注前校验目标用户存在。
func (r *ShopRepo) UserExists(ctx context.Context, id int64) (bool, error) {
	var exists int
	err := r.db.QueryRowContext(ctx, "SELECT 1 FROM tb_user WHERE id = ? LIMIT 1", id).Scan(&exists)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}
