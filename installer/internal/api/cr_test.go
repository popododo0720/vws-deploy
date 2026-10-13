package api

import (
	"encoding/json"
	"strings"
	"testing"
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
