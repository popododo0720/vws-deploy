// Package admin serves the VWS admin page for the EBS volume type mapping (vws-deploy#391 AC3,
// docs/design/admin-volume-types.md). It lists spec.storage.volumeTypes of VWSCluster vws, and a
// save writes the CR and ConfigMap vws-volume-types. The page listens on loopback only and is
// reached with kubectl port-forward, so the operator's kube credentials are its auth.
package admin

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"html/template"
	"net"
	"net/http"
	"slices"
	"strings"

	"github.com/popododo0720/vws-deploy/installer/internal/api"
)

// ConfigMap is the object vws-storage reads the mapping from. The chart creates it (R11 gives no
// create by name), so the page only patches it.
const ConfigMap = "vws-volume-types"

// Cluster is the kubectl surface the page uses; cluster.Kube is the real one.
type Cluster interface {
	Get(ctx context.Context, kind, name string) ([]byte, error)
	Names(ctx context.Context, kind string) ([]string, error)
	Patch(ctx context.Context, kind, name string, patch []byte) error
}

// Handler serves GET / (the mapping) and POST / (save).
func Handler(c Cluster) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		v, status := load(r.Context(), c)
		v.Saved = r.URL.Query().Has("saved")
		render(w, status, v)
	})
	mux.HandleFunc("POST /{$}", func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
		if err := r.ParseForm(); err != nil {
			http.Error(w, "bad form", http.StatusBadRequest)
			return
		}
		if v, status := save(r.Context(), c, r.PostForm); status != http.StatusOK {
			render(w, status, v)
			return
		}
		http.Redirect(w, r, "/?saved=1", http.StatusSeeOther)
	})
	return loopbackHost(http.NewCrossOriginProtection().Handler(mux))
}

// loopbackHost refuses a Host other than localhost or a loopback IP. A page on another site
// could otherwise reach the port-forward through DNS rebinding.
func loopbackHost(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		host, _, err := net.SplitHostPort(r.Host)
		if err != nil {
			host = r.Host
		}
		if ip := net.ParseIP(strings.Trim(host, "[]")); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
			http.Error(w, "host must be localhost", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// cr is the part of VWSCluster vws the page reads.
type cr struct {
	Metadata struct {
		ResourceVersion string `json:"resourceVersion"`
	} `json:"metadata"`
	Spec struct {
		Storage api.Storage `json:"storage"`
	} `json:"spec"`
}

// row is one EBS volume type on the page. An empty StorageClass means unmapped.
type row struct {
	Type string
	api.VolumeType
}

type view struct {
	Driver          string
	ResourceVersion string
	Rows            []row
	StorageClasses  []string
	Errors          []string
	Saved           bool
}

func load(ctx context.Context, c Cluster) (view, int) {
	cur, err := current(ctx, c)
	if err != nil {
		return view{Errors: []string{err.Error()}}, http.StatusBadGateway
	}
	v := view{
		Driver: cur.Spec.Storage.Driver, ResourceVersion: cur.Metadata.ResourceVersion,
		Rows: rows(cur.Spec.Storage.VolumeTypes),
	}
	if v.StorageClasses, err = c.Names(ctx, "storageclasses"); err != nil {
		v.Errors = append(v.Errors, err.Error())
	}
	return v, http.StatusOK
}

func current(ctx context.Context, c Cluster) (cr, error) {
	var cur cr
	raw, err := c.Get(ctx, "vwsclusters", "vws")
	if err == nil {
		err = json.Unmarshal(raw, &cur)
	}
	if err != nil {
		return cr{}, fmt.Errorf("VWSCluster vws: %w", err)
	}
	return cur, nil
}

func rows(m map[string]api.VolumeType) []row {
	out := make([]row, 0, len(api.EBSVolumeTypes))
	for _, t := range api.EBSVolumeTypes {
		out = append(out, row{Type: t, VolumeType: m[t]})
	}
	return out
}

// save checks the form and writes VWSCluster vws first, then ConfigMap vws-volume-types. The CR
// is the source: if the ConfigMap write fails, a re-save or the next vws apply converges it.
func save(ctx context.Context, c Cluster, form map[string][]string) (view, int) {
	get := func(key string) string {
		if vals := form[key]; len(vals) > 0 {
			return strings.TrimSpace(vals[0])
		}
		return ""
	}
	want := map[string]api.VolumeType{}
	for _, t := range api.EBSVolumeTypes {
		vt := api.VolumeType{
			StorageClass: get(t + ".storageClass"), VolumeMode: get(t + ".volumeMode"),
			VolumeSnapshotClass: get(t + ".volumeSnapshotClass"),
		}
		if vt != (api.VolumeType{}) {
			want[t] = vt
		}
	}
	cur, err := current(ctx, c)
	if err != nil {
		return view{Errors: []string{err.Error()}}, http.StatusBadGateway
	}
	v := view{Driver: cur.Spec.Storage.Driver, ResourceVersion: cur.Metadata.ResourceVersion, Rows: rows(want)}
	if get("resourceVersion") != cur.Metadata.ResourceVersion {
		v.Errors = []string{"VWSCluster vws changed since this page loaded; reload it and save again"}
		return v, http.StatusConflict
	}
	if v.Errors, err = check(ctx, c, cur.Spec.Storage, want); err != nil {
		v.Errors = append(v.Errors, err.Error())
		return v, http.StatusBadGateway
	}
	if len(v.Errors) > 0 {
		return v, http.StatusBadRequest
	}
	ops := []map[string]any{{"op": "test", "path": "/metadata/resourceVersion", "value": cur.Metadata.ResourceVersion}}
	switch {
	case len(want) > 0:
		ops = append(ops, map[string]any{"op": "add", "path": "/spec/storage/volumeTypes", "value": want})
	case len(cur.Spec.Storage.VolumeTypes) > 0:
		ops = append(ops, map[string]any{"op": "remove", "path": "/spec/storage/volumeTypes"})
	}
	data := map[string]string{}
	for t, vt := range want {
		data[t] = FlagValue(vt)
	}
	for _, w := range []struct {
		kind, name string
		ops        []map[string]any
	}{
		{"vwsclusters", "vws", ops},
		{"configmaps", ConfigMap, []map[string]any{{"op": "add", "path": "/data", "value": data}}},
	} {
		patch, err := json.Marshal(w.ops)
		if err == nil {
			err = c.Patch(ctx, w.kind, w.name, patch)
		}
		if err != nil {
			v.Errors = []string{err.Error()}
			return v, http.StatusBadGateway
		}
	}
	return v, http.StatusOK
}

// check returns what is wrong with want: the CRD rules, classes that do not exist, and a new
// Filesystem type, which needs permission to list datavolumes.
func check(ctx context.Context, c Cluster, cur api.Storage, want map[string]api.VolumeType) ([]string, error) {
	var out []string
	for _, e := range api.CheckVolumeTypes(api.Storage{Driver: cur.Driver, VolumeTypes: want}) {
		out = append(out, e.Field+": "+e.Message)
	}
	if len(out) > 0 {
		return out, nil
	}
	known := map[string][]string{}
	for _, t := range api.EBSVolumeTypes {
		vt, ok := want[t]
		if !ok {
			continue
		}
		for _, ref := range []struct{ kind, name string }{
			{"storageclasses", vt.StorageClass}, {"volumesnapshotclasses", vt.VolumeSnapshotClass},
		} {
			if ref.name == "" {
				continue
			}
			names, ok := known[ref.kind]
			if !ok {
				var err error
				if names, err = c.Names(ctx, ref.kind); err != nil {
					return out, err
				}
				known[ref.kind] = names
			}
			if !slices.Contains(names, ref.name) {
				out = append(out, fmt.Sprintf("spec.storage.volumeTypes.%s: %s %s does not exist", t, ref.kind, ref.name))
			}
		}
	}
	filesystem := func(m map[string]api.VolumeType) bool {
		for _, vt := range m {
			if vt.VolumeMode == "Filesystem" {
				return true
			}
		}
		return false
	}
	if filesystem(want) && !filesystem(cur.VolumeTypes) {
		out = append(out, "a first Filesystem type needs a storage RBAC rule: set it with vws reconfigure and vws apply")
	}
	return out, nil
}

// FlagValue is the vws-storage -kube-volume-type value after TYPE=: CLASS[/MODE[/SNAPSHOTCLASS]]
// with empty trailing fields left out.
func FlagValue(vt api.VolumeType) string {
	fields := []string{vt.StorageClass, vt.VolumeMode, vt.VolumeSnapshotClass}
	for len(fields) > 1 && fields[len(fields)-1] == "" {
		fields = fields[:len(fields)-1]
	}
	return strings.Join(fields, "/")
}

//go:embed page.html
var pageHTML string

var page = template.Must(template.New("page").Parse(pageHTML))

func render(w http.ResponseWriter, status int, v view) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
	w.WriteHeader(status)
	_ = page.Execute(w, v) //nolint:errcheck // the status line is already sent
}
