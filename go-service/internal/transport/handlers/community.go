package handlers

import (
	"net/http"
	"strconv"
	"strings"

	"xiangkou/go-service/internal/app"
	"xiangkou/go-service/internal/platform/errs"
	"xiangkou/go-service/internal/platform/httpx"
)

type BlogHandler struct {
	community *app.Community
	writer    *app.Writer
}

func NewBlogHandler(community *app.Community, writer *app.Writer) *BlogHandler {
	return &BlogHandler{community: community, writer: writer}
}

func (h *BlogHandler) Hot(w http.ResponseWriter, r *http.Request) {
	page := parseIntDefault(r.URL.Query().Get("current"), 1)
	blogs, err := h.community.HotBlogs(r.Context(), page, httpx.CurrentUser(r.Context()))
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, blogs)
}

func (h *BlogHandler) Detail(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	blog, err := h.community.Blog(r.Context(), id, httpx.CurrentUser(r.Context()))
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, blog)
}

func (h *BlogHandler) ByShop(w http.ResponseWriter, r *http.Request) {
	shopID, err := strconv.ParseInt(r.URL.Query().Get("shopId"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	page := parseIntDefault(r.URL.Query().Get("current"), 1)
	blogs, total, err := h.community.BlogsByShop(r.Context(), shopID, page)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OKList(w, blogs, total)
}

func (h *BlogHandler) ByUser(w http.ResponseWriter, r *http.Request) {
	userID, err := strconv.ParseInt(r.URL.Query().Get("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	page := parseIntDefault(r.URL.Query().Get("current"), 1)
	blogs, err := h.community.BlogsByUser(r.Context(), userID, page)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, blogs)
}

func (h *BlogHandler) Search(w http.ResponseWriter, r *http.Request) {
	shopID, err := strconv.ParseInt(r.URL.Query().Get("shopId"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	blogs, total, err := h.community.SearchBlogs(r.Context(), shopID, r.URL.Query().Get("keyword"), parseIntDefault(r.URL.Query().Get("current"), 1))
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OKList(w, blogs, total)
}

func (h *BlogHandler) Comments(w http.ResponseWriter, r *http.Request) {
	blogID, err := strconv.ParseInt(r.PathValue("blogId"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	comments, err := h.community.Comments(r.Context(), blogID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, comments)
}

type FollowHandler struct {
	community *app.Community
	writer    *app.Writer
}

func NewFollowHandler(community *app.Community, writer *app.Writer) *FollowHandler {
	return &FollowHandler{community: community, writer: writer}
}

func (h *FollowHandler) IsFollow(w http.ResponseWriter, r *http.Request) {
	authorID, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	following, err := h.community.IsFollowing(r.Context(), httpx.CurrentUser(r.Context()), authorID)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, following)
}

// Batch 批量取店铺详情，参数 ids=1,2,3。
func (h *ShopHandler) Batch(w http.ResponseWriter, r *http.Request) {
	raw := strings.TrimSpace(r.URL.Query().Get("ids"))
	if raw == "" {
		httpx.Fail(w, errs.New(1000, 200, "ids 不能为空"))
		return
	}
	parts := strings.Split(raw, ",")
	ids := make([]int64, 0, len(parts))
	for _, part := range parts {
		id, err := strconv.ParseInt(strings.TrimSpace(part), 10, 64)
		if err != nil || id <= 0 {
			httpx.Fail(w, errs.New(1000, 200, "ids 必须是正整数"))
			return
		}
		ids = append(ids, id)
	}
	if len(ids) > 40 {
		httpx.Fail(w, errs.New(1000, 200, "ids 最多 40 个"))
		return
	}
	shops, err := h.catalog.Batch(r.Context(), ids)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, shops)
}

// Catalog 对应 /shop/catalog，默认 size=12、最大 20。
func (h *ShopHandler) Catalog(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	page := parseIntDefault(query.Get("page"), 1)
	size := parseIntDefault(query.Get("size"), 12)
	sort := query.Get("sort")
	if sort == "" {
		sort = "score"
	}
	var typeID *int64
	if raw := query.Get("typeId"); raw != "" {
		value, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			httpx.Fail(w, errs.New(1000, 200, "店铺查询参数无效"))
			return
		}
		typeID = &value
	}
	shops, total, err := h.catalog.Catalog(r.Context(), strings.TrimSpace(query.Get("keyword")), typeID, page, size, sort)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OKList(w, shops, total)
}
