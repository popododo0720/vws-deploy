package cluster

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
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

func TestNodesStatusAndLinks(t *testing.T) {
	dir := t.TempDir()
	kubectl := fakeBin(t, dir, "kubectl", `case "$*" in
"get nodes -o json") echo '{"items":[{"metadata":{"name":"node1"},"status":{"conditions":[{"type":"Ready","status":"True"}],"addresses":[{"type":"InternalIP","address":"10.0.0.11"},{"type":"Hostname","address":"node1"}]}},{"metadata":{"name":"node2"},"status":{"conditions":[{"type":"Ready","status":"False"}],"addresses":[{"type":"InternalIP","address":"10.0.0.12"}]}},{"metadata":{"name":"node3"}}]}' ;;
"get storageclasses -o jsonpath={.items[*].metadata.name}") printf 'ceph-block fast-rbd' ;;
*) exit 9 ;;
esac
`)
	links := filepath.Join(dir, "links.json")
	// talosctl names a node by its address or its name; node3 has no links.
	stream := `{"node":"10.0.0.11","metadata":{"id":"ens3"},"spec":{"mtu":1500}}
{"node":"10.0.0.11","metadata":{"id":"ens8"},"spec":{"mtu":9000}}
{"node":"node2","metadata":{"id":"ens3"},"spec":{"mtu":1500}}`
	if err := os.WriteFile(links, []byte(stream), 0o600); err != nil {
		t.Fatal(err)
	}
	v := VWS{Kubectl: kubectl, Links: links}
	nodes, err := v.Nodes(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !nodes[0].Ready || nodes[1].Ready || nodes[2].Ready {
		t.Fatalf("ready %+v", nodes)
	}
	if strings.Join(nodes[0].InternalIPs, ",") != "10.0.0.11" {
		t.Fatalf("internal IPs %v", nodes[0].InternalIPs)
	}
	if !nodes[0].NICsKnown || len(nodes[0].NICs) != 2 || nodes[0].NICs[1] != (NIC{Name: "ens8", MTU: 9000}) {
		t.Fatalf("node1 NICs %+v", nodes[0])
	}
	if !nodes[1].NICsKnown || len(nodes[1].NICs) != 1 || nodes[2].NICsKnown {
		t.Fatalf("node2/node3 NICs %+v %+v", nodes[1], nodes[2])
	}
	classes, err := v.StorageClasses(context.Background())
	if err != nil || strings.Join(classes, ",") != "ceph-block,fast-rbd" {
		t.Fatalf("classes %v %v", classes, err)
	}
	if err := os.WriteFile(links, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := v.Nodes(context.Background()); err == nil {
		t.Fatal("a broken links file was accepted")
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

func TestInstallSurvivesALineLongerThanTheScanner(t *testing.T) {
	dir := t.TempDir()
	// 2 MiB on one line, over maxLogLine, then more output: a stopped reader would block the child on the pipe.
	vws := fakeBin(t, dir, "vws", `head -c 2097152 /dev/zero | tr '\0' x; echo; echo after; i=0; while [ $i -lt 2000 ]; do echo line $i; i=$((i+1)); done
`)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	var lines []string
	if _, err := (VWS{VWS: vws, WorkDir: dir}).Install(ctx, []byte(`{}`), func(l string) { lines = append(lines, l) }); err != nil {
		t.Fatalf("install: %v", err)
	}
	if ctx.Err() != nil {
		t.Fatal("install hung on a long output line")
	}
	if len(lines) != 1 || !strings.Contains(lines[0], "output not shown") {
		t.Fatalf("want one notice about the dropped output, got %d lines", len(lines))
	}
}
