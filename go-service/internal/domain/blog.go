package domain

import (
	"encoding/json"
	"time"
)

// Blog 覆盖两种返回形态：列表只带基础字段；热门/详情额外带作者与是否点赞。
type Blog struct {
	ID         int64
	ShopID     int64
	UserID     int64
	Title      string
	Images     string
	Content    string
	Liked      int
	Comments   int
	CreateTime time.Time
	UpdateTime time.Time
	Name       *string
	Icon       *string
	IsLike     *bool
}

func (b Blog) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		ID         int64   `json:"id"`
		ShopID     int64   `json:"shopId"`
		UserID     int64   `json:"userId"`
		Icon       *string `json:"icon,omitempty"`
		Name       *string `json:"name,omitempty"`
		IsLike     *bool   `json:"isLike,omitempty"`
		Title      string  `json:"title"`
		Images     string  `json:"images"`
		Content    string  `json:"content"`
		Liked      int     `json:"liked"`
		Comments   int     `json:"comments"`
		CreateTime string  `json:"createTime"`
		UpdateTime string  `json:"updateTime"`
	}{
		ID: b.ID, ShopID: b.ShopID, UserID: b.UserID, Icon: b.Icon, Name: b.Name, IsLike: b.IsLike,
		Title: b.Title, Images: b.Images, Content: b.Content, Liked: b.Liked, Comments: b.Comments,
		CreateTime: formatTime(b.CreateTime), UpdateTime: formatTime(b.UpdateTime),
	})
}

// Comment 与既有接口的评论列表一致：带作者昵称与头像。
type Comment struct {
	ID         int64
	UserID     int64
	BlogID     int64
	ParentID   int64
	Content    string
	Liked      int
	NickName   string
	Icon       string
	CreateTime time.Time
}

// MarshalJSON 与既有接口的评论 JSON 完全对齐（字段顺序也一致，便于逐字节比对）。
func (c Comment) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		CreateTime string `json:"createTime"`
		NickName   string `json:"nickName"`
		Icon       string `json:"icon"`
		ID         int64  `json:"id"`
		UserID     int64  `json:"userId"`
		ParentID   int64  `json:"parentId"`
		Content    string `json:"content"`
		Liked      int    `json:"liked"`
	}{
		CreateTime: formatTime(c.CreateTime), NickName: c.NickName, Icon: c.Icon,
		ID: c.ID, UserID: c.UserID, ParentID: c.ParentID, Content: c.Content, Liked: c.Liked,
	})
}
