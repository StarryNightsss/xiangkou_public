package handlers

import (
	"net/http"
	"strconv"
	"strings"

	"xiangkou/go-service/internal/app"
	"xiangkou/go-service/internal/platform/errs"
	"xiangkou/go-service/internal/platform/httpx"
)

type OrderHandler struct {
	orders *app.Orders
}

func NewOrderHandler(orders *app.Orders) *OrderHandler {
	return &OrderHandler{orders: orders}
}

func pathInt64(r *http.Request, name string) (int64, error) {
	value, err := strconv.ParseInt(r.PathValue(name), 10, 64)
	if err != nil {
		return 0, errs.ErrBadRequest
	}
	return value, nil
}

// Route 统一处理 /voucher-order/* 的 POST。
// Go 1.22 不允许 "seckill/{id}" 与 "{id}/cancel" 同时注册，所以在同一入口按路径段分派。
func (h *OrderHandler) Route(w http.ResponseWriter, r *http.Request) {
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/voucher-order"), "/")
	parts := strings.Split(path, "/")
	switch {
	case len(parts) == 2 && parts[0] == "seckill":
		if id, err := strconv.ParseInt(parts[1], 10, 64); err == nil {
			h.seckill(w, r, id)
			return
		}
	case len(parts) == 2 && parts[0] == "ordinary":
		if id, err := strconv.ParseInt(parts[1], 10, 64); err == nil {
			h.ordinary(w, r, id)
			return
		}
	case len(parts) == 2 && parts[1] == "cancel":
		if id, err := strconv.ParseInt(parts[0], 10, 64); err == nil {
			h.cancel(w, r, id)
			return
		}
	case len(parts) == 2 && parts[1] == "complete":
		if id, err := strconv.ParseInt(parts[0], 10, 64); err == nil {
			h.complete(w, r, id)
			return
		}
	case len(parts) == 3 && parts[1] == "pay" && parts[2] == "mock":
		if id, err := strconv.ParseInt(parts[0], 10, 64); err == nil {
			h.payMock(w, r, id)
			return
		}
	}
	httpx.Fail(w, errs.ErrNotFound)
}

func (h *OrderHandler) seckill(w http.ResponseWriter, r *http.Request, voucherID int64) {
	orderID, err := h.orders.Seckill(r.Context(), httpx.CurrentUser(r.Context()), voucherID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, orderID)
}

func (h *OrderHandler) ordinary(w http.ResponseWriter, r *http.Request, voucherID int64) {
	orderID, err := h.orders.OrderOrdinary(r.Context(), httpx.CurrentUser(r.Context()), voucherID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, orderID)
}

func (h *OrderHandler) payMock(w http.ResponseWriter, r *http.Request, orderID int64) {
	// 旧接口的 @RequestParam 同时接受 query 与 form；Python 客户端用 form 提交。
	_ = r.ParseForm()
	raw := r.PostFormValue("expectedPayCents")
	if raw == "" {
		raw = r.URL.Query().Get("expectedPayCents")
	}
	expected, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		httpx.Fail(w, errs.New(1000, 200, "支付金额不匹配，请重新查询"))
		return
	}
	paid, err := h.orders.PayMock(r.Context(), httpx.CurrentUser(r.Context()), orderID, expected)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, paid)
}

func (h *OrderHandler) cancel(w http.ResponseWriter, r *http.Request, orderID int64) {
	value, err := h.orders.Cancel(r.Context(), httpx.CurrentUser(r.Context()), orderID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, value)
}

func (h *OrderHandler) complete(w http.ResponseWriter, r *http.Request, orderID int64) {
	value, err := h.orders.Complete(r.Context(), httpx.CurrentUser(r.Context()), orderID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, value)
}

func (h *OrderHandler) Detail(w http.ResponseWriter, r *http.Request) {
	orderID, err := pathInt64(r, "id")
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	detail, err := h.orders.Detail(r.Context(), httpx.CurrentUser(r.Context()), orderID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, detail)
}

func (h *OrderHandler) Mine(w http.ResponseWriter, r *http.Request) {
	page := parseIntDefault(r.URL.Query().Get("page"), 1)
	items, total, err := h.orders.MyOrders(r.Context(), httpx.CurrentUser(r.Context()), page, false)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OKList(w, items, total)
}

func (h *OrderHandler) MineVouchers(w http.ResponseWriter, r *http.Request) {
	page := parseIntDefault(r.URL.Query().Get("page"), 1)
	items, total, err := h.orders.MyOrders(r.Context(), httpx.CurrentUser(r.Context()), page, true)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OKList(w, items, total)
}

// OfVoucher 查询当前用户在某张券上的最近一单。
func (h *OrderHandler) OfVoucher(w http.ResponseWriter, r *http.Request) {
	voucherID, err := pathInt64(r, "id")
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	order, err := h.orders.MyVoucherOrder(r.Context(), httpx.CurrentUser(r.Context()), voucherID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	if order == nil {
		httpx.OK(w, nil)
		return
	}
	httpx.OK(w, order)
}
