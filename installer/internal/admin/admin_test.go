package admin

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"

	"github.com/popododo0720/vws-deploy/installer/internal/api"
)

type patch struct {
	kind, name string
	ops        []map[string]any
}

type fake struct {
	cr       string
	names    map[string][]string
	patches  []patch
	failKind string
}

func (f *fake) Get(_ context.Context, kind, name string) ([]byte, error) {
	if kind != "vwsclusters" || name != "vws" {
		return nil, errors.New("unexpected get " + kind)
	}
	return []byte(f.cr), nil
}

func (f *fake) Names(_ context.Context, kind string) ([]string, error) { return f.names[kind], nil }

func (f *fake) Patch(_ context.Context, kind, name string, raw []byte) error {
	if kind == f.failKind {
		return errors.New("kubectl patch " + kind + ": connection refused")
	}
	var ops []map[string]any
	if err := json.Unmarshal(raw, &ops); err != nil {
		return fmt.Errorf("patch is not JSON: %w", err)
	}
	f.patches = append(f.patches, patch{kind, name, ops})
	return nil
}

func newFake(storage string) *fake {
	return &fake{
		cr: `{"metadata":{"resourceVersion":"7"},"spec":{"storage":` + storage + `}}`,
		names: map[string][]string{
			"storageclasses":        {"ceph-block", "ceph-fs", "local-path"},
			"volumesnapshotclasses": {"ceph-block-snap"},
		},
	}
}

func do(t *testing.T, f *fake, method, host string, form url.Values, hdr map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequestWithContext(t.Context(), method, "http://"+host+"/", strings.NewReader(form.Encode()))
	if form != nil {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	Handler(f).ServeHTTP(rec, req)
	return rec
}

func TestPageShowsEveryTypeAndWhichAreUnmapped(t *testing.T) {
	f := newFake(`{"driver":"rook-ceph","volumeTypes":{"gp3":{"storageClass":"ceph-block","volumeMode":"Block"}}}`)
	rec := do(t, f, "GET", "localhost:8080", nil, nil)
	body := rec.Body.String()
	if rec.Code != http.StatusOK {
		t.Fatalf("GET: %d %s", rec.Code, body)
	}
	for _, typ := range api.EBSVolumeTypes {
		if !strings.Contains(body, `name="`+typ+`.storageClass"`) {
			t.Errorf("page has no row for %s", typ)
		}
	}
	if n := strings.Count(body, `class="unmapped">not mapped`); n != 6 {
		t.Errorf("%d types shown as not mapped, want 6", n)
	}
	if !strings.Contains(body, `value="ceph-block"`) || !strings.Contains(body, `value="7"`) {
		t.Errorf("page lacks the gp3 class or the resourceVersion:\n%s", body)
	}
}

func form(kv ...string) url.Values {
	v := url.Values{"resourceVersion": {"7"}}
	for i := 0; i < len(kv); i += 2 {
		v.Set(kv[i], kv[i+1])
	}
	return v
}

func TestSaveWritesTheCRThenTheConfigMap(t *testing.T) {
	f := newFake(`{"driver":"rook-ceph"}`)
	in := form("gp3.storageClass", "ceph-block", "gp3.volumeMode", "Block",
		"gp3.volumeSnapshotClass", "ceph-block-snap", "st1.storageClass", " local-path ")
	rec := do(t, f, "POST", "127.0.0.1:8080", in, nil)
	if rec.Code != http.StatusSeeOther || rec.Header().Get("Location") != "/?saved=1" {
		t.Fatalf("save: %d %s", rec.Code, rec.Body)
	}
	want := []patch{
		{"vwsclusters", "vws", []map[string]any{
			{"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
			{"op": "add", "path": "/spec/storage/volumeTypes", "value": map[string]any{
				"gp3": map[string]any{
					"storageClass": "ceph-block", "volumeMode": "Block", "volumeSnapshotClass": "ceph-block-snap",
				},
				"st1": map[string]any{"storageClass": "local-path"},
			}},
		}},
		{"configmaps", "vws-volume-types", []map[string]any{
			{"op": "add", "path": "/data", "value": map[string]any{
				"gp3": "ceph-block/Block/ceph-block-snap", "st1": "local-path",
			}},
		}},
	}
	if !reflect.DeepEqual(f.patches, want) {
		t.Fatalf("patches\n%v\nwant\n%v", f.patches, want)
	}

	f.patches = nil
	rec = do(t, f, "POST", "127.0.0.1:8080", in, nil)
	if rec.Code != http.StatusSeeOther || !reflect.DeepEqual(f.patches, want) {
		t.Fatalf("a re-save of the same mapping must write the same patches: %d %v", rec.Code, f.patches)
	}
}

func TestUnmappingEveryTypeRemovesTheField(t *testing.T) {
	f := newFake(`{"driver":"rook-ceph","volumeTypes":{"gp3":{"storageClass":"ceph-block"}}}`)
	if rec := do(t, f, "POST", "localhost", form(), nil); rec.Code != http.StatusSeeOther {
		t.Fatalf("save: %d %s", rec.Code, rec.Body)
	}
	if got := f.patches[0].ops[1]; got["op"] != "remove" || got["path"] != "/spec/storage/volumeTypes" {
		t.Fatalf("CR op %v", got)
	}
	if got := f.patches[1].ops[0]["value"]; !reflect.DeepEqual(got, map[string]any{}) {
		t.Fatalf("ConfigMap data %v, want empty", got)
	}
}

func TestSaveRefusesAndWritesNothing(t *testing.T) {
	cases := []struct {
		name, storage string
		in            url.Values
		status        int
		message       string
	}{
		{
			"missing class", `{"driver":"rook-ceph"}`, form("gp3.storageClass", "gp3-fast"),
			http.StatusBadRequest, "storageclasses gp3-fast does not exist",
		},
		{
			"missing snapshot class", `{"driver":"rook-ceph"}`,
			form("io1.storageClass", "ceph-block", "io1.volumeSnapshotClass", "nope"),
			http.StatusBadRequest, "volumesnapshotclasses nope does not exist",
		},
		{
			"not a DNS name", `{"driver":"rook-ceph"}`, form("gp3.storageClass", "../../etc"),
			http.StatusBadRequest, "not a DNS-1123 name",
		},
		{
			"mode without class", `{"driver":"rook-ceph"}`, form("io2.volumeMode", "Block"),
			http.StatusBadRequest, "io2.storageClass is required",
		},
		{
			"bad mode", `{"driver":"rook-ceph"}`, form("gp3.storageClass", "ceph-block", "gp3.volumeMode", "Raw"),
			http.StatusBadRequest, "Block or Filesystem",
		},
		{
			"nfs", `{"driver":"nfs"}`, form("gp3.storageClass", "ceph-block"),
			http.StatusBadRequest, "not for driver nfs",
		},
		{
			"first Filesystem type", `{"driver":"rook-ceph"}`,
			form("gp3.storageClass", "ceph-fs", "gp3.volumeMode", "Filesystem"),
			http.StatusBadRequest, "vws reconfigure and vws apply",
		},
		{
			"stale page", `{"driver":"rook-ceph"}`,
			url.Values{"resourceVersion": {"6"}, "gp3.storageClass": {"ceph-block"}},
			http.StatusConflict, "changed since this page loaded",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := newFake(c.storage)
			rec := do(t, f, "POST", "localhost:8080", c.in, nil)
			if rec.Code != c.status || !strings.Contains(rec.Body.String(), c.message) {
				t.Fatalf("%d, want %d with %q:\n%s", rec.Code, c.status, c.message, rec.Body)
			}
			if len(f.patches) != 0 {
				t.Fatalf("wrote %v", f.patches)
			}
		})
	}
}

func TestExistingFilesystemTypeCanBeKept(t *testing.T) {
	f := newFake(`{"driver":"rook-ceph","volumeTypes":{"st1":{"storageClass":"ceph-fs","volumeMode":"Filesystem"}}}`)
	in := form("st1.storageClass", "ceph-fs", "st1.volumeMode", "Filesystem", "sc1.storageClass", "ceph-fs",
		"sc1.volumeMode", "Filesystem")
	if rec := do(t, f, "POST", "localhost", in, nil); rec.Code != http.StatusSeeOther {
		t.Fatalf("save: %d %s", rec.Code, rec.Body)
	}
}

func TestCRWriteFailureLeavesTheConfigMap(t *testing.T) {
	f := newFake(`{"driver":"rook-ceph"}`)
	f.failKind = "vwsclusters"
	rec := do(t, f, "POST", "localhost", form("gp3.storageClass", "ceph-block"), nil)
	if rec.Code != http.StatusBadGateway || !strings.Contains(rec.Body.String(), "connection refused") {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	if len(f.patches) != 0 {
		t.Fatalf("ConfigMap written after the CR write failed: %v", f.patches)
	}
}

func TestOnlyLocalSameOriginRequests(t *testing.T) {
	f := newFake(`{"driver":"rook-ceph"}`)
	in := form("gp3.storageClass", "ceph-block")
	for name, rec := range map[string]*httptest.ResponseRecorder{
		"DNS rebinding":  do(t, f, "GET", "evil.example:8080", nil, nil),
		"pod IP":         do(t, f, "POST", "10.0.0.5:8080", in, nil),
		"cross-site":     do(t, f, "POST", "localhost:8080", in, map[string]string{"Sec-Fetch-Site": "cross-site"}),
		"foreign origin": do(t, f, "POST", "localhost:8080", in, map[string]string{"Origin": "https://evil.example"}),
	} {
		if rec.Code != http.StatusForbidden {
			t.Errorf("%s: %d, want 403", name, rec.Code)
		}
	}
	if len(f.patches) != 0 {
		t.Fatalf("wrote %v", f.patches)
	}
}

func TestFlagValueMatchesRender(t *testing.T) {
	for in, want := range map[api.VolumeType]string{
		{StorageClass: "a"}:                                                "a",
		{StorageClass: "a", VolumeMode: "Block"}:                           "a/Block",
		{StorageClass: "a", VolumeSnapshotClass: "s"}:                      "a//s",
		{StorageClass: "a", VolumeMode: "Block", VolumeSnapshotClass: "s"}: "a/Block/s",
	} {
		if got := FlagValue(in); got != want {
			t.Errorf("FlagValue(%+v) = %q, want %q", in, got, want)
		}
	}
}
