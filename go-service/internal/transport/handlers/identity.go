package handlers

import (
	"encoding/json"
	"net/http"
	"strconv"

	"xiangkou/go-service/internal/app"
	"xiangkou/go-service/internal/platform/errs"
	"xiangkou/go-service/internal/platform/httpx"
)

type IdentityHandler struct {
	identity *app.Identity
}

func NewIdentityHandler(identity *app.Identity) *IdentityHandler {
	return &IdentityHandler{identity: identity}
}

func (h *IdentityHandler) SendCode(w http.ResponseWriter, r *http.Request) {
	// 既有服务 的 @RequestParam 同时接受 query 与 form；Python 客户端用的是 form。
	_ = r.ParseForm()
	email := r.PostFormValue("email")
	if email == "" {
		email = r.URL.Query().Get("email")
	}
	if err := h.identity.SendCode(r.Context(), email); err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, nil)
}

type loginForm struct {
	Email string `json:"email"`
	Code  string `json:"code"`
}

func (h *IdentityHandler) Login(w http.ResponseWriter, r *http.Request) {
	var form loginForm
	if err := json.NewDecoder(r.Body).Decode(&form); err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	token, err := h.identity.Login(r.Context(), form.Email, form.Code)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, token)
}

func (h *IdentityHandler) Logout(w http.ResponseWriter, r *http.Request) {
	token := r.Header.Get("authorization")
	if err := h.identity.Logout(r.Context(), token); err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, nil)
}

func (h *IdentityHandler) GuestLogin(w http.ResponseWriter, r *http.Request) {
	token, err := h.identity.GuestLogin(r.Context())
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, token)
}

func (h *IdentityHandler) Me(w http.ResponseWriter, r *http.Request) {
	user, err := h.identity.Me(r.Context(), httpx.CurrentUser(r.Context()))
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	if user == nil {
		httpx.OK(w, nil)
		return
	}
	httpx.OK(w, user)
}

func (h *IdentityHandler) UserByID(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	user, err := h.identity.UserByID(r.Context(), id)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	if user == nil {
		httpx.OK(w, nil)
		return
	}
	httpx.OK(w, user)
}

func (h *IdentityHandler) Info(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	info, err := h.identity.Info(r.Context(), id)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	if info == nil {
		httpx.OK(w, nil)
		return
	}
	httpx.OK(w, info)
}

func (h *IdentityHandler) Sign(w http.ResponseWriter, r *http.Request) {
	if err := h.identity.Sign(r.Context(), httpx.CurrentUser(r.Context())); err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, nil)
}

func (h *IdentityHandler) SignCount(w http.ResponseWriter, r *http.Request) {
	count, err := h.identity.SignCount(r.Context(), httpx.CurrentUser(r.Context()))
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, count)
}

// MyBlogs 当前用户发布的笔记。
func (h *BlogHandler) MyBlogs(w http.ResponseWriter, r *http.Request) {
	page := parseIntDefault(r.URL.Query().Get("current"), 1)
	blogs, err := h.community.MyBlogs(r.Context(), httpx.CurrentUser(r.Context()), page)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, blogs)
}

// Feed 关注流，返回 list + minTime + offset。
func (h *BlogHandler) Feed(w http.ResponseWriter, r *http.Request) {
	// 既有服务 的分页参数名是 lastId，不是 max。
	max, _ := strconv.ParseInt(r.URL.Query().Get("lastId"), 10, 64)
	offset := parseIntDefault(r.URL.Query().Get("offset"), 0)
	result, err := h.community.Feed(r.Context(), httpx.CurrentUser(r.Context()), max, offset)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	if result == nil {
		httpx.OK(w, nil)
		return
	}
	httpx.OK(w, result)
}
