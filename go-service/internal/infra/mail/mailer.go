// Package mail 提供登录验证码的邮件发送；未配置 SMTP 时退化为空实现。
package mail

import (
	"context"
	"crypto/tls"
	"fmt"
	"log/slog"
	"net"
	"net/smtp"
	"strings"
	"time"
)

type Sender interface {
	Send(ctx context.Context, to, subject, body string) error
	Enabled() bool
}

// Noop 未配置 SMTP 时使用：只记录日志，验证码仍需从 Redis 取。
type Noop struct{}

func (Noop) Send(ctx context.Context, to, subject, body string) error {
	slog.Warn("smtp not configured; code kept in redis", "to", to)
	return nil
}

func (Noop) Enabled() bool { return false }

// SMTP 用 QQ/163 等邮箱的 SMTP 发信；465 走隐式 TLS，其余端口走 STARTTLS。
type SMTP struct {
	Host string
	Port int
	User string
	Pass string
	From string
}

func (s *SMTP) Enabled() bool { return s != nil && s.Host != "" && s.User != "" && s.Pass != "" }

func (s *SMTP) Send(ctx context.Context, to, subject, body string) error {
	from := s.From
	if from == "" {
		from = s.User
	}
	message := strings.Join([]string{
		"From: " + from,
		"To: " + to,
		"Subject: " + subject,
		"MIME-Version: 1.0",
		"Content-Type: text/plain; charset=UTF-8",
		"Content-Transfer-Encoding: 8bit",
		"",
		body,
	}, "\r\n")

	address := fmt.Sprintf("%s:%d", s.Host, s.Port)
	auth := smtp.PlainAuth("", s.User, s.Pass, s.Host)

	if s.Port == 465 {
		return s.sendTLS(address, to, []byte(message), auth)
	}
	// 587/25：net/smtp 会在服务端支持时自动升级 STARTTLS。
	done := make(chan error, 1)
	go func() { done <- smtp.SendMail(address, auth, from, []string{to}, []byte(message)) }()
	select {
	case err := <-done:
		return err
	case <-time.After(15 * time.Second):
		return fmt.Errorf("smtp send timeout")
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *SMTP) sendTLS(address, to string, message []byte, auth smtp.Auth) error {
	conn, err := tls.DialWithDialer(&net.Dialer{Timeout: 10 * time.Second}, "tcp", address,
		&tls.Config{ServerName: s.Host, MinVersion: tls.VersionTLS12})
	if err != nil {
		return fmt.Errorf("smtp tls dial: %w", err)
	}
	defer conn.Close()
	client, err := smtp.NewClient(conn, s.Host)
	if err != nil {
		return fmt.Errorf("smtp client: %w", err)
	}
	defer client.Close()
	if err := client.Auth(auth); err != nil {
		return fmt.Errorf("smtp auth: %w", err)
	}
	from := s.From
	if from == "" {
		from = s.User
	}
	if err := client.Mail(from); err != nil {
		return err
	}
	if err := client.Rcpt(to); err != nil {
		return err
	}
	writer, err := client.Data()
	if err != nil {
		return err
	}
	if _, err := writer.Write(message); err != nil {
		return err
	}
	if err := writer.Close(); err != nil {
		return err
	}
	return client.Quit()
}
