package cluster

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeBin writes an executable shell script that stands in for kubectl or vws.
func fakeBin(t *testing.T, dir, name, body string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestNodes(t *testing.T) {
	dir := t.TempDir()
	kubectl := fakeBin(t, dir, "kubectl", `[ "$*" = "get nodes -o json" ] || exit 9
echo '{"items":[{"metadata":{"name":"node1","labels":{"node-role.kubernetes.io/control-plane":""}}},{"metadata":{"name":"node2"}}]}'
`)
	nodes, err := VWS{Kubectl: kubectl}.Nodes(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(nodes) != 2 || nodes[0].Name != "node1" || nodes[1].Name != "node2" {
		t.Fatalf("nodes %+v", nodes)
	}
	if _, ok := nodes[0].Labels["node-role.kubernetes.io/control-plane"]; !ok {
		t.Fatal("labels lost")
	}
}

func TestInstallRunsApplyWithTheFile(t *testing.T) {
	dir := t.TempDir()
	calls := filepath.Join(dir, "calls")
	vws := fakeBin(t, dir, "vws", `echo "$*" >> `+calls+`
echo "step $1"
cat "$4" >> `+calls+`
exit 0
`)
	var lines []string
	if _, err := (VWS{VWS: vws, WorkDir: dir}).Install(context.Background(), []byte(`{"kind":"VWSCluster"}`), func(l string) { lines = append(lines, l) }); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(calls)
	want := "apply --unattended -f " + filepath.Join(dir, "vwscluster.json") + "\n{\"kind\":\"VWSCluster\"}"
	if string(got) != want {
		t.Fatalf("calls %q, want %q", got, want)
	}
	if strings.Join(lines, ",") != "step apply" {
		t.Fatalf("log lines %v", lines)
	}
	if fi, _ := os.Stat(filepath.Join(dir, "vwscluster.json")); fi.Mode().Perm() != 0o600 {
		t.Fatalf("CR file mode %v", fi.Mode().Perm())
	}
}

func TestInstallFailsWithApply(t *testing.T) {
	dir := t.TempDir()
	vws := fakeBin(t, dir, "vws", "exit 2\n")
	if _, err := (VWS{VWS: vws, WorkDir: dir}).Install(context.Background(), []byte(`{}`), func(string) {}); err == nil {
		t.Fatal("a failed apply did not fail the install")
	}
}
