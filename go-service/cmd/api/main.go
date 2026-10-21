package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/app"
	"xiangkou/go-service/internal/infra/mail"
	"xiangkou/go-service/internal/infra/redislock"
	"xiangkou/go-service/internal/jobs"
	"xiangkou/go-service/internal/platform/config"
	"xiangkou/go-service/internal/repo/cache"
	"xiangkou/go-service/internal/repo/mysql"
	"xiangkou/go-service/internal/transport"
	"xiangkou/go-service/internal/transport/handlers"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))
	cfg := config.Load()

	db, err := mysql.Open(cfg.MySQLDSN)
	if err != nil {
		slog.Error("mysql connect failed", "err", err)
		os.Exit(1)
	}
	defer db.Close()

	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr, Password: cfg.RedisPass})
	if err := rdb.Ping(context.Background()).Err(); err != nil {
		slog.Error("redis connect failed", "err", err)
		os.Exit(1)
	}

	// 显式装配：依赖一眼可见，不依赖框架注入。
	shopRepo := mysql.NewShopRepo(db)
	locks := redislock.New(rdb)
	store := cache.New(rdb, cfg.ShopCacheTTL, locks)
	catalog := app.NewCatalog(shopRepo, store, rdb)
	community := app.NewCommunity(shopRepo, rdb)
	var mailer mail.Sender = mail.Noop{}
	if cfg.SMTPHost != "" && cfg.SMTPUser != "" && cfg.SMTPPass != "" {
		mailer = &mail.SMTP{Host: cfg.SMTPHost, Port: cfg.SMTPPort, User: cfg.SMTPUser, Pass: cfg.SMTPPass, From: cfg.SMTPFrom}
	}
	identity := app.NewIdentity(shopRepo, rdb, mailer)
	writer := app.NewWriter(shopRepo, rdb)
	orders := app.NewOrders(shopRepo, rdb, locks, cfg.MockPayment)
	vouchers := app.NewVoucherAdmin(shopRepo, cfg.AdminIDs, cfg.DemoIDs)
	consumer := jobs.NewSeckillConsumer(rdb, orders, shopRepo)
	reconciler := jobs.NewReconciler(rdb, shopRepo, cfg.ReconcileInterval)
	consumerCtx, stopConsumer := context.WithCancel(context.Background())
	defer stopConsumer()
	go consumer.Run(consumerCtx)
	go reconciler.Run(consumerCtx)
	router := transport.NewRouter(
		handlers.NewShopHandler(catalog),
		handlers.NewBlogHandler(community, writer),
		handlers.NewFollowHandler(community, writer),
		handlers.NewIdentityHandler(identity),
		handlers.NewUploadHandler(cfg.UploadDir),
		handlers.NewOrderHandler(orders),
		handlers.NewVoucherAdminHandler(vouchers),
		rdb,
	)

	server := &http.Server{
		Addr:              ":" + cfg.Port,
		Handler:           router,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		slog.Info("http server starting", "addr", server.Addr)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("http server failed", "err", err)
			os.Exit(1)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop

	// 两段式退出：先停止接收新连接，再给在途请求 10s 排空。
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		slog.Error("graceful shutdown failed", "err", err)
	}
	slog.Info("http server stopped")
}
