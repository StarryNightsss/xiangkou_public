package domain

import "time"

// Voucher 与既有接口的 Voucher 对齐：type=0 普通券、type=1 秒杀券（带库存与时段）。
type Voucher struct {
	ID          int64      `json:"id"`
	ShopID      int64      `json:"shopId"`
	Title       string     `json:"title"`
	SubTitle    string     `json:"subTitle"`
	Rules       string     `json:"rules"`
	PayValue    int64      `json:"payValue"`
	ActualValue int64      `json:"actualValue"`
	Type        int        `json:"type"`
	Stock       *int       `json:"stock,omitempty"`
	BeginTime   *time.Time `json:"-"`
	EndTime     *time.Time `json:"-"`
	Orderable   bool       `json:"orderable"`
}

// MarshalJSON 输出与 既有服务 一致：秒杀券才有 stock/beginTime/endTime，普通券不出现这些键。
func (v Voucher) MarshalJSON() ([]byte, error) {
	type alias struct {
		ID          int64   `json:"id"`
		ShopID      int64   `json:"shopId"`
		Title       string  `json:"title"`
		SubTitle    string  `json:"subTitle"`
		Rules       string  `json:"rules"`
		PayValue    int64   `json:"payValue"`
		ActualValue int64   `json:"actualValue"`
		Type        int     `json:"type"`
		Stock       *int    `json:"stock,omitempty"`
		BeginTime   *string `json:"beginTime,omitempty"`
		EndTime     *string `json:"endTime,omitempty"`
		Orderable   bool    `json:"orderable"`
	}
	item := alias{ID: v.ID, ShopID: v.ShopID, Title: v.Title, SubTitle: v.SubTitle, Rules: v.Rules,
		PayValue: v.PayValue, ActualValue: v.ActualValue, Type: v.Type, Stock: v.Stock, Orderable: v.Orderable}
	if v.BeginTime != nil {
		text := v.BeginTime.Format(localTimeLayout)
		item.BeginTime = &text
	}
	if v.EndTime != nil {
		text := v.EndTime.Format(localTimeLayout)
		item.EndTime = &text
	}
	return marshalWithTimes(item)
}
