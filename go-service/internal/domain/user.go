package domain

import (
	"encoding/json"
	"time"
)

type User struct {
	ID       int64
	Email    string
	NickName string
	Icon     string
}

// UserDTO 与 既有服务 一致，只暴露 id/nickName/icon。
type UserDTO struct {
	ID       int64  `json:"id"`
	NickName string `json:"nickName"`
	Icon     string `json:"icon"`
}

type UserInfo struct {
	UserID    int64
	City      *string
	Introduce *string
	Fans      int
	Followee  int
	Gender    *bool
	Birthday  *time.Time
	Credits   int
	Level     *bool
}

func (u UserInfo) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		UserID    int64   `json:"userId"`
		City      *string `json:"city,omitempty"`
		Introduce *string `json:"introduce,omitempty"`
		Fans      int     `json:"fans"`
		Followee  int     `json:"followee"`
		Gender    *bool   `json:"gender,omitempty"`
		Birthday  *string `json:"birthday,omitempty"`
		Credits   int     `json:"credits"`
		Level     *bool   `json:"level,omitempty"`
	}{
		UserID: u.UserID, City: u.City, Introduce: u.Introduce, Fans: u.Fans, Followee: u.Followee,
		Gender: u.Gender, Birthday: formatDate(u.Birthday), Credits: u.Credits, Level: u.Level,
	})
}

func formatDate(value *time.Time) *string {
	if value == nil {
		return nil
	}
	text := value.Format("2006-01-02")
	return &text
}
