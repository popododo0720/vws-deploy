package tui

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
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

	"github.com/popododo0720/vws-deploy/installer/internal/api"
	"github.com/popododo0720/vws-deploy/installer/internal/client"
	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

type fakeCluster struct {
	nodes []cluster.Node
	err   error
	crs   [][]byte
}

func (f *fakeCluster) Nodes(context.Context) ([]cluster.Node, error) { return f.nodes, nil }
func (f *fakeCluster) Install(_ context.Context, cr []byte, _ func(string)) (cluster.Result, error) {
	f.crs = append(f.crs, cr)
	return cluster.Result{ConsoleURL: "https://console.example.invalid", AdminUser: "admin"}, f.err
}

func apiVersion(t *testing.T) string {
	t.Helper()
	crd, err := os.ReadFile("../../../charts/vws/crds/vwsclusters.yaml")
	if err != nil {
		t.Fatal(err)
	}
	g := regexp.MustCompile(`(?m)^  group: (\S+)$`).FindSubmatch(crd)
	v := regexp.MustCompile(`(?m)^    - name: (\S+)$`).FindSubmatch(crd)
	return string(g[1]) + "/" + string(v[1])
}

// serve starts the real API on TLS against a fake cluster and returns a logged-in client
// pinned to the test certificate.
func serve(t *testing.T, fc *fakeCluster, pf api.Preflight) (*client.Client, *api.Server) {
	t.Helper()
	return serveCreds(t, fc, pf, nil)
}

// serveCreds is serve with a credential store (#146); nil hands no password out.
func serveCreds(
	t *testing.T, fc *fakeCluster, pf api.Preflight, creds cluster.Credentials,
) (*client.Client, *api.Server) {
	t.Helper()
	var code string
	auth, err := api.NewAuth(time.Now, func(c string) error { code = c; return nil })
	if err != nil {
		t.Fatal(err)
	}
	cfg := api.Config{Auth: auth, Cluster: fc, Preflight: pf, APIVersion: apiVersion(t)}
	if creds != nil {
		cfg.Credentials = creds
	}
	srv := api.New(cfg)
	hs := httptest.NewTLSServer(srv.Handler())
	t.Cleanup(func() { srv.Wait(); hs.Close() })
	sum := sha256.Sum256(hs.Certificate().Raw)
	c := &client.Client{Base: hs.URL, HTTP: client.PinnedHTTP(hex.EncodeToString(sum[:]))}
	if err := c.Login(context.Background(), code); err != nil {
		t.Fatal(err)
	}
	return c, srv
}

var nodes = []cluster.Node{
	{Name: "node1", NICs: []cluster.NIC{{Name: "ens3", MTU: 1500}, {Name: "ens8", MTU: 1500}}},
	{Name: "node2", NICs: []cluster.NIC{{Name: "ens3", MTU: 1500}}},
}

// answers for: roles, management nic/cidr/vlan/mtu, tunnel skip, storage public skip,
// external nic/cidr/vlan/mtu/gateway, storage driver, volume types skip, eip, profile, confirm.
const happy = "\n" +
	"ens3\n10.0.0.0/24\n\n\n" +
	"\n" +
	"\n" +
	"ens8\n198.18.0.0/24\n\n\n198.18.0.1\n" +
	"\n" +
	"\n" +
	"198.18.0.128/25\n" +
	"\n" +
	"yes\n"

func run(t *testing.T, c *client.Client, input string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	tu := New(c, strings.NewReader(input), &out)
	tu.Poll = time.Millisecond
	err := tu.Run(context.Background())
	return out.String(), err
}

func TestWizardInstalls(t *testing.T) {
	fc := &fakeCluster{nodes: nodes}
	c, _ := serve(t, fc, nil)
	out, err := run(t, c, happy)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	for _, want := range []string{
		"NIC ens3 mtu 1500 on node1,node2", "minio: Coming soon", "local: Coming soon", "== summary ==",
		`"eipPool": "198.18.0.128/25"`, "installed. console: https://console.example.invalid",
		"hands out no initial admin password",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
	if len(fc.crs) != 1 || !strings.Contains(string(fc.crs[0]), `"profile":"small"`) || !strings.Contains(string(fc.crs[0]), `"driver":"rook-ceph"`) {
		t.Fatalf("installed CR %s", fc.crs)
	}
}

// The storage step maps EBS volume types (#391): an empty StorageClass leaves a type unmapped, the
// TUI names every unmapped type, and the installed CR carries only the mapped ones.
func TestWizardMapsVolumeTypes(t *testing.T) {
	fc := &fakeCluster{nodes: nodes}
	c, _ := serve(t, fc, nil)
	// yes, standard and gp2 unmapped, gp3 mapped with mode and snapshot class, io1 mapped with
	// defaults, io2/st1/sc1 unmapped.
	types := "yes\n" + "\n\n" + "ceph-block\nBlock\nceph-snap\n" + "fast-rbd\n\n\n" + "\n\n\n"
	input := strings.Replace(happy, "198.18.0.1\n\n\n", "198.18.0.1\n\n"+types, 1)
	out, err := run(t, c, input)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	for _, want := range []string{"gp3      ceph-block", "io1      fast-rbd", "standard unmapped", "sc1      unmapped"} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
	if len(fc.crs) != 1 {
		t.Fatalf("installed %d CRs", len(fc.crs))
	}
	var cr struct {
		Spec struct {
			Storage struct {
				VolumeTypes map[string]map[string]string `json:"volumeTypes"`
			} `json:"storage"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(fc.crs[0], &cr); err != nil {
		t.Fatal(err)
	}
	want := map[string]map[string]string{
		"gp3": {"storageClass": "ceph-block", "volumeMode": "Block", "volumeSnapshotClass": "ceph-snap"},
		"io1": {"storageClass": "fast-rbd"},
	}
	if !reflect.DeepEqual(cr.Spec.Storage.VolumeTypes, want) {
		t.Fatalf("volumeTypes %v, want %v", cr.Spec.Storage.VolumeTypes, want)
	}
}

func TestWizardLeavesVolumeTypesOut(t *testing.T) {
	fc := &fakeCluster{nodes: nodes}
	c, _ := serve(t, fc, nil)
	out, err := run(t, c, happy)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	if !strings.Contains(out, "EBS volume types: not mapped") || len(fc.crs) != 1 ||
		strings.Contains(string(fc.crs[0]), "volumeTypes") {
		t.Fatalf("skipping the mapping changed the CR %s:\n%s", fc.crs, out)
	}
}

func TestWizardReasksOnFieldErrors(t *testing.T) {
	c, _ := serve(t, &fakeCluster{nodes: nodes}, nil)
	// The first eip is outside the external network; the API answers validation_failed and the
	// TUI asks the step again.
	input := strings.Replace(happy, "198.18.0.128/25\n", "10.9.0.0/24\n198.18.0.128/25\n", 1)
	out, err := run(t, c, input)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	if !strings.Contains(out, "! spec.network.nodes.external.eipPool: the pool is outside the external network") {
		t.Fatalf("no field error shown:\n%s", out)
	}
	if strings.Count(out, "== eip ==") != 2 {
		t.Fatalf("eip was not asked again:\n%s", out)
	}
}

func TestWizardShowsPreflight(t *testing.T) {
	pf := func(_ context.Context, step string, _ any, _ []cluster.Node) []api.Check {
		if step == "network" {
			return []api.Check{{ID: "nic.mtu", Severity: "warning", Status: "fail", Node: "node2", Message: "mtu differs", Hint: "set mtu"}}
		}
		return nil
	}
	c, _ := serve(t, &fakeCluster{nodes: nodes}, pf)
	out, err := run(t, c, happy)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	for _, want := range []string{"[warning/fail] nic.mtu node2 mtu differs", "hint: set mtu", "warning [warning/fail] nic.mtu mtu differs"} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
}

func TestWizardShowsUnknownAsCannotVerify(t *testing.T) {
	pf := func(_ context.Context, step string, _ any, _ []cluster.Node) []api.Check {
		if step == "network" {
			return []api.Check{{
				ID: "nic.links", Severity: "warning", Status: "unknown", Node: "node2",
				Message: "NICs and MTUs are not verified: no links for this node",
			}}
		}
		return nil
	}
	c, _ := serve(t, &fakeCluster{nodes: nodes}, pf)
	out, err := run(t, c, happy)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	for _, want := range []string{
		"[warning/CANNOT VERIFY] nic.links node2 NICs and MTUs",
		"warning [warning/CANNOT VERIFY] nic.links NICs and MTUs",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "/unknown]") {
		t.Fatalf("unknown is shown as a raw status:\n%s", out)
	}
}

func TestDeclineInstallsNothing(t *testing.T) {
	fc := &fakeCluster{nodes: nodes}
	c, _ := serve(t, fc, nil)
	_, err := run(t, c, strings.Replace(happy, "yes\n", "no\n", 1))
	if !errors.Is(err, ErrAborted) || len(fc.crs) != 0 {
		t.Fatalf("decline: %v, %d installs", err, len(fc.crs))
	}
	// Input that ends early aborts too.
	_, err = run(t, c, "\n")
	if !errors.Is(err, ErrAborted) {
		t.Fatalf("short input: %v", err)
	}
}

func TestFailedInstallReports(t *testing.T) {
	c, _ := serve(t, &fakeCluster{nodes: nodes, err: errors.New("helm failed")}, nil)
	out, err := run(t, c, happy)
	if err == nil || !strings.Contains(err.Error(), "install failed") {
		t.Fatalf("failed install: %v\n%s", err, out)
	}
}

func TestPinRejectsAnotherCertificate(t *testing.T) {
	var code string
	auth, _ := api.NewAuth(time.Now, func(c string) error { code = c; return nil })
	hs := httptest.NewTLSServer(api.New(api.Config{Auth: auth, Cluster: &fakeCluster{}, APIVersion: "x/v1"}).Handler())
	defer hs.Close()
	c := &client.Client{Base: hs.URL, HTTP: client.PinnedHTTP(strings.Repeat("0", 64))}
	if err := c.Login(context.Background(), code); err == nil || !strings.Contains(err.Error(), "fingerprint") {
		t.Fatalf("a wrong pin logged in: %v", err)
	}
}

func TestClientErrorModel(t *testing.T) {
	c, _ := serve(t, &fakeCluster{nodes: nodes}, nil)
	err := c.Do(context.Background(), "PUT", "/steps/network", map[string]any{"value": map[string]any{}}, nil)
	var e *client.Error
	if !errors.As(err, &e) || e.Status != 409 || e.Code != "step_out_of_order" || e.Message == "" {
		t.Fatalf("error %#v", err)
	}
	err = c.Do(context.Background(), "PUT", "/steps/roles", map[string]any{"value": map[string]any{"nodes": []string{"nodeX"}}}, nil)
	if !errors.As(err, &e) || e.Code != "validation_failed" || len(e.Fields) != 1 || e.Fields[0].Field != "roles.nodes[0]" {
		t.Fatalf("validation error %#v", err)
	}
}

func TestMalformedStepJSON(t *testing.T) {
	cases := []struct {
		name   string
		r      stepResult
		ask    func(*TUI, stepResult) (any, error)
		want   string
		prompt string
	}{
		{
			name: "roles defaults",
			r:    stepResult{Defaults: json.RawMessage(`[]`)},
			ask:  askRoles, want: "cannot unmarshal array", prompt: "nodes (comma separated)",
		},
		{
			name: "network options",
			r:    stepResult{Options: json.RawMessage(`"ens3"`)},
			ask:  askNetwork, want: "cannot unmarshal string", prompt: "management NIC",
		},
		{
			name: "storage options",
			r:    stepResult{Options: json.RawMessage(`1`)},
			ask:  askStorage, want: "cannot unmarshal number", prompt: "storage driver",
		},
		{
			name: "storage defaults",
			r:    stepResult{Options: json.RawMessage(`{"drivers":[]}`), Defaults: json.RawMessage(`[]`)},
			ask:  askStorage, want: "cannot unmarshal array", prompt: "storage driver",
		},
		{
			name: "profile default",
			r:    stepResult{Defaults: json.RawMessage(`{}`)},
			ask:  askProfile, want: "cannot unmarshal object", prompt: "profile",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var out bytes.Buffer
			tu := New(nil, strings.NewReader("node1\n"), &out)
			got, err := c.ask(tu, c.r)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("error %v, value %#v, want substring %q\nout:\n%s", err, got, c.want, out.String())
			}
			if strings.Contains(out.String(), c.prompt) {
				t.Fatalf("prompted %q after a JSON error:\n%s", c.prompt, out.String())
			}
		})
	}
}

func TestSummaryReportsMissingCR(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/summary" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if _, err := io.WriteString(w, `{"specHash":"abc","warnings":[]}`); err != nil {
			t.Errorf("write summary: %v", err)
		}
	}))
	t.Cleanup(srv.Close)
	c := &client.Client{Base: srv.URL, HTTP: srv.Client()}
	var out bytes.Buffer
	tu := New(c, strings.NewReader("yes\n"), &out)
	err := tu.confirmAndInstall(context.Background())
	if err == nil || !strings.Contains(err.Error(), "unexpected end of JSON input") {
		t.Fatalf("err %v\nout:\n%s", err, out.String())
	}
	if strings.Contains(out.String(), "install this?") {
		t.Fatalf("asked to install a summary with no CR:\n%s", out.String())
	}
}

func TestAttachmentReportsBadNumber(t *testing.T) {
	cases := []struct {
		name  string
		input string
		want  string
	}{
		{"vlan", "ens3\n10.0.0.0/24\nnope\n", "VLAN"},
		{"mtu", "ens3\n10.0.0.0/24\n0\nbogus\n", "MTU"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var out bytes.Buffer
			tu := New(nil, strings.NewReader(c.input), &out)
			got, err := tu.attachment("management", true, false)
			if err == nil || !strings.Contains(err.Error(), c.want) || !strings.Contains(err.Error(), "invalid syntax") {
				t.Fatalf("error %v, value %#v\nout:\n%s", err, got, out.String())
			}
		})
	}
}

// credStore stands in for Secret vws-initial-admin. loadFails makes that many Loads fail first.
type credStore struct {
	mu             sync.Mutex
	user, password string
	consumed       bool
	loadFails      int
	loads          int
}

func (f *credStore) Store(_ context.Context, user, pw string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.password != "" {
		return cluster.ErrCredentialExists
	}
	f.user, f.password = user, pw
	return nil
}

func (f *credStore) Load(context.Context) (string, string, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.loads++
	if f.loads <= f.loadFails {
		return "", "", false, errors.New("apiserver unavailable")
	}
	return f.user, f.password, f.consumed, nil
}

func (f *credStore) MarkConsumed(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.consumed = true
	return nil
}

func (f *credStore) secret() (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.password, f.consumed
}

// After the install the TUI fetches the password once from the real API, shows it on the
// terminal with a shown-once notice, and the server then refuses a second hand-out with 410.
func TestWizardHandsOutCredentialOnce(t *testing.T) {
	cs := &credStore{}
	c, _ := serveCreds(t, &fakeCluster{nodes: nodes}, nil, cs)
	out, err := run(t, c, happy)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	pw, consumed := cs.secret()
	if len(pw) != api.PasswordLength || !consumed {
		t.Fatalf("secret password %d chars, consumed %v", len(pw), consumed)
	}
	for _, want := range []string{
		"(shown once)", "user:     admin", "password: " + pw, "Secret vws-initial-admin", "vws installer finish",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
	if strings.Count(out, pw) != 1 {
		t.Fatalf("password shown %d times:\n%s", strings.Count(out, pw), out)
	}
	var apiErr *client.Error
	err = c.Do(context.Background(), http.MethodPost, "/result/credential", nil, nil)
	if !errors.As(err, &apiErr) || apiErr.Status != http.StatusGone {
		t.Fatalf("second hand-out: %v", err)
	}
}

// A Secret that was already consumed (another client took the hand-out) gives 410; the TUI
// points to the Secret, shows no password, and the install still counts as done.
func TestWizardCredentialConsumed(t *testing.T) {
	cs := &credStore{user: "admin", password: "already-taken-password-0", consumed: true}
	c, _ := serveCreds(t, &fakeCluster{nodes: nodes}, nil, cs)
	out, err := run(t, c, happy)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	if !strings.Contains(out, "already handed out; it is in Secret vws-initial-admin") ||
		strings.Contains(out, "already-taken-password-0") || strings.Contains(out, "shown once") {
		t.Fatalf("output:\n%s", out)
	}
}

// A 500 from the hand-out (the Secret could not be read) is retried; the password is shown once.
func TestWizardCredentialRetries500(t *testing.T) {
	cs := &credStore{loadFails: 2}
	c, _ := serveCreds(t, &fakeCluster{nodes: nodes}, nil, cs)
	out, err := run(t, c, happy)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	pw, _ := cs.secret()
	if strings.Count(out, "retrying") != 2 || strings.Count(out, pw) != 1 ||
		strings.Contains(out, "apiserver unavailable") {
		t.Fatalf("output:\n%s", out)
	}
}

// A 500 that does not clear stops after a bounded number of attempts and names the Secret.
func TestWizardCredentialGivesUp(t *testing.T) {
	cs := &credStore{loadFails: 100}
	c, _ := serveCreds(t, &fakeCluster{nodes: nodes}, nil, cs)
	out, err := run(t, c, happy)
	if err == nil || !strings.Contains(err.Error(), "4 attempts failed") ||
		!strings.Contains(err.Error(), "vws-initial-admin") {
		t.Fatalf("err %v\n%s", err, out)
	}
	if cs.loads != 4 {
		t.Fatalf("loads %d", cs.loads)
	}
}

// A 409 (no succeeded install) is an error, not a retry.
func TestCredentialConflictIsError(t *testing.T) {
	c, _ := serveCreds(t, &fakeCluster{nodes: nodes}, nil, &credStore{})
	var out bytes.Buffer
	tu := New(c, strings.NewReader(""), &out)
	tu.Poll = time.Millisecond
	err := tu.credential(context.Background())
	var apiErr *client.Error
	if !errors.As(err, &apiErr) || apiErr.Status != http.StatusConflict ||
		strings.Contains(out.String(), "retrying") {
		t.Fatalf("err %v\n%s", err, out.String())
	}
}
