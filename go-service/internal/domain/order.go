package domain

import (
	"encoding/json"
	"time"
)

type Order struct {
	ID         int64
	UserID     int64
	VoucherID  int64
	PayType    int
	Status     int
	CreateTime time.Time
	PayTime    *time.Time
	UseTime    *time.Time
	RefundTime *time.Time
	UpdateTime time.Time
}

// VoucherBrief 订单相关的券信息（下单校验与列表展示）。
type VoucherBrief struct {
	ID          int64
	ShopID      int64
	Title       string
	Rules       string
	PayValue    int64
	ActualValue int64
	Type        int
	Status      int
}

func (o Order) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		ID         int64   `json:"id"`
		UserID     int64   `json:"userId"`
		VoucherID  int64   `json:"voucherId"`
		PayType    int     `json:"payType"`
		Status     int     `json:"status"`
		CreateTime string  `json:"createTime"`
		PayTime    *string `json:"payTime,omitempty"`
		UseTime    *string `json:"useTime,omitempty"`
		RefundTime *string `json:"refundTime,omitempty"`
		UpdateTime string  `json:"updateTime"`
	}{
		ID: o.ID, UserID: o.UserID, VoucherID: o.VoucherID, PayType: o.PayType, Status: o.Status,
		CreateTime: formatTime(o.CreateTime), PayTime: formatOptionalTime(o.PayTime),
		UseTime: formatOptionalTime(o.UseTime), RefundTime: formatOptionalTime(o.RefundTime),
		UpdateTime: formatTime(o.UpdateTime),
	})
}

func formatOptionalTime(value *time.Time) *string {
	if value == nil {
		return nil
	}
	text := value.Format(localTimeLayout)
	return &text
}
