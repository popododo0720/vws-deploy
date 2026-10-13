package tui

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http/httptest"
	"os"
	"regexp"
	"strings"
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
	var code string
	auth, err := api.NewAuth(time.Now, func(c string) error { code = c; return nil })
	if err != nil {
		t.Fatal(err)
	}
	srv := api.New(api.Config{Auth: auth, Cluster: fc, Preflight: pf, APIVersion: apiVersion(t)})
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
// external nic/cidr/vlan/mtu/gateway, storage driver, eip, profile, confirm.
const happy = "\n" +
	"ens3\n10.0.0.0/24\n\n\n" +
	"\n" +
	"\n" +
	"ens8\n198.18.0.0/24\n\n\n198.18.0.1\n" +
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
	for _, want := range []string{"NIC ens3 mtu 1500 on node1,node2", "minio: 준비 중", "local: 준비 중", "== summary ==", `"eipPool": "198.18.0.128/25"`, "installed. console: https://console.example.invalid", "vws-deploy#146"} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
	if len(fc.crs) != 1 || !strings.Contains(string(fc.crs[0]), `"profile":"small"`) || !strings.Contains(string(fc.crs[0]), `"driver":"rook-ceph"`) {
		t.Fatalf("installed CR %s", fc.crs)
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
