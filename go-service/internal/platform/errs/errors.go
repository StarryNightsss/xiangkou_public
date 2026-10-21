// Package errs 统一错误载体：业务错误码 + 对外文案 + HTTP 状态，避免 handler 里散落字符串。
package errs

import "net/http"

type AppError struct {
	Code     int    `json:"code"`
	Message  string `json:"message"`
	HTTPCode int    `json:"-"`
}

func (e *AppError) Error() string { return e.Message }

func New(code int, httpCode int, message string) *AppError {
	return &AppError{Code: code, Message: message, HTTPCode: httpCode}
}

var (
	ErrNotFound     = New(1003, http.StatusNotFound, "资源不存在")
	ErrBadRequest   = New(1000, http.StatusBadRequest, "参数不合法")
	ErrUnauthorized = New(1001, http.StatusUnauthorized, "登录已失效")
	ErrInternal     = New(1500, http.StatusInternalServerError, "服务暂时不可用")
)
