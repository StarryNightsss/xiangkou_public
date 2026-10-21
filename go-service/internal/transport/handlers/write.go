package handlers

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"xiangkou/go-service/internal/platform/errs"
	"xiangkou/go-service/internal/platform/httpx"
)

type blogForm struct {
	ShopID  int64  `json:"shopId"`
	Title   string `json:"title"`
	Images  string `json:"images"`
	Content string `json:"content"`
}

func (h *BlogHandler) Create(w http.ResponseWriter, r *http.Request) {
	var form blogForm
	if err := json.NewDecoder(r.Body).Decode(&form); err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	id, err := h.writer.SaveBlog(r.Context(), httpx.CurrentUser(r.Context()), form.ShopID, form.Title, form.Images, form.Content)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, id)
}

func (h *BlogHandler) Like(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	if err := h.writer.LikeBlog(r.Context(), httpx.CurrentUser(r.Context()), id); err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, nil)
}

type commentForm struct {
	BlogID   int64  `json:"blogId"`
	ParentID int64  `json:"parentId"`
	Content  string `json:"content"`
}

func (h *BlogHandler) AddComment(w http.ResponseWriter, r *http.Request) {
	var form commentForm
	if err := json.NewDecoder(r.Body).Decode(&form); err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	id, err := h.writer.AddComment(r.Context(), httpx.CurrentUser(r.Context()), form.BlogID, form.ParentID, form.Content)
	if err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, id)
}


func (h *FollowHandler) Follow(w http.ResponseWriter, r *http.Request) {
	targetID, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	follow, err := strconv.ParseBool(r.PathValue("isFollow"))
	if err != nil {
		httpx.Fail(w, errs.ErrBadRequest)
		return
	}
	if err := h.writer.Follow(r.Context(), httpx.CurrentUser(r.Context()), targetID, follow); err != nil {
		httpx.Fail(w, err)
		return
	}
	httpx.OK(w, nil)
}

type UploadHandler struct {
	dir string
}

func NewUploadHandler(dir string) *UploadHandler {
	if dir == "" {
		dir = "./uploads"
	}
	return &UploadHandler{dir: dir}
}

// Upload 保存图片到本地目录，返回相对路径（与既有接口的 fileName 语义一致）。
func (h *UploadHandler) Upload(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseMultipartForm(6 << 20); err != nil {
		httpx.Fail(w, errs.New(1000, 200, "图片类型或大小不合法"))
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		httpx.Fail(w, errs.New(1000, 200, "图片类型或大小不合法"))
		return
	}
	defer file.Close()
	name := strings.ToLower(header.Filename)
	ext := filepath.Ext(name)
	if header.Size > 5_000_000 || (ext != ".jpg" && ext != ".jpeg" && ext != ".png" && ext != ".webp") {
		httpx.Fail(w, errs.New(1000, 200, "图片类型或大小不合法"))
		return
	}
	now := time.Now()
	relative := filepath.Join("blogs", now.Format("2006"), now.Format("01"), now.Format("02"), randomName()+ext)
	target := filepath.Join(h.dir, relative)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		httpx.Fail(w, errs.ErrInternal)
		return
	}
	out, err := os.Create(target)
	if err != nil {
		httpx.Fail(w, errs.ErrInternal)
		return
	}
	defer out.Close()
	if _, err := io.Copy(out, file); err != nil {
		httpx.Fail(w, errs.ErrInternal)
		return
	}
	// 与前端 imageUrl 约定一致：返回 /blogs/... 形式，由静态目录补上 /imgs 前缀。
	httpx.OK(w, "/"+filepath.ToSlash(relative))
}

// Files 暴露上传目录：/uploads/* 与前端使用的 /imgs/* 都指向同一份文件。
func (h *UploadHandler) Files() http.Handler {
	return http.StripPrefix("/uploads/", http.FileServer(http.Dir(h.dir)))
}

func (h *UploadHandler) Images() http.Handler {
	return http.StripPrefix("/imgs/", http.FileServer(http.Dir(h.dir)))
}

func randomName() string {
	buffer := make([]byte, 16)
	_, _ = rand.Read(buffer)
	return hex.EncodeToString(buffer)
}
