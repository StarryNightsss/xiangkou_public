// Package domain 只放业务实体与规则，不依赖 HTTP / 数据库 / Redis。
package domain

import (
	"encoding/json"
	"time"
)

type Shop struct {
	ID         int64
	Name       string
	TypeID     int64
	Images     string
	Area       string
	Address    string
	X          float64
	Y          float64
	AvgPrice   int64
	Sold       int
	Comments   int
	Score      int
	OpenHours  string
	CreateTime time.Time
	UpdateTime time.Time
}

// MarshalJSON 与既有接口的 Shop 序列化保持一致。
func (s Shop) MarshalJSON() ([]byte, error) {
	return marshalShop(s)
}

type ShopType struct {
	ID   int64
	Name string
	Icon string
	Sort int
}

func (t ShopType) MarshalJSON() ([]byte, error) {
	return marshalWithTimes(t)
}

// UnmarshalJSON 兼容缓存里的时间格式（2006-01-02T15:04:05），否则命中缓存会解析失败。
func (s *Shop) UnmarshalJSON(data []byte) error {
	var raw struct {
		ID         int64  `json:"id"`
		Name       string `json:"name"`
		TypeID     int64  `json:"typeId"`
		Images     string `json:"images"`
		Area       string `json:"area"`
		Address    string `json:"address"`
		X          float64 `json:"x"`
		Y          float64 `json:"y"`
		AvgPrice   int64  `json:"avgPrice"`
		Sold       int    `json:"sold"`
		Comments   int    `json:"comments"`
		Score      int    `json:"score"`
		OpenHours  string `json:"openHours"`
		CreateTime string `json:"createTime"`
		UpdateTime string `json:"updateTime"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	s.ID, s.Name, s.TypeID, s.Images = raw.ID, raw.Name, raw.TypeID, raw.Images
	s.Area, s.Address, s.X, s.Y = raw.Area, raw.Address, raw.X, raw.Y
	s.AvgPrice, s.Sold, s.Comments, s.Score = raw.AvgPrice, raw.Sold, raw.Comments, raw.Score
	s.OpenHours = raw.OpenHours
	s.CreateTime = parseLocalTime(raw.CreateTime)
	s.UpdateTime = parseLocalTime(raw.UpdateTime)
	return nil
}

func parseLocalTime(text string) time.Time {
	if text == "" {
		return time.Time{}
	}
	for _, layout := range []string{localTimeLayout, time.RFC3339} {
		if value, err := time.ParseInLocation(layout, text, time.Local); err == nil {
			return value
		}
	}
	return time.Time{}
}
