package api

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

// failWriter fails every body write, as a client that closed the connection does.
type failWriter struct{ err error }

func (w failWriter) Header() http.Header       { return http.Header{} }
func (w failWriter) Write([]byte) (int, error) { return 0, w.err }
func (w failWriter) WriteHeader(int)           {}
func (w failWriter) Flush()                    {}

func TestResponseWriteErrorsAreReported(t *testing.T) {
	broken := failWriter{err: errors.New("client went away")}
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
