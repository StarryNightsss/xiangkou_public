package handlers

import (
	"encoding/json"
	"net/http"
	"strconv"

	"xiangkou/go-service/internal/app"
	"xiangkou/go-service/internal/platform/errs"
	"xiangkou/go-service/internal/platform/httpx"
)

type VoucherAdminHandler struct {
	admin *app.VoucherAdmin
}

func NewVoucherAdminHandler(admin *app.VoucherAdmin) *VoucherAdminHandler {
	return &VoucherAdminHandler{admin: admin}
}

func (h *VoucherAdminHandler) Capabilities(w http.ResponseWriter, r *http.Request) {
	httpx.OK(w, h.admin.Capabilities(httpx.CurrentUser(r.Context())))
}

func (h *VoucherAdminHandler) Create(w http.ResponseWriter, r *http.Request) {
	var req app.CreateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	id, err := h.admin.Create(r.Context(), httpx.CurrentUser(r.Context()), req, false)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, id)
}

func (h *VoucherAdminHandler) CreateSeckill(w http.ResponseWriter, r *http.Request) {
	var req app.CreateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	id, err := h.admin.Create(r.Context(), httpx.CurrentUser(r.Context()), req, true)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, id)
}

func (h *VoucherAdminHandler) Delete(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	if err := h.admin.Delete(r.Context(), httpx.CurrentUser(r.Context()), id); err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, nil)
}
