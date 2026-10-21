package domain

// SearchCriteria 对应店铺检索条件：评分是 0~50 的整数（页面显示时除以 10）。
type SearchCriteria struct {
	TypeID     int64
	Keyword    string
	Area       string
	MaxPrice   *int64
	MinScore   *int
	HasVoucher bool
	Sort       string
	Page       int
	Size       int
}
