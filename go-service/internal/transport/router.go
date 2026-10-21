// Package transport wires routes. Middleware order defines the security semantics.
package transport

import (
	"net/http"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/platform/httpx"
	"xiangkou/go-service/internal/transport/handlers"
)

// NewRouter registers the frozen endpoint list; literal routes take precedence over wildcards.
func NewRouter(shops *handlers.ShopHandler, blogs *handlers.BlogHandler,
	follows *handlers.FollowHandler, identity *handlers.IdentityHandler,
	upload *handlers.UploadHandler, orders *handlers.OrderHandler,
	vouchers *handlers.VoucherAdminHandler, rdb *redis.Client) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /shop-type/list", shops.ListTypes)
	mux.HandleFunc("GET /shop/areas", shops.ListAreas)
	mux.HandleFunc("GET /shop/search", shops.Search)
	mux.HandleFunc("GET /shop/catalog", shops.Catalog)
	mux.HandleFunc("GET /shop/batch", shops.Batch)
	mux.HandleFunc("GET /voucher/list/{shopId}", shops.ListVouchers)
	mux.HandleFunc("GET /shop/{id}", shops.GetShop)

	mux.HandleFunc("GET /blog/hot", blogs.Hot)
	mux.HandleFunc("GET /blog/of/shop", blogs.ByShop)
	mux.HandleFunc("GET /blog/of/user", blogs.ByUser)
	mux.HandleFunc("GET /blog/search", blogs.Search)
	mux.HandleFunc("GET /blog/{id}", blogs.Detail)
	mux.HandleFunc("GET /blog-comments/{blogId}", blogs.Comments)
	mux.HandleFunc("GET /blog/of/me", httpx.RequireUser(blogs.MyBlogs))
	mux.HandleFunc("GET /blog/of/follow", httpx.RequireUser(blogs.Feed))

	mux.HandleFunc("GET /follow/or/not/{id}", httpx.RequireUser(follows.IsFollow))

	mux.HandleFunc("POST /blog", httpx.RequireUser(blogs.Create))
	mux.HandleFunc("PUT /blog/like/{id}", httpx.RequireUser(blogs.Like))
	mux.HandleFunc("POST /blog-comments", httpx.RequireUser(blogs.AddComment))
	mux.HandleFunc("PUT /follow/{id}/{isFollow}", httpx.RequireUser(follows.Follow))
	mux.HandleFunc("POST /upload/blog", httpx.RequireUser(upload.Upload))

	mux.HandleFunc("POST /user/code", identity.SendCode)
	mux.HandleFunc("POST /user/login", identity.Login)
	mux.HandleFunc("POST /user/logout", identity.Logout)
	mux.HandleFunc("POST /user/guest-login", identity.GuestLogin)
	mux.HandleFunc("GET /user/me", httpx.RequireUser(identity.Me))
	mux.HandleFunc("POST /user/sign", httpx.RequireUser(identity.Sign))
	mux.HandleFunc("GET /user/sign/count", httpx.RequireUser(identity.SignCount))
	mux.HandleFunc("GET /user/info/{id}", identity.Info)
	mux.HandleFunc("GET /user/{id}", identity.UserByID)

	mux.HandleFunc("GET /voucher/manage/capabilities", httpx.RequireUser(vouchers.Capabilities))
	mux.HandleFunc("POST /voucher", httpx.RequireUser(vouchers.Create))
	mux.HandleFunc("POST /voucher/seckill", httpx.RequireUser(vouchers.CreateSeckill))
	mux.HandleFunc("DELETE /voucher/{id}", httpx.RequireUser(vouchers.Delete))

	// 订单写接口统一分派：避免 Go 1.22 路由对 "seckill/{id}" 与 "{id}/cancel" 的冲突。
	mux.HandleFunc("POST /voucher-order/", httpx.RequireUser(orders.Route))
	mux.HandleFunc("GET /voucher-order/mine", httpx.RequireUser(orders.Mine))
	mux.HandleFunc("GET /voucher-order/mine/vouchers", httpx.RequireUser(orders.MineVouchers))
	mux.HandleFunc("GET /voucher-order/of/voucher/{id}", httpx.RequireUser(orders.OfVoucher))
	mux.HandleFunc("GET /voucher-order/{id}", httpx.RequireUser(orders.Detail))

	mux.Handle("GET /uploads/", upload.Files())
	mux.Handle("GET /imgs/", upload.Images())

	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		httpx.OK(w, map[string]string{"status": "ok"})
	})
	// 中间件顺序：recover 最外层兜 panic，request_id 其次，auth 最后解析登录态。
	return httpx.Recover(httpx.RequestID(httpx.Auth(rdb)(mux)))
}
