package api

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// failWriter fails every body write, as a client that closed the connection does, and counts the
// headers the response sent.
type failWriter struct {
	err     error
	headers int
}

func (w *failWriter) Header() http.Header       { return http.Header{} }
func (w *failWriter) Write([]byte) (int, error) { return 0, w.err }
func (w *failWriter) WriteHeader(int)           { w.headers++ }
func (w *failWriter) Flush()                    {}

func TestResponseWriteErrorsAreReported(t *testing.T) {
	broken := &failWriter{err: errors.New("client went away")}
	cases := []struct {
		path string
		call func(*Server, http.ResponseWriter, *http.Request) error
	}{
		{"/api/v1/state", (*Server).getState},
		{"/api/v1/install/events", (*Server).getEvents},
	}
	for _, c := range cases {
		t.Run(c.path, func(t *testing.T) {
			req := httptest.NewRequestWithContext(context.Background(), "GET", c.path, nil)
			if err := c.call(New(Config{}), broken, req); err == nil {
				t.Fatalf("%s dropped the response write error", c.path)
			}
		})
	}
}

// TestErrorAfterTheResponseStartedWritesNoSecondHeader covers a handler that writes the header
// and then fails: the router must not write an error header over the response that already
// started (vws-deploy#387).
func TestErrorAfterTheResponseStartedWritesNoSecondHeader(t *testing.T) {
	auth, err := NewAuth(time.Now, func(string) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	tok, apiErr := auth.Exchange(auth.Secrets()[0])
	if apiErr != nil {
		t.Fatal(apiErr)
	}
	w := &failWriter{err: errors.New("client went away")}
	req := httptest.NewRequestWithContext(context.Background(), "GET", "/api/v1/state", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	New(Config{Auth: auth}).Handler().ServeHTTP(w, req)
	if w.headers != 1 {
		t.Fatalf("the response sent %d headers, want 1: the router wrote an error after the response started", w.headers)
	}
}
