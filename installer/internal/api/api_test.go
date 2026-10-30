package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

// fakeCluster is the fake cluster: fixed nodes, and an install that waits for release.
type fakeCluster struct {
	mu       sync.Mutex
	nodes    []cluster.Node
	nodesErr error
	// nodesStarted and nodesRelease gate a node lookup: when set, Nodes signals on started and
	// waits for a receive or close on release.
	nodesStarted chan struct{}
	nodesRelease chan struct{}
	release      chan error
	crs          [][]byte
	// lines are logged after "installing" (#145).
	lines []string
}

func (f *fakeCluster) Nodes(context.Context) ([]cluster.Node, error) {
	if f.nodesStarted != nil {
		f.nodesStarted <- struct{}{}
		<-f.nodesRelease
	}
	if f.nodesErr != nil {
		return nil, f.nodesErr
	}
	return f.nodes, nil
}

func (f *fakeCluster) Install(_ context.Context, cr []byte, log func(string)) (cluster.Result, error) {
	f.mu.Lock()
	f.crs = append(f.crs, cr)
	f.mu.Unlock()
	log("installing")
	for _, l := range f.lines {
		log(l)
	}
	if err := <-f.release; err != nil {
		return cluster.Result{}, err
	}
	return cluster.Result{ConsoleURL: "https://console.example.invalid", AdminUser: "admin"}, nil
}

type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) now() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *clock) add(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

type harness struct {
	t       *testing.T
	srv     *Server
	http    *httptest.Server
	cluster *fakeCluster
	clock   *clock
	codes   []string
	token   string
	bodies  bytes.Buffer
	header  http.Header
}

func newHarness(t *testing.T, pf Preflight) *harness {
	t.Helper()
	h := &harness{t: t, clock: &clock{t: time.Date(2026, 10, 13, 7, 0, 0, 0, time.UTC)}}
	h.cluster = &fakeCluster{
		nodes: []cluster.Node{
			{Name: "node1", NICs: []cluster.NIC{{Name: "ens3", MTU: 1500}, {Name: "ens8", MTU: 9000}}},
			{Name: "node2", NICs: []cluster.NIC{{Name: "ens3", MTU: 1500}}},
		},
		release: make(chan error, 1),
	}
	auth, err := NewAuth(h.clock.now, func(code string) error { h.codes = append(h.codes, code); return nil })
	if err != nil {
		t.Fatal(err)
	}
	h.srv = New(Config{Auth: auth, Cluster: h.cluster, Preflight: pf, Now: h.clock.now, APIVersion: crdAPIVersion(t)})
	h.http = httptest.NewServer(h.srv.Handler())
	t.Cleanup(func() {
		select {
		case h.cluster.release <- nil:
		default:
		}
		h.srv.Wait()
		h.http.Close()
	})
	return h
}

func (h *harness) code() string { return h.codes[len(h.codes)-1] }

// do sends a request with the current token and returns the status and the JSON body.
func (h *harness) do(method, path string, body any) (int, map[string]any) {
	h.t.Helper()
	var rd io.Reader
	switch b := body.(type) {
	case nil:
	case string:
		rd = strings.NewReader(b)
	default:
		j, _ := json.Marshal(b)
		rd = bytes.NewReader(j)
	}
	req, _ := http.NewRequest(method, h.http.URL+path, rd)
	if h.token != "" {
		req.Header.Set("Authorization", "Bearer "+h.token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	h.header = resp.Header
	raw, _ := io.ReadAll(resp.Body)
	h.bodies.Write(raw)
	var out map[string]any
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			h.t.Fatalf("%s %s: body is not JSON: %s", method, path, raw)
		}
	}
	if ct := resp.Header.Get("Content-Type"); ct != "application/json" {
		h.t.Fatalf("%s %s: Content-Type %q", method, path, ct)
	}
	return resp.StatusCode, out
}

func (h *harness) login() {
	h.t.Helper()
	status, body := h.do("POST", "/api/v1/session", map[string]string{"code": h.code()})
	if status != 200 {
		h.t.Fatalf("login: %d %v", status, body)
	}
	h.token = body["token"].(string)
}

// wantError checks the #139 error model: status, code, a message and retryable false.
func (h *harness) wantError(status int, body map[string]any, wantStatus int, code Reason) map[string]any {
	h.t.Helper()
	e, _ := body["error"].(map[string]any)
	if status != wantStatus || e == nil || e["code"] != string(code) {
		h.t.Fatalf("want %d %s, got %d %v", wantStatus, code, status, body)
	}
	if m, _ := e["message"].(string); m == "" {
		h.t.Fatalf("error %s has no message", code)
	}
	if e["retryable"] != false {
		h.t.Fatalf("error %s: retryable %v", code, e["retryable"])
	}
	return e
}

var goodValues = []struct {
	step  string
	value any
}{
	{"roles", map[string]any{"nodes": []string{"node1", "node2"}}},
	{"network", map[string]any{"nodes": map[string]any{
		"management": map[string]any{"nic": "ens3", "cidr": "10.0.0.0/24"},
		"external":   map[string]any{"nic": "ens8", "cidr": "198.18.0.0/24", "gateway": "198.18.0.1"},
	}}},
	{"storage", map[string]any{"driver": "rook-ceph"}},
	{"eip", "198.18.0.128/25"},
	{"profile", "small"},
	{"observability", map[string]any{"otlpEndpoint": ""}},
}

func (h *harness) fill() {
	h.t.Helper()
	for _, v := range goodValues {
		if status, body := h.do("PUT", "/api/v1/steps/"+v.step, map[string]any{"value": v.value}); status != 200 || body["status"] != "valid" {
			h.t.Fatalf("PUT %s: %d %v", v.step, status, body)
		}
	}
}

func (h *harness) state() string {
	h.t.Helper()
	_, body := h.do("GET", "/api/v1/state", nil)
	return body["state"].(string)
}

func (h *harness) summary() string {
	h.t.Helper()
	status, body := h.do("GET", "/api/v1/summary", nil)
	if status != 200 {
		h.t.Fatalf("summary: %d %v", status, body)
	}
	return body["specHash"].(string)
}

func (h *harness) waitState(want string) {
	h.t.Helper()
	h.srv.Wait()
	if got := h.state(); got != want {
		h.t.Fatalf("state %s, want %s", got, want)
	}
}

func TestAccessCodeShape(t *testing.T) {
	h := newHarness(t, nil)
	re := regexp.MustCompile(`^[` + codeAlphabet + `]{16}$`)
	if !re.MatchString(h.code()) {
		t.Fatalf("code %q is not 16 characters from the alphabet", h.code())
	}
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		if err := h.srv.auth.rotate(); err != nil {
			t.Fatal(err)
		}
		if seen[h.code()] {
			t.Fatalf("code repeated after %d rotations", i)
		}
		seen[h.code()] = true
	}
}

func TestEveryEndpointNeedsASession(t *testing.T) {
	h := newHarness(t, nil)
	for _, e := range []struct{ method, path string }{
		{"GET", "/api/v1/state"},
		{"GET", "/api/v1/steps/roles"},
		{"PUT", "/api/v1/steps/roles"},
		{"POST", "/api/v1/steps/roles/preflight"},
		{"GET", "/api/v1/summary"},
		{"POST", "/api/v1/install"},
		{"GET", "/api/v1/install/events"},
		{"GET", "/api/v1/result"},
		{"POST", "/api/v1/result/credential"},
		{"GET", "/api/v1/cr"},
		{"POST", "/api/v1/cr"},
	} {
		status, body := h.do(e.method, e.path, nil)
		h.wantError(status, body, 401, ReasonUnauthenticated)
	}
	h.token = "not-a-token"
	status, body := h.do("GET", "/api/v1/state", nil)
	h.wantError(status, body, 401, ReasonUnauthenticated)
}

func TestOneTimeCode(t *testing.T) {
	h := newHarness(t, nil)
	first := h.code()
	h.login()
	if h.code() == first {
		t.Fatal("a used code was not replaced")
	}
	old := h.token
	h.token = ""
	status, body := h.do("POST", "/api/v1/session", map[string]string{"code": first})
	h.wantError(status, body, 401, ReasonUnauthenticated)

	// A second login with the next code replaces the first session: one session at a time.
	h.login()
	h.token, old = old, h.token
	status, body = h.do("GET", "/api/v1/state", nil)
	h.wantError(status, body, 401, ReasonUnauthenticated)
	h.token = old
	if status, _ := h.do("GET", "/api/v1/state", nil); status != 200 {
		t.Fatalf("new session: %d", status)
	}
	if strings.Contains(h.bodies.String(), first) || strings.Contains(h.bodies.String(), h.code()) {
		t.Fatal("an access code appeared in a response body")
	}
}

func TestCookieSession(t *testing.T) {
	h := newHarness(t, nil)
	resp, err := http.Post(h.http.URL+"/api/v1/session", "application/json", strings.NewReader(`{"code":"`+h.code()+`"}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	var c *http.Cookie
	for _, x := range resp.Cookies() {
		if x.Name == SessionCookie {
			c = x
		}
	}
	if c == nil || !c.HttpOnly || !c.Secure || c.SameSite != http.SameSiteStrictMode {
		t.Fatalf("session cookie %+v", c)
	}
	req, _ := http.NewRequest("GET", h.http.URL+"/api/v1/state", nil)
	req.AddCookie(&http.Cookie{Name: SessionCookie, Value: c.Value})
	got, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	got.Body.Close()
	if got.StatusCode != 200 {
		t.Fatalf("cookie session: %d", got.StatusCode)
	}
}

func TestFiveWrongCodesIssueANewOne(t *testing.T) {
	h := newHarness(t, nil)
	first := h.code()
	for i := 1; i <= 4; i++ {
		status, body := h.do("POST", "/api/v1/session", map[string]string{"code": "wrong-code-xxxxx"})
		h.wantError(status, body, 401, ReasonUnauthenticated)
	}
	status, body := h.do("POST", "/api/v1/session", map[string]string{"code": "wrong-code-xxxxx"})
	h.wantError(status, body, 403, ReasonForbidden)
	if h.code() == first {
		t.Fatal("five wrong codes did not issue a new code")
	}
	status, body = h.do("POST", "/api/v1/session", map[string]string{"code": first})
	h.wantError(status, body, 401, ReasonUnauthenticated)
	h.login()
}

func TestIdleSessionExpires(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.clock.add(29 * time.Minute)
	if status, _ := h.do("GET", "/api/v1/state", nil); status != 200 {
		t.Fatal("session expired before 30 minutes")
	}
	h.clock.add(29 * time.Minute) // the last request refreshed the idle timer
	if status, _ := h.do("GET", "/api/v1/state", nil); status != 200 {
		t.Fatal("activity did not refresh the idle timer")
	}
	h.clock.add(31 * time.Minute)
	status, body := h.do("GET", "/api/v1/state", nil)
	h.wantError(status, body, 401, ReasonSessionExpired)
	status, body = h.do("GET", "/api/v1/state", nil)
	h.wantError(status, body, 401, ReasonUnauthenticated)
}

func TestHappyPath(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	if h.state() != "editing" {
		t.Fatal("a new session is not editing")
	}
	status, body := h.do("GET", "/api/v1/steps/network", nil)
	if status != 200 || body["status"] != "pending" {
		t.Fatalf("GET step: %d %v", status, body)
	}
	nics := body["options"].(map[string]any)["nics"].([]any)
	if len(nics) != 2 || nics[0].(map[string]any)["name"] != "ens3" || len(nics[0].(map[string]any)["nodes"].([]any)) != 2 {
		t.Fatalf("nic options %v", nics)
	}
	_, body = h.do("GET", "/api/v1/steps/profile", nil)
	if body["defaults"] != "small" {
		t.Fatalf("profile default for two nodes: %v", body["defaults"])
	}
	h.fill()
	_, st := h.do("GET", "/api/v1/state", nil)
	for _, s := range st["steps"].([]any) {
		if s.(map[string]any)["status"] != "valid" {
			t.Fatalf("state steps %v", st["steps"])
		}
	}
	hash := h.summary()
	if h.state() != "confirming" {
		t.Fatal("summary did not move to confirming")
	}
	status, body = h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash})
	if status != 202 || body["state"] != "installing" {
		t.Fatalf("install: %d %v", status, body)
	}
	status, body = h.do("PUT", "/api/v1/steps/profile", map[string]any{"value": "ha"})
	h.wantError(status, body, 409, ReasonInstallInProgress)
	status, body = h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash})
	h.wantError(status, body, 409, ReasonInstallInProgress)
	status, body = h.do("GET", "/api/v1/summary", nil)
	h.wantError(status, body, 409, ReasonInstallInProgress)

	h.cluster.release <- nil
	h.waitState("succeeded")
	_, body = h.do("GET", "/api/v1/result", nil)
	if body["consoleURL"] != "https://console.example.invalid" || body["adminUser"] != "admin" || body["credentialAvailable"] != false {
		t.Fatalf("result %v", body)
	}
	status, body = h.do("PUT", "/api/v1/steps/profile", map[string]any{"value": "ha"})
	h.wantError(status, body, 409, ReasonAlreadyInstalled)
	status, body = h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash})
	h.wantError(status, body, 409, ReasonAlreadyInstalled)

	// The installed CR is the summary CR, and its hash is the confirmed hash.
	var cr map[string]any
	if err := json.Unmarshal(h.cluster.crs[0], &cr); err != nil {
		t.Fatal(err)
	}
	if got, _, _ := specHash(cr); got != hash {
		t.Fatalf("installed CR hash %s, confirmed %s", got, hash)
	}
	spec := cr["spec"].(map[string]any)
	ext := spec["network"].(map[string]any)["nodes"].(map[string]any)["external"].(map[string]any)
	if ext["eipPool"] != "198.18.0.128/25" || spec["profile"] != "small" || spec["storage"].(map[string]any)["driver"] != "rook-ceph" {
		t.Fatalf("CR spec %v", spec)
	}
	if cr["apiVersion"] != crdAPIVersion(t) || cr["kind"] != "VWSCluster" {
		t.Fatalf("CR apiVersion %v kind %v", cr["apiVersion"], cr["kind"])
	}
	if _, ok := spec["roles"]; ok {
		t.Fatal("roles leaked into the CR spec")
	}
}

// crdAPIVersion reads group/version from the chart CRD, the one place the group is written (R18).
func crdAPIVersion(t *testing.T) string {
	t.Helper()
	crd, err := os.ReadFile("../../../charts/vws/crds/vwsclusters.yaml")
	if err != nil {
		t.Fatal(err)
	}
	group := regexp.MustCompile(`(?m)^  group: (\S+)$`).FindSubmatch(crd)
	version := regexp.MustCompile(`(?m)^    - name: (\S+)$`).FindSubmatch(crd)
	if group == nil || version == nil {
		t.Fatal("no group or version in the CRD")
	}
	return string(group[1]) + "/" + string(version[1])
}

func TestStepOrder(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	status, body := h.do("PUT", "/api/v1/steps/network", map[string]any{"value": goodValues[1].value})
	h.wantError(status, body, 409, ReasonStepOutOfOrder)
	status, body = h.do("GET", "/api/v1/summary", nil)
	h.wantError(status, body, 409, ReasonStepOutOfOrder)
	status, body = h.do("POST", "/api/v1/steps/roles/preflight", nil)
	h.wantError(status, body, 409, ReasonStepOutOfOrder)
	status, body = h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": "x"})
	h.wantError(status, body, 409, ReasonNotConfirmed)
}

func TestSpecChangedAndConfirm(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	hash := h.summary()
	status, body := h.do("POST", "/api/v1/install", map[string]any{"confirm": false, "specHash": hash})
	h.wantError(status, body, 400, ReasonBadRequest)
	status, body = h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": "stale"})
	h.wantError(status, body, 409, ReasonSpecChanged)

	// Saving a step after the summary goes back to editing; the old summary no longer installs.
	if status, _ := h.do("PUT", "/api/v1/steps/profile", map[string]any{"value": "ha"}); status != 200 {
		t.Fatal("PUT after summary")
	}
	if h.state() != "editing" {
		t.Fatal("PUT after summary did not go back to editing")
	}
	status, body = h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash})
	h.wantError(status, body, 409, ReasonNotConfirmed)
	if h.summary() == hash {
		t.Fatal("a different draft has the same specHash")
	}
}

func TestFailedInstallRetries(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	hash := h.summary()
	h.cluster.release <- errors.New("helm upgrade failed: password=hunter2-fake")
	if status, _ := h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash}); status != 202 {
		t.Fatal("install")
	}
	h.waitState("failed")
	_, body := h.do("GET", "/api/v1/result", nil)
	if body["error"] == "" || strings.Contains(body["error"].(string), "hunter2") {
		t.Fatalf("failed result %v", body)
	}
	// failed -> editing on the next save, then a fresh summary installs again.
	if status, _ := h.do("PUT", "/api/v1/steps/observability", map[string]any{"value": map[string]any{"otlpEndpoint": ""}}); status != 200 {
		t.Fatal("PUT after failure")
	}
	if h.state() != "editing" {
		t.Fatal("failed did not go back to editing")
	}
	hash = h.summary()
	if status, _ := h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash}); status != 202 {
		t.Fatal("retry install")
	}
	h.cluster.release <- nil
	h.waitState("succeeded")
	if strings.Contains(h.bodies.String(), "hunter2") {
		t.Fatal("the install error cause reached a response")
	}
}

func TestValidation(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	cases := []struct {
		step  string
		value any
		field string
		code  string
	}{
		{"roles", map[string]any{"nodes": []string{}}, "roles.nodes", "required"},
		{"roles", map[string]any{"nodes": []string{"node9"}}, "roles.nodes[0]", "not_found"},
		{"roles", map[string]any{"extra": true}, "value", "invalid"},
	}
	for _, c := range cases {
		status, body := h.do("PUT", "/api/v1/steps/"+c.step, map[string]any{"value": c.value})
		e := h.wantError(status, body, 400, ReasonValidationFailed)
		f := e["fields"].([]any)[0].(map[string]any)
		if f["field"] != c.field || f["code"] != c.code {
			t.Fatalf("%s %v: fields %v", c.step, c.value, e["fields"])
		}
	}
	_, body := h.do("GET", "/api/v1/steps/roles", nil)
	if body["status"] != "invalid" {
		t.Fatalf("an invalid value left the step %v", body["status"])
	}
	h.fill()
	more := []struct {
		step  string
		value any
		field string
		code  string
	}{
		{"network", map[string]any{"driver": "calico"}, "spec.network.driver", "unsupported"},
		{"network", map[string]any{"nodes": map[string]any{"management": map[string]any{"nic": "ens3", "cidr": "10.0.0.0/33"}}}, "spec.network.nodes.management.cidr", "invalid"},
		{"network", map[string]any{"nodes": map[string]any{"management": map[string]any{"nic": "ens3", "cidr": "10.0.0.0/24", "vlan": 5000}}}, "spec.network.nodes.management.vlan", "out_of_range"},
		{"network", map[string]any{"nodes": map[string]any{"management": map[string]any{"nic": "ens3", "cidr": "10.0.0.0/24"}, "external": map[string]any{"nic": "ens8", "cidr": "198.18.0.0/24", "gateway": "10.9.9.1"}}}, "spec.network.nodes.external.gateway", "invalid"},
		{"network", map[string]any{"nodes": map[string]any{"management": map[string]any{"nic": "ens3", "cidr": "10.0.0.0/24"}, "external": map[string]any{"nic": "ens8", "cidr": "198.18.0.0/24", "gateway": "198.18.0.1", "eipPool": "198.18.0.0/25"}}}, "spec.network.nodes.external.eipPool", "wrong_step"},
		{"storage", map[string]any{"driver": "minio"}, "spec.storage.driver", "not_available"},
		{"storage", map[string]any{"driver": "local"}, "spec.storage.driver", "not_available"},
		{"storage", map[string]any{"driver": "existing"}, "spec.storage.storageClass", "required"},
		{"storage", map[string]any{"driver": "nfs", "nfs": map[string]any{"server": "nfs.example.invalid"}}, "spec.storage.nfs.share", "required"},
		{"storage", map[string]any{"driver": "rook-ceph", "volumeTypes": map[string]any{"gp4": map[string]any{"storageClass": "fast"}}}, "spec.storage.volumeTypes.gp4", "unsupported"},
		{"storage", map[string]any{"driver": "rook-ceph", "volumeTypes": map[string]any{"gp3": map[string]any{"volumeMode": "Block"}}}, "spec.storage.volumeTypes.gp3.storageClass", "required"},
		{"storage", map[string]any{"driver": "rook-ceph", "volumeTypes": map[string]any{"gp3": map[string]any{"storageClass": "Fast"}}}, "spec.storage.volumeTypes.gp3.storageClass", "invalid"},
		{"storage", map[string]any{"driver": "rook-ceph", "volumeTypes": map[string]any{"io2": map[string]any{"storageClass": "fast", "volumeMode": "Raw"}}}, "spec.storage.volumeTypes.io2.volumeMode", "unsupported"},
		{"storage", map[string]any{"driver": "rook-ceph", "volumeTypes": map[string]any{"io2": map[string]any{"storageClass": "fast", "volumeSnapshotClass": "snap_1"}}}, "spec.storage.volumeTypes.io2.volumeSnapshotClass", "invalid"},
		{"storage", map[string]any{"driver": "rook-ceph", "volumeTypes": map[string]any{"gp3": map[string]any{"storageClass": "fast", "iops": 3000}}}, "value", "invalid"},
		{"storage", map[string]any{"driver": "nfs", "nfs": map[string]any{"server": "nas", "share": "/e"}, "volumeTypes": map[string]any{"gp3": map[string]any{"storageClass": "fast"}}}, "spec.storage.volumeTypes", "unsupported"},
		{"eip", "", "spec.network.nodes.external.eipPool", "required"},
		{"eip", "10.1.0.0/24", "spec.network.nodes.external.eipPool", "invalid"},
		{"profile", "huge", "spec.profile", "unsupported"},
		{"observability", map[string]any{"otlpEndpoint": "http://collector:4317"}, "spec.observability.otlpEndpoint", "not_available"},
	}
	for _, c := range more {
		h.fill()
		status, body := h.do("PUT", "/api/v1/steps/"+c.step, map[string]any{"value": c.value})
		e := h.wantError(status, body, 400, ReasonValidationFailed)
		found := false
		for _, f := range e["fields"].([]any) {
			m := f.(map[string]any)
			found = found || (m["field"] == c.field && m["code"] == c.code)
		}
		if !found {
			t.Fatalf("%s %v: fields %v, want %s %s", c.step, c.value, e["fields"], c.field, c.code)
		}
	}
	_, body = h.do("GET", "/api/v1/steps/storage", nil)
	drivers := body["options"].(map[string]any)["drivers"].([]any)
	if d := drivers[3].(map[string]any); d["driver"] != "minio" || d["disabled"] != true || !strings.Contains(d["reason"].(string), "Coming soon") {
		t.Fatalf("minio option %v", d)
	}
	if got := body["options"].(map[string]any)["volumeTypes"]; !reflect.DeepEqual(got, []any{"standard", "gp2", "gp3", "io1", "io2", "st1", "sc1"}) {
		t.Fatalf("volume type options %v", got)
	}
}

// A valid per-type mapping (#391) is kept as sent, unmapped types stay absent, and the summary CR
// carries it under spec.storage.volumeTypes.
func TestStorageVolumeTypesReachTheCR(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	value := map[string]any{"driver": "existing", "storageClass": "ceph-block", "volumeTypes": map[string]any{
		"gp3": map[string]any{"storageClass": "ceph-block", "volumeMode": "Block", "volumeSnapshotClass": "ceph-snap"},
		"sc1": map[string]any{"storageClass": "cold.nfs", "volumeMode": "Filesystem"},
	}}
	if status, body := h.do("PUT", "/api/v1/steps/storage", map[string]any{"value": value}); status != 200 {
		t.Fatalf("PUT storage %d %v", status, body)
	}
	status, body := h.do("GET", "/api/v1/summary", nil)
	if status != 200 {
		t.Fatalf("summary %d %v", status, body)
	}
	got := body["cr"].(map[string]any)["spec"].(map[string]any)["storage"].(map[string]any)["volumeTypes"]
	if !reflect.DeepEqual(got, value["volumeTypes"]) {
		t.Fatalf("volumeTypes %v, want %v", got, value["volumeTypes"])
	}
}

func TestEIPFollowsTheNetwork(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	moved := map[string]any{"nodes": map[string]any{
		"management": map[string]any{"nic": "ens3", "cidr": "10.0.0.0/24"},
		"external":   map[string]any{"nic": "ens8", "cidr": "198.19.0.0/24", "gateway": "198.19.0.1"},
	}}
	if status, _ := h.do("PUT", "/api/v1/steps/network", map[string]any{"value": moved}); status != 200 {
		t.Fatal("PUT network")
	}
	_, body := h.do("GET", "/api/v1/steps/eip", nil)
	if body["status"] != "invalid" {
		t.Fatalf("eip outside the new external network stays %v", body["status"])
	}
	status, body := h.do("GET", "/api/v1/summary", nil)
	h.wantError(status, body, 409, ReasonStepOutOfOrder)
}

func TestPreflightBlocksAndWarns(t *testing.T) {
	var mu sync.Mutex
	block := true
	pf := func(_ context.Context, step string, _ any, _ []cluster.Node) []Check {
		mu.Lock()
		defer mu.Unlock()
		switch {
		case step == "network" && block:
			return []Check{{ID: "nic.exists", Severity: "error", Status: "fail", Node: "node2", Field: "spec.network.nodes.external.nic", Message: "node2 has no ens8"}}
		case step == "network":
			return []Check{{ID: "nic.mtu", Severity: "warning", Status: "fail", Message: "mtu differs"}, {ID: "nic.link", Severity: "info", Status: "unknown", Message: "R10"}}
		}
		return nil
	}
	h := newHarness(t, pf)
	h.login()
	h.do("PUT", "/api/v1/steps/roles", map[string]any{"value": goodValues[0].value})
	status, body := h.do("PUT", "/api/v1/steps/network", map[string]any{"value": goodValues[1].value})
	if status != 200 || body["status"] != "invalid" {
		t.Fatalf("a blocking preflight: %d %v", status, body)
	}
	if c := body["preflight"].(map[string]any)["checks"].([]any)[0].(map[string]any); c["id"] != "nic.exists" {
		t.Fatalf("preflight %v", body["preflight"])
	}
	status, body = h.do("PUT", "/api/v1/steps/storage", map[string]any{"value": goodValues[2].value})
	h.wantError(status, body, 409, ReasonStepOutOfOrder)

	mu.Lock()
	block = false
	mu.Unlock()
	status, body = h.do("POST", "/api/v1/steps/network/preflight", nil)
	if status != 200 || body["status"] != "valid" {
		t.Fatalf("preflight again: %d %v", status, body)
	}
	h.fill()
	_, body = h.do("GET", "/api/v1/summary", nil)
	if w := body["warnings"].([]any); len(w) != 2 {
		t.Fatalf("summary warnings %v", w)
	}
}

func TestBadRequests(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	for _, b := range []string{`not json`, `{"value": 1, "extra": 2}`, `{}`, `{"value": {"nodes":["node1"]}} {}`} {
		status, body := h.do("PUT", "/api/v1/steps/roles", b)
		h.wantError(status, body, 400, ReasonBadRequest)
	}
	big := `{"value": "` + strings.Repeat("a", 2<<20) + `"}`
	status, body := h.do("PUT", "/api/v1/steps/roles", big)
	h.wantError(status, body, 400, ReasonBadRequest)
	status, body = h.do("GET", "/api/v1/steps/billing", nil)
	h.wantError(status, body, 404, ReasonNotFound)
	status, body = h.do("GET", "/api/v1/nothing", nil)
	h.wantError(status, body, 404, ReasonNotFound)
	status, body = h.do("POST", "/api/v1/result/credential", nil)
	h.wantError(status, body, 501, ReasonNotImplemented)
}

func TestUnsupportedMethodIs405(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	for _, c := range []struct{ method, path, allow string }{
		{"DELETE", "/api/v1/cr", "GET, POST"},
		{"PATCH", "/api/v1/steps/roles", "GET, PUT"},
		{"GET", "/api/v1/install", "POST"},
		{"POST", "/api/v1/state", "GET"},
		{"GET", "/api/v1/session", "POST"},
	} {
		status, body := h.do(c.method, c.path, nil)
		h.wantError(status, body, 405, Reason("method_not_allowed"))
		if got := h.header.Get("Allow"); got != c.allow {
			t.Fatalf("%s %s: Allow %q, want %q", c.method, c.path, got, c.allow)
		}
	}
	status, body := h.do("DELETE", "/api/v1/nothing", nil)
	h.wantError(status, body, 404, ReasonNotFound)
	if got := h.header.Get("Allow"); got != "" {
		t.Fatalf("unknown path has Allow %q", got)
	}
}

func TestEveryReasonHasAStatus(t *testing.T) {
	for r, v := range reasons {
		if v.status < 400 || v.code.String() == "Unspecified" {
			t.Fatalf("reason %s maps to %d %v", r, v.status, v.code)
		}
	}
	for _, r := range []Reason{
		ReasonValidationFailed, ReasonBadRequest, ReasonUnauthenticated, ReasonSessionExpired, ReasonForbidden,
		ReasonStepOutOfOrder, ReasonSpecChanged, ReasonInstallInProgress, ReasonAlreadyInstalled,
	} {
		if _, ok := reasons[r]; !ok {
			t.Fatalf("#139 reason %s is missing", r)
		}
	}
}

func TestRoute(t *testing.T) {
	h := newHarness(t, nil)
	var seen string
	handler := h.srv.Handler()
	wrapped := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		handler.ServeHTTP(w, r)
		seen = Route(r)
	})
	rec := httptest.NewRecorder()
	wrapped.ServeHTTP(rec, httptest.NewRequest("GET", "/api/v1/steps/network", nil))
	if seen != "GET /api/v1/steps/{step}" {
		t.Fatalf("route %q", seen)
	}
}
