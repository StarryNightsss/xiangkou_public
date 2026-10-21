package domain

import (
	"encoding/json"
	"time"
)

const localTimeLayout = "2006-01-02T15:04:05"

// marshalShop 显式列出字段，保证与前端契约的 JSON 键一致。
func marshalShop(s Shop) ([]byte, error) {
	return json.Marshal(struct {
		ID         int64  `json:"id"`
		Name       string `json:"name"`
		TypeID     int64  `json:"typeId"`
		Images     string `json:"images,omitempty"`
		Area       string `json:"area,omitempty"`
		Address    string `json:"address,omitempty"`
		X          float64 `json:"x"`
		Y          float64 `json:"y"`
		AvgPrice   int64  `json:"avgPrice"`
		Sold       int    `json:"sold"`
		Comments   int    `json:"comments"`
		Score      int    `json:"score"`
		OpenHours  string `json:"openHours,omitempty"`
		CreateTime string `json:"createTime"`
		UpdateTime string `json:"updateTime"`
	}{
		ID: s.ID, Name: s.Name, TypeID: s.TypeID, Images: s.Images, Area: s.Area, Address: s.Address,
		X: s.X, Y: s.Y, AvgPrice: s.AvgPrice, Sold: s.Sold, Comments: s.Comments, Score: s.Score,
		OpenHours: s.OpenHours, CreateTime: formatTime(s.CreateTime), UpdateTime: formatTime(s.UpdateTime),
	})
}

// marshalWithTimes 处理其余实体的时间格式。
func marshalWithTimes(value any) ([]byte, error) {
	switch typed := value.(type) {
	case ShopType:
		return json.Marshal(struct {
			ID   int64  `json:"id"`
			Name string `json:"name"`
			Icon string `json:"icon"`
			Sort int    `json:"sort"`
		}{typed.ID, typed.Name, typed.Icon, typed.Sort})
	default:
		return json.Marshal(value)
	}
}

func formatTime(value time.Time) string {
	if value.IsZero() {
		return ""
	}
	return value.Format(localTimeLayout)
}
