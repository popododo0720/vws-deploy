package api

import (
	"encoding/json"
	"errors"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func (h *harness) exportCR() map[string]any {
	h.t.Helper()
	status, body := h.do("GET", "/api/v1/cr", nil)
	if status != 200 {
		h.t.Fatalf("GET /cr: %d %v", status, body)
	}
	return body
}

func TestExportNeedsEveryStep(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	status, body := h.do("GET", "/api/v1/cr", nil)
	h.wantError(status, body, 409, ReasonStepOutOfOrder)
}

func TestCRRoundTrip(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	cr := h.exportCR()
	hash := h.summary()
	spec := cr["spec"].(map[string]any)
	if spec["network"].(map[string]any)["nodes"].(map[string]any)["external"].(map[string]any)["eipPool"] != "198.18.0.128/25" {
		t.Fatalf("exported CR %v", cr)
	}

	// A fresh installer given the exported file reaches the same summary without the wizard.
	other := newHarness(t, nil)
	other.login()
	status, body := other.do("POST", "/api/v1/cr", cr)
	if status != 200 || body["state"] != "editing" {
		t.Fatalf("POST /cr: %d %v", status, body)
	}
	for _, s := range body["steps"].([]any) {
		if s.(map[string]any)["status"] != "valid" {
			t.Fatalf("steps after POST /cr %v", body["steps"])
		}
	}
	if got := other.summary(); got != hash {
		t.Fatalf("summary hash %s after POST /cr, want %s", got, hash)
	}
	_, eip := other.do("GET", "/api/v1/steps/eip", nil)
	if string(mustJSON(t, eip["value"])) != `"198.18.0.128/25"` {
		t.Fatalf("eip step value %v", eip["value"])
	}
	// Posting the same CR again is idempotent: same steps, same hash.
	if status, _ := other.do("POST", "/api/v1/cr", cr); status != 200 {
		t.Fatal("second POST /cr")
	}
	if got := other.summary(); got != hash {
		t.Fatalf("second POST /cr changed the hash to %s", got)
	}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestPostCRRejects(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	good := h.exportCR()
	cases := []struct {
		name   string
		change func(cr map[string]any)
		field  string
		code   string
	}{
		{"wrong apiVersion", func(cr map[string]any) { cr["apiVersion"] = "example.com/v1" }, "apiVersion", "invalid"},
		{"wrong kind", func(cr map[string]any) { cr["kind"] = "Other" }, "kind", "invalid"},
		{"wrong name", func(cr map[string]any) { cr["metadata"] = map[string]any{"name": "x"} }, "metadata.name", "invalid"},
		{"services override", func(cr map[string]any) {
			cr["spec"].(map[string]any)["services"] = map[string]any{"network": map[string]any{"flags": map[string]any{"pool": "198.18.0.0/24"}}}
		}, "spec.services", "unsupported"},
		{"no eip pool", func(cr map[string]any) {
			delete(cr["spec"].(map[string]any)["network"].(map[string]any)["nodes"].(map[string]any)["external"].(map[string]any), "eipPool")
		}, "spec.network.nodes.external.eipPool", "required"},
		{"no profile", func(cr map[string]any) { delete(cr["spec"].(map[string]any), "profile") }, "spec.profile", "required"},
		{"bad storage", func(cr map[string]any) { cr["spec"].(map[string]any)["storage"] = map[string]any{"driver": "minio"} }, "spec.storage.driver", "not_available"},
	}
	for _, c := range cases {
		cr := map[string]any{}
		_ = json.Unmarshal(mustJSON(t, good), &cr)
		c.change(cr)
		status, body := h.do("POST", "/api/v1/cr", cr)
		e := h.wantError(status, body, 400, ReasonValidationFailed)
		found := false
		for _, f := range e["fields"].([]any) {
			m := f.(map[string]any)
			found = found || (m["field"] == c.field && m["code"] == c.code)
		}
		if !found {
			t.Fatalf("%s: fields %v, want %s %s", c.name, e["fields"], c.field, c.code)
		}
	}
	status, body := h.do("POST", "/api/v1/cr", `{"spec": 1}`)
	h.wantError(status, body, 400, ReasonValidationFailed)
	status, body = h.do("POST", "/api/v1/cr", `not json`)
	h.wantError(status, body, 400, ReasonBadRequest)
	if !strings.Contains(h.state(), "editing") {
		t.Fatal("POST /cr left editing")
	}
}

func TestPostCRWhileInstalling(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	cr := h.exportCR()
	hash := h.summary()
	if status, _ := h.do("POST", "/api/v1/install", map[string]any{"confirm": true, "specHash": hash}); status != 202 {
		t.Fatal("install")
	}
	status, body := h.do("POST", "/api/v1/cr", cr)
	h.wantError(status, body, 409, ReasonInstallInProgress)
	h.cluster.release <- nil
	h.waitState("succeeded")
	status, body = h.do("POST", "/api/v1/cr", cr)
	h.wantError(status, body, 409, ReasonAlreadyInstalled)
}

// A blocked node lookup must not hold the server lock: an unrelated request answers while
// POST /cr waits for the lookup.
func TestPostCRNodeLookupDoesNotHoldLock(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	body := string(mustJSON(t, h.exportCR()))

	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) }
	t.Cleanup(unblock)
	h.cluster.nodesStarted, h.cluster.nodesRelease = started, release

	handler := h.srv.Handler()
	post := make(chan int, 1)
	go func() {
		req := httptest.NewRequestWithContext(t.Context(), "POST", "/api/v1/cr", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+h.token)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		post <- rec.Code
	}()
	<-started

	get := make(chan int, 1)
	go func() {
		req := httptest.NewRequestWithContext(t.Context(), "GET", "/api/v1/state", nil)
		req.Header.Set("Authorization", "Bearer "+h.token)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		get <- rec.Code
	}()
	select {
	case status := <-get:
		if status != 200 {
			t.Fatalf("GET /state during node lookup: %d", status)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("GET /state blocked while POST /cr looked up nodes")
	}

	unblock()
	if status := <-post; status != 200 {
		t.Fatalf("POST /cr: %d", status)
	}
}

func TestPostCRValidatesBeforeNodeLookup(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	good := h.exportCR()
	badStorage := map[string]any{}
	if err := json.Unmarshal(mustJSON(t, good), &badStorage); err != nil {
		t.Fatal(err)
	}
	spec, ok := badStorage["spec"].(map[string]any)
	if !ok {
		t.Fatal("exported CR has no spec object")
	}
	spec["storage"] = map[string]any{"driver": "minio"}
	h.cluster.nodesErr = errors.New("kube-apiserver is unavailable")

	for _, c := range []struct {
		name string
		body any
	}{
		{"not a CR", `{"spec": 1}`},
		{"bad step value", badStorage},
	} {
		status, body := h.do("POST", "/api/v1/cr", c.body)
		h.wantError(status, body, 400, ReasonValidationFailed)
		if strings.Contains(h.bodies.String(), "kube-apiserver is unavailable") {
			t.Fatalf("%s: node lookup error reached the response", c.name)
		}
	}
}

func fieldMessage(e map[string]any, field string) string {
	fields, ok := e["fields"].([]any)
	if !ok {
		return ""
	}
	var msgs []string
	for _, f := range fields {
		m, ok := f.(map[string]any)
		if !ok || m["field"] != field {
			continue
		}
		s, ok := m["message"].(string)
		if ok {
			msgs = append(msgs, s)
		}
	}
	return strings.Join(msgs, "\n")
}

func TestPostCRReportsJSONTypeErrors(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	good := h.exportCR()
	cases := []struct {
		name  string
		field string
		set   func(cr map[string]any)
		want  string
		clean string
	}{
		{
			name: "apiVersion number", field: "apiVersion", want: "cannot unmarshal number", clean: "kind",
			set: func(cr map[string]any) { cr["apiVersion"] = 1 },
		},
		{
			name: "kind array", field: "kind", want: "cannot unmarshal array", clean: "apiVersion",
			set: func(cr map[string]any) { cr["kind"] = []any{"VWSCluster"} },
		},
		{
			name: "metadata number", field: "metadata", want: "cannot unmarshal number", clean: "apiVersion",
			set: func(cr map[string]any) { cr["metadata"] = 1 },
		},
		{
			name: "apiVersion object", field: "apiVersion", want: "cannot unmarshal object", clean: "kind",
			set: func(cr map[string]any) { cr["apiVersion"] = map[string]any{"injected": true} },
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cr := map[string]any{}
			if err := json.Unmarshal(mustJSON(t, good), &cr); err != nil {
				t.Fatal(err)
			}
			c.set(cr)
			var first string
			for range 2 {
				status, body := h.do("POST", "/api/v1/cr", cr)
				if status != 400 {
					t.Fatalf("status %d body %v", status, body)
				}
				e := h.wantError(status, body, 400, ReasonValidationFailed)
				msg := fieldMessage(e, c.field)
				if !strings.Contains(msg, c.want) {
					t.Fatalf("field %s message %q, want %q; fields %v", c.field, msg, c.want, e["fields"])
				}
				if clean := fieldMessage(e, c.clean); clean != "" {
					t.Fatalf("valid %s was rejected because %s failed to decode: %q", c.clean, c.field, clean)
				}
				if first == "" {
					first = msg
				} else if msg != first {
					t.Fatalf("second POST changed the error from %q to %q", first, msg)
				}
			}
		})
	}
	if h.state() != "editing" {
		t.Fatal("malformed POST /cr left editing")
	}
}

func TestPostCRReportsSpecDecodeError(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	good := h.exportCR()
	cr := map[string]any{}
	if err := json.Unmarshal(mustJSON(t, good), &cr); err != nil {
		t.Fatal(err)
	}
	cr["spec"] = 1
	status, body := h.do("POST", "/api/v1/cr", cr)
	e := h.wantError(status, body, 400, ReasonValidationFailed)
	fields, ok := e["fields"].([]any)
	if !ok || len(fields) != 1 {
		t.Fatalf("fields %v, want one spec error", e["fields"])
	}
	f, ok := fields[0].(map[string]any)
	if !ok || f["field"] != "spec" || f["code"] != "invalid" {
		t.Fatalf("field %v, want spec invalid", fields[0])
	}
	msg, ok := f["message"].(string)
	if !ok || !strings.Contains(msg, "cannot unmarshal number") {
		t.Fatalf("spec message %q, want the JSON error", msg)
	}
	// A missing spec is a required field, not a decode error.
	delete(cr, "spec")
	status, body = h.do("POST", "/api/v1/cr", cr)
	e = h.wantError(status, body, 400, ReasonValidationFailed)
	if msg := fieldMessage(e, "spec"); msg != "spec is required" {
		t.Fatalf("missing spec message %q", msg)
	}
}

func crNetwork(t *testing.T, cr map[string]any) map[string]any {
	t.Helper()
	spec, ok := cr["spec"].(map[string]any)
	if !ok {
		t.Fatal("exported CR has no spec object")
	}
	network, ok := spec["network"].(map[string]any)
	if !ok {
		t.Fatal("exported CR has no network object")
	}
	return network
}

func crNodes(t *testing.T, cr map[string]any) map[string]any {
	t.Helper()
	nodes, ok := crNetwork(t, cr)["nodes"].(map[string]any)
	if !ok {
		t.Fatal("exported CR has no nodes object")
	}
	return nodes
}

func TestPostCRReportsNonObjectNetworkNodes(t *testing.T) {
	h := newHarness(t, nil)
	h.login()
	h.fill()
	good := h.exportCR()
	cases := []struct {
		name  string
		field string
		set   func(cr map[string]any)
	}{
		{
			name: "nodes string", field: "spec.network.nodes",
			set: func(cr map[string]any) { crNetwork(t, cr)["nodes"] = "nope" },
		},
		{
			name: "nodes null", field: "spec.network.nodes",
			set: func(cr map[string]any) { crNetwork(t, cr)["nodes"] = nil },
		},
		{
			name: "external string", field: "spec.network.nodes.external",
			set: func(cr map[string]any) { crNodes(t, cr)["external"] = "nope" },
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cr := map[string]any{}
			if err := json.Unmarshal(mustJSON(t, good), &cr); err != nil {
				t.Fatal(err)
			}
			c.set(cr)
			status, body := h.do("POST", "/api/v1/cr", cr)
			e := h.wantError(status, body, 400, ReasonValidationFailed)
			msg := fieldMessage(e, c.field)
			if !strings.Contains(msg, "object") {
				t.Fatalf("field %s message %q, want a non-object error; fields %v", c.field, msg, e["fields"])
			}
		})
	}
	if h.state() != "editing" {
		t.Fatal("malformed POST /cr left editing")
	}
}

func TestSplitCRReportsInvalidRaw(t *testing.T) {
	const ver = "demo.example/v1"
	cr := map[string]json.RawMessage{
		"apiVersion": json.RawMessage(`not-json`),
		"kind":       json.RawMessage(`"VWSCluster"`),
		"metadata":   json.RawMessage(`{"name":"vws"}`),
		"spec":       json.RawMessage(`{"profile":"small"}`),
	}
	_, errs := splitCR(cr, ver, json.RawMessage(`{"nodes":["node1"]}`))
	var apiMsg, kindMsg string
	for _, e := range errs {
		switch e.Field {
		case "apiVersion":
			apiMsg = e.Message
		case "kind":
			kindMsg = e.Message
		}
	}
	if !strings.Contains(apiMsg, "invalid character") {
		t.Fatalf("apiVersion message %q", apiMsg)
	}
	if kindMsg != "" {
		t.Fatalf("kind was rejected because apiVersion did not decode: %q", kindMsg)
	}
}
