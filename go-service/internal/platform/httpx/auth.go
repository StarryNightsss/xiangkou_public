package httpx

import (
	"context"
	"net/http"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/platform/errs"
)

type userKey struct{}

// CurrentUser 从上下文取当前登录用户 id；未登录为 0。
func CurrentUser(ctx context.Context) int64 {
	value, _ := ctx.Value(userKey{}).(int64)
	return value
}

// Auth 可选登录：带 token 就解析并续期，不带也放行；需要强制的接口用 RequireUser 包一层。
func Auth(rdb *redis.Client) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			token := r.Header.Get("authorization")
			if token == "" {
				if cookie, err := r.Cookie("xiangkou_dp_token"); err == nil {
					token = cookie.Value
				}
			}
			if token == "" {
				next.ServeHTTP(w, r)
				return
			}
			key := "login:token:" + token
			values, err := rdb.HGetAll(r.Context(), key).Result()
			if err != nil || len(values) == 0 {
				next.ServeHTTP(w, r)
				return
			}
			// 与 既有服务 一致：每次请求滑动续期 30 分钟。
			_ = rdb.Expire(r.Context(), key, 30*60e9).Err()
			var userID int64
			if raw := values["id"]; raw != "" {
				userID = parseInt64(raw)
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), userKey{}, userID)))
		})
	}
}

// RequireUser 需要登录的接口：未登录直接 401，行为与既有接口的 LoginInterceptor 一致。
func RequireUser(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if CurrentUser(r.Context()) == 0 {
			Fail(w, errs.ErrUnauthorized)
			return
		}
		next(w, r)
	}
}

func parseInt64(raw string) int64 {
	var value int64
	for _, ch := range raw {
		if ch < '0' || ch > '9' {
			return 0
		}
		value = value*10 + int64(ch-'0')
	}
	return value
}
