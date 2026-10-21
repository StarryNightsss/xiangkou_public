package config

import "testing"

func TestSMTPHostForCommonDomains(t *testing.T) {
	cases := map[string]string{
		"a@qq.com":    "smtp.qq.com",
		"A@QQ.COM":    "smtp.qq.com",
		"b@163.com":   "smtp.163.com",
		"c@126.com":   "smtp.126.com",
		"d@gmail.com": "smtp.gmail.com",
		"bad":         "",
		"e@unknown":   "",
	}
	for input, want := range cases {
		if got := smtpHostFor(input); got != want {
			t.Fatalf("smtpHostFor(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestLoadReadsLegacyMailKeys(t *testing.T) {
	t.Setenv("SPRING_MAIL_USERNAME", "user@qq.com")
	t.Setenv("SPRING_MAIL_PASSWORD", "secret")
	t.Setenv("SMTP_HOST", "")
	t.Setenv("SMTP_USER", "")
	t.Setenv("SMTP_PASS", "")
	cfg := Load()
	if cfg.SMTPUser != "user@qq.com" || cfg.SMTPPass != "secret" {
		t.Fatalf("legacy mail keys not honored: %+v", cfg)
	}
	if cfg.SMTPHost != "smtp.qq.com" {
		t.Fatalf("host not derived: %q", cfg.SMTPHost)
	}
	if cfg.SMTPFrom != "user@qq.com" {
		t.Fatalf("from not defaulted: %q", cfg.SMTPFrom)
	}
}
