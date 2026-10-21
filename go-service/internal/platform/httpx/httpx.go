// Package httpx 定义对外协议：统一信封、请求 ID、错误渲染，与现有 既有服务 的 Result 保持一致。
package httpx

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"runtime/debug"

	"xiangkou/go-service/internal/platform/errs"
)

// Envelope 与既有接口的 Result 对齐：成功时只出现 success/data[/total]，失败时出现 errorMsg。
type Envelope struct {
	Success  bool `json:"success"`
	Data     any  `json:"data,omitempty"`
	ErrorMsg string `json:"errorMsg,omitempty"`
	Total    *int   `json:"total,omitempty"`
}

func WriteJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	// 用 Marshal 而不是 Encoder：Encoder 会多写一个换行，与 既有服务 响应字节不一致。
	body, err := json.Marshal(payload)
	if err != nil {
		_, _ = w.Write([]byte(`{"success":false,"errorMsg":"响应序列化失败"}`))
		return
	}
	_, _ = w.Write(body)
}

func OK(w http.ResponseWriter, data any) {
	WriteJSON(w, http.StatusOK, Envelope{Success: true, Data: data})
}

func OKList(w http.ResponseWriter, data any, total int64) {
	value := int(total)
	WriteJSON(w, http.StatusOK, Envelope{Success: true, Data: data, Total: &value})
}

func Fail(w http.ResponseWriter, err error) {
	var appErr *errs.AppError
	if !errors.As(err, &appErr) {
		appErr = errs.ErrInternal
		slog.Error("unexpected error", "err", err)
	}
	WriteJSON(w, appErr.HTTPCode, Envelope{Success: false, ErrorMsg: appErr.Message})
}

type ctxKey string

const requestIDKey ctxKey = "request_id"

func RequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-ID")
		if id == "" {
			buffer := make([]byte, 8)
			_, _ = rand.Read(buffer)
			id = hex.EncodeToString(buffer)
		}
		w.Header().Set("X-Request-ID", id)
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), requestIDKey, id)))
	})
}

func RequestIDFrom(ctx context.Context) string {
	value, _ := ctx.Value(requestIDKey).(string)
	return value
}

// Recover 把 panic 转成 500，并记录堆栈，避免单个请求把进程带走。
func Recover(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recovered := recover(); recovered != nil {
				slog.Error("panic recovered", "request_id", RequestIDFrom(r.Context()),
					"panic", recovered, "stack", string(debug.Stack()))
				Fail(w, errs.ErrInternal)
			}
		}()
		next.ServeHTTP(w, r)
	})
}
