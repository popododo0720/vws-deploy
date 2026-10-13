package api

import (
	"bytes"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
)

// syncBuf is a goroutine-safe log sink.
type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuf) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

// captureLogs sends the default slog logger (what the pod log carries) to a buffer at debug level.
func captureLogs(t *testing.T) *syncBuf {
	t.Helper()
	buf := &syncBuf{}
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return buf
}

// TestCredentialNotInEventsOrLogs: the generated password reaches neither the install event
// stream (GET /api/v1/install/events) nor the log, through a failed and a good hand-out.
func TestCredentialNotInEventsOrLogs(t *testing.T) {
	logs := captureLogs(t)
	h, fs := credHarness(t)
	h.cluster.lines = []string{"helm upgrade vws", "waiting for the console"}
	h.installed()
	pw := fs.password
	if len(pw) != PasswordLength {
		t.Fatalf("no password generated: %d", len(pw))
	}
	// A failed mark logs an internal error; that line must not carry the value either.
	fs.markErr = errors.New("kubectl label failed")
	if status, _, _ := h.credential(); status != 500 {
		t.Fatalf("failed mark: %d", status)
	}
	fs.markErr = nil
	status, body, _ := h.credential()
	if status != 200 || body["password"] != pw {
		t.Fatalf("hand-out %d", status)
	}
	h.credential() // 410
	status, evs, raw := h.stream("")
	if status != 200 || len(evs) == 0 {
		t.Fatalf("stream %d %v", status, evs)
	}
	if strings.Contains(raw, pw) {
		t.Fatalf("the password reached the event stream:\n%s", raw)
	}
	if !strings.Contains(logs.String(), "kubectl label failed") {
		t.Fatalf("the log capture saw nothing:\n%s", logs.String())
	}
	if strings.Contains(logs.String(), pw) {
		t.Fatal("the password reached the log")
	}
}
