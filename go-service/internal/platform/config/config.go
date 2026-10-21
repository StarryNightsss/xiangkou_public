// Package config 集中读取环境变量，保证部署时只换环境变量不改代码。
package config

import (
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	Port         string
	MySQLDSN     string
	RedisAddr    string
	RedisPass    string
	ShopCacheTTL time.Duration
	UploadDir    string
	MockPayment  bool
	AdminIDs     string
	DemoIDs      string

	SMTPHost string
	SMTPPort int
	SMTPUser string
	SMTPPass string
	SMTPFrom string

	ReconcileInterval time.Duration
}

func Load() Config {
	// 兼容 Java 时代的 SPRING_MAIL_USERNAME / SPRING_MAIL_PASSWORD，已有凭据可直接复用。
	user := env("SMTP_USER", env("SPRING_MAIL_USERNAME", ""))
	pass := env("SMTP_PASS", env("SPRING_MAIL_PASSWORD", ""))
	return Config{
		Port:         env("PORT", "8081"),
		MySQLDSN:     env("MYSQL_DSN", "root:xiangkou_local_only@tcp(mysql:3306)/xiangkou?parseTime=true&charset=utf8mb4&loc=Local"),
		RedisAddr:    env("REDIS_ADDR", "redis:6379"),
		RedisPass:    env("REDIS_PASSWORD", ""),
		ShopCacheTTL: time.Duration(envInt("SHOP_CACHE_TTL_SECONDS", 1800)) * time.Second,
		UploadDir:    env("UPLOAD_DIR", "./uploads"),
		MockPayment:  env("XIANGKOU_PAYMENT_MOCK_ENABLED", env("MOCK_PAYMENT_ENABLED", "false")) == "true",
		AdminIDs:     env("XIANGKOU_CATALOG_ADMIN_IDS", ""),
		DemoIDs:      env("XIANGKOU_CATALOG_DEMO_IDS", ""),
		SMTPHost:     env("SMTP_HOST", smtpHostFor(user)),
		SMTPPort:     envInt("SMTP_PORT", 465),
		SMTPUser:     user,
		SMTPPass:     pass,
		SMTPFrom:     env("SMTP_FROM", user),

		ReconcileInterval: time.Duration(envInt("RECONCILE_INTERVAL_SECONDS", 300)) * time.Second,
	}
}

// smtpHostFor 按发件邮箱域名推断 SMTP 服务器，覆盖常见的 QQ / 163 / 126。
func smtpHostFor(email string) string {
	at := strings.LastIndex(email, "@")
	if at < 0 {
		return ""
	}
	domain := strings.ToLower(email[at+1:])
	switch domain {
	case "qq.com":
		return "smtp.qq.com"
	case "163.com":
		return "smtp.163.com"
	case "126.com":
		return "smtp.126.com"
	case "gmail.com":
		return "smtp.gmail.com"
	}
	return ""
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envInt(key string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(key))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}
