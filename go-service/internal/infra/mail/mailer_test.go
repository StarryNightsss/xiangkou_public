package mail

import (
	"context"
	"testing"
)

func TestNoopDisabled(t *testing.T) {
	var sender Sender = Noop{}
	if sender.Enabled() {
		t.Fatal("Noop must report disabled")
	}
	if err := sender.Send(context.Background(), "a@b.com", "subject", "body"); err != nil {
		t.Fatalf("Noop.Send returned error: %v", err)
	}
}

func TestSMTPEnabledRequiresCredentials(t *testing.T) {
	empty := &SMTP{}
	if empty.Enabled() {
		t.Fatal("empty SMTP must be disabled")
	}
	full := &SMTP{Host: "smtp.qq.com", User: "u@qq.com", Pass: "p"}
	if !full.Enabled() {
		t.Fatal("configured SMTP must be enabled")
	}
}
