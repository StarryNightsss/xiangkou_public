package handlers

import (
	"net/http"
	"strconv"

	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/platform/errs"
	"xiangkou/go-service/internal/platform/httpx"
)

func parseOptionalInt(r *http.Request, key string) (*int64, bool) {
	raw := r.URL.Query().Get(key)
	if raw == "" {
		// 参数缺席时返回 ok=false，调用方据此跳过赋值；返回 (nil, true) 会导致解引用 panic。
		return nil, false
	}
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return nil, false
	}
	return &value, true
}

func (h *ShopHandler) Search(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	typeID, err := strconv.ParseInt(query.Get("typeId"), 10, 64)
	if err != nil {
		typeID = 0
	}
	criteria := domain.SearchCriteria{
		TypeID:  typeID,
		Keyword: query.Get("keyword"),
		Area:    query.Get("area"),
		Sort:    query.Get("sort"),
		Page:    parseIntDefault(query.Get("page"), 1),
		Size:    parseIntDefault(query.Get("size"), 10),
		HasVoucher: query.Get("hasVoucher") == "true",
	}
	if value, ok := parseOptionalInt(r, "maxPrice"); ok {
		criteria.MaxPrice = value
	}
	if value, ok := parseOptionalInt(r, "minScore"); ok {
		score := int(*value)
		criteria.MinScore = &score
	}
	shops, total, err := h.catalog.Search(r.Context(), criteria)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OKList(w, shops, total)
}

func (h *ShopHandler) ListVouchers(w http.ResponseWriter, r *http.Request) {
	shopID, err := strconv.ParseInt(r.PathValue("shopId"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	vouchers, err := h.catalog.ListVouchers(r.Context(), shopID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, vouchers)
}

func parseIntDefault(raw string, fallback int) int {
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return value
}
