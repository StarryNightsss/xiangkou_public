// Package handlers 只做协议转换：解析参数 → 调用用例 → 写统一信封。
package handlers

import (
	"net/http"
	"strconv"

	"xiangkou/go-service/internal/app"
	"xiangkou/go-service/internal/platform/errs"
	"xiangkou/go-service/internal/platform/httpx"
)

type ShopHandler struct {
	catalog *app.Catalog
}

func NewShopHandler(catalog *app.Catalog) *ShopHandler {
	return &ShopHandler{catalog: catalog}
}

func (h *ShopHandler) ListTypes(w http.ResponseWriter, r *http.Request) {
	types, err := h.catalog.ListTypes(r.Context())
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, types)
}

func (h *ShopHandler) ListAreas(w http.ResponseWriter, r *http.Request) {
	areas, err := h.catalog.ListAreas(r.Context())
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, areas)
}

func (h *ShopHandler) GetShop(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	shop, err := h.catalog.GetShop(r.Context(), id)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, shop)
}
