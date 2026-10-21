// Package mysql 提供显式的 SQL 访问；单表查询直接用 SQL，复杂查询后续可换 sqlc。
package mysql

import (
	"context"
	"database/sql"
	"fmt"

	_ "github.com/go-sql-driver/mysql"

	"xiangkou/go-service/internal/domain"
)

type DB struct {
	*sql.DB
}

func Open(dsn string) (*DB, error) {
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(20)
	db.SetMaxIdleConns(10)
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return &DB{db}, nil
}

type ShopRepo struct {
	db *DB
}

func NewShopRepo(db *DB) *ShopRepo { return &ShopRepo{db: db} }

func (r *ShopRepo) Get(ctx context.Context, id int64) (*domain.Shop, error) {
	const query = `SELECT id, name, type_id, COALESCE(images,''), COALESCE(area,''), COALESCE(address,''),
	                      COALESCE(x,0), COALESCE(y,0), COALESCE(avg_price,0), COALESCE(sold,0),
	                      COALESCE(comments,0), COALESCE(score,0), COALESCE(open_hours,''),
	                      create_time, update_time
	               FROM tb_shop WHERE id = ?`
	var shop domain.Shop
	err := r.db.QueryRowContext(ctx, query, id).Scan(
		&shop.ID, &shop.Name, &shop.TypeID, &shop.Images, &shop.Area, &shop.Address,
		&shop.X, &shop.Y, &shop.AvgPrice, &shop.Sold, &shop.Comments, &shop.Score,
		&shop.OpenHours, &shop.CreateTime, &shop.UpdateTime,
	)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("query shop %d: %w", id, err)
	}
	return &shop, nil
}

func (r *ShopRepo) ListTypes(ctx context.Context) ([]domain.ShopType, error) {
	const query = `SELECT id, name, icon, sort FROM tb_shop_type ORDER BY sort ASC, id ASC`
	rows, err := r.db.QueryContext(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("query shop types: %w", err)
	}
	defer rows.Close()
	types := make([]domain.ShopType, 0, 12)
	for rows.Next() {
		var item domain.ShopType
		if err := rows.Scan(&item.ID, &item.Name, &item.Icon, &item.Sort); err != nil {
			return nil, err
		}
		types = append(types, item)
	}
	return types, rows.Err()
}

func (r *ShopRepo) ListAreas(ctx context.Context) ([]string, error) {
	const query = `SELECT DISTINCT area FROM tb_shop WHERE area <> '' ORDER BY area ASC`
	rows, err := r.db.QueryContext(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("query areas: %w", err)
	}
	defer rows.Close()
	areas := make([]string, 0, 16)
	for rows.Next() {
		var area string
		if err := rows.Scan(&area); err != nil {
			return nil, err
		}
		areas = append(areas, area)
	}
	return areas, rows.Err()
}

// ShopExists 发笔记前校验店铺存在，避免产生指向不存在店铺的笔记。
func (r *ShopRepo) ShopExists(ctx context.Context, id int64) (bool, error) {
	var exists int
	err := r.db.QueryRowContext(ctx, "SELECT 1 FROM tb_shop WHERE id = ? LIMIT 1", id).Scan(&exists)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}
