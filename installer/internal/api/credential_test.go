package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

// fakeSecret stands in for Secret vws-initial-admin.
type fakeSecret struct {
	mu                sync.Mutex
	user, password    string
	consumed          bool
	storeErr, markErr error
	stores            int
}

func (f *fakeSecret) Store(_ context.Context, user, pw string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.stores++
	if f.storeErr != nil {
		return f.storeErr
	}
	if f.password != "" {
		return cluster.ErrCredentialExists
	}
	f.user, f.password = user, pw
	return nil
}

func (f *fakeSecret) Load(context.Context) (string, string, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.password == "" {
		return "", "", false, errors.New("no secret")
	}
	return f.user, f.password, f.consumed, nil
}

func (f *fakeSecret) MarkConsumed(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.markErr != nil {
		return f.markErr
	}
	f.consumed = true
	return nil
}

func credHarness(t *testing.T) (*harness, *fakeSecret) {
	h := newHarness(t, nil)
	fs := &fakeSecret{}
	h.srv.credentials = fs
	return h, fs
}

func (h *harness) installed() { h.t.Helper(); h.installed2("succeeded") }

// credential posts without recording the body in h.bodies, so the grep below proves the value
// is nowhere else.
func (h *harness) credential() (int, map[string]any, http.Header) {
	h.t.Helper()
	req, _ := http.NewRequest("POST", h.http.URL+"/api/v1/result/credential", nil)
	req.Header.Set("Authorization", "Bearer "+h.token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out, resp.Header
}

func TestCredentialOnce(t *testing.T) {
	h, fs := credHarness(t)
	if status, body, _ := h.credential(); status != 401 {
		t.Fatalf("no session: %d %v", status, body)
	}
	h.login()
	status, body, _ := h.credential()
	h.wantError(status, body, 409, ReasonStepOutOfOrder)
	h.installed()
	_, res := h.do("GET", "/api/v1/result", nil)
	if res["credentialAvailable"] != true {
		t.Fatalf("result %v", res)
	}
	status, body, hdr := h.credential()
	if status != 200 || body["adminUser"] != "admin" || hdr.Get("Cache-Control") != "no-store" {
		t.Fatalf("credential %d %v", status, body)
	}
	pw := body["password"].(string)
	if pw != fs.password || len(pw) != PasswordLength || strings.Trim(pw, passwordAlphabet) != "" {
		t.Fatalf("password shape %d", len(pw))
	}
	status, body, _ = h.credential()
	h.wantError(status, body, 410, ReasonCredentialConsumed)
	if h.state() != "finished" {
		t.Fatal("state is not finished")
	}
	_, res = h.do("GET", "/api/v1/result", nil)
	if res["credentialAvailable"] != false {
		t.Fatalf("result after %v", res)
	}
	// The value is only in the Secret: no response but the one hand-out, no CR, no pod log.
	for _, b := range []string{"/api/v1/state", "/api/v1/summary", "/api/v1/cr", "/api/v1/result"} {
		h.do("GET", b, nil)
	}
	if strings.Contains(h.bodies.String(), pw) {
		t.Fatal("the password reached a response")
	}
	for _, cr := range h.cluster.crs {
		if strings.Contains(string(cr), pw) {
			t.Fatal("the password reached the CR")
		}
	}
	if strings.Contains(strings.Join(h.codes, ""), pw) {
		t.Fatal("the password reached the code file")
	}
}

func TestCredentialFailures(t *testing.T) {
	h, fs := credHarness(t)
	fs.storeErr = errors.New("kubectl create failed")
	h.installed2("failed")
	_, res := h.do("GET", "/api/v1/result", nil)
	if res["credentialAvailable"] != false || !strings.Contains(res["error"].(string), cluster.InitialAdminSecret) {
		t.Fatalf("result %v", res)
	}

	h, fs = credHarness(t)
	h.installed()
	fs.markErr = errors.New("kubectl label failed")
	status, body, _ := h.credential()
	if status != 500 || body["password"] != nil {
		t.Fatalf("a failed mark returned %d %v", status, body)
	}
	fs.markErr = nil
	if status, _, _ := h.credential(); status != 200 {
		t.Fatal("retry after a failed mark")
	}

	// An installer that restarts after the hand-out finds the label and answers 410.
	h, fs = credHarness(t)
	fs.user, fs.password, fs.consumed = "admin", "Fake-Existing-Password-01", true
	h.installed()
	if fs.password != "Fake-Existing-Password-01" {
		t.Fatal("an existing Secret was replaced")
	}
	status, body, _ = h.credential()
	h.wantError(status, body, 410, ReasonCredentialConsumed)
}

func (h *harness) installed2(want string) {
	h.t.Helper()
	h.login()
	h.fill()
	hash := h.summary()
	h.cluster.release <- nil
	h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash})
	h.waitState(want)
}

func TestNewPassword(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		pw, err := newPassword()
		if err != nil || len(pw) != PasswordLength || strings.Trim(pw, passwordAlphabet) != "" || seen[pw] {
			t.Fatalf("%v %d", err, len(pw))
		}
		seen[pw] = true
	}
}
