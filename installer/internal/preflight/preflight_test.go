package preflight

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/popododo0720/vws-deploy/installer/internal/api"
	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

func find(checks []api.Check, id, node string) *api.Check {
	for i := range checks {
		if checks[i].ID == id && checks[i].Node == node {
			return &checks[i]
		}
	}
	return nil
}

func want(t *testing.T, checks []api.Check, id, node, status string, blocks bool) *api.Check {
	t.Helper()
	c := find(checks, id, node)
	if c == nil {
		t.Fatalf("no check %s on %q in %+v", id, node, checks)
	}
	if c.Status != status || c.Blocks() != blocks || c.Message == "" {
		t.Fatalf("check %s on %q: %+v, want status %s blocks %v", id, node, *c, status, blocks)
	}
	return c
}

var engine = Engine{StorageClasses: func(context.Context) ([]string, error) { return []string{"fast-rbd"}, nil }}

func TestRolesReady(t *testing.T) {
	nodes := []cluster.Node{{Name: "a", Ready: true}, {Name: "b"}}
	got := engine.Run(context.Background(), "roles", api.Roles{Nodes: []string{"a", "b"}}, nodes)
	want(t, got, "node.ready", "a", "pass", false)
	want(t, got, "node.ready", "b", "fail", true)
}

func TestProfileNodes(t *testing.T) {
	three := []cluster.Node{{Name: "a", Ready: true}, {Name: "b", Ready: true}, {Name: "c", Ready: true}, {Name: "d"}}
	want(t, engine.Run(context.Background(), "profile", "small", three), "profile.nodes", "", "pass", false)
	c := want(t, engine.Run(context.Background(), "profile", "ha", three), "profile.nodes", "", "fail", true)
	if !strings.Contains(c.Message, "3 Ready nodes, profile ha needs 6") {
		t.Fatalf("message %q", c.Message)
	}
}

func net(external bool, vlan, mtu int) api.Network {
	n := api.Network{Driver: "kube-ovn", Nodes: &api.NodeNets{
		Management: &api.Attachment{NIC: "ens3", CIDR: "10.0.0.0/24"},
		Tunnel:     &api.Attachment{NIC: "ens3", VLAN: vlan, CIDR: "10.1.0.0/24", MTU: mtu},
	}}
	if external {
		n.Nodes.External = &api.Attachment{NIC: "ens8", VLAN: 30, CIDR: "198.18.0.0/24", Gateway: "198.18.0.1"}
	}
	return n
}

func TestNodeIPInsideManagement(t *testing.T) {
	nodes := []cluster.Node{{Name: "a", InternalIPs: []string{"10.0.0.5"}}, {Name: "b", InternalIPs: []string{"192.168.0.5"}}, {Name: "c"}}
	got := engine.Run(context.Background(), "network", net(false, 0, 0), nodes)
	want(t, got, "node.ip", "a", "pass", false)
	want(t, got, "node.ip", "b", "fail", true)
	if c := want(t, got, "node.ip", "c", "fail", true); !strings.Contains(c.Message, "none") {
		t.Fatalf("no address message %q", c.Message)
	}
}

func TestLinksUnknownDoesNotBlock(t *testing.T) {
	nodes := []cluster.Node{{Name: "a", InternalIPs: []string{"10.0.0.5"}}}
	got := engine.Run(context.Background(), "network", net(true, 20, 9000), nodes)
	c := want(t, got, "nic.links", "a", "unknown", false)
	if c.Severity != "warning" || !strings.Contains(c.Hint, "-links") {
		t.Fatalf("unknown links %+v", *c)
	}
}

func TestNICsAndMTU(t *testing.T) {
	// Host networks use <nic>.<vlan>; the external underlay uses the bare NIC even with a VLAN.
	ok := cluster.Node{Name: "ok", InternalIPs: []string{"10.0.0.5"}, NICsKnown: true,
		NICs: []cluster.NIC{{Name: "ens3", MTU: 1500}, {Name: "ens3.20", MTU: 9000}, {Name: "ens8", MTU: 1500}}}
	small := cluster.Node{Name: "small", InternalIPs: []string{"10.0.0.6"}, NICsKnown: true,
		NICs: []cluster.NIC{{Name: "ens3", MTU: 1500}, {Name: "ens3.20", MTU: 1500}, {Name: "ens8", MTU: 1500}}}
	missing := cluster.Node{Name: "missing", InternalIPs: []string{"10.0.0.7"}, NICsKnown: true,
		NICs: []cluster.NIC{{Name: "ens3", MTU: 1500}, {Name: "ens8.30", MTU: 1500}}}
	got := engine.Run(context.Background(), "network", net(true, 20, 9000), []cluster.Node{ok, small, missing})
	for _, c := range got {
		if c.Node == "ok" && c.Status != "pass" {
			t.Fatalf("a good node failed %+v", c)
		}
	}
	if find(got, "nic.exists", "ok") != nil {
		t.Fatal("an existing NIC reported missing")
	}
	var mtuFail, missingFail int
	for _, c := range got {
		if c.Node == "small" && c.ID == "nic.mtu" && c.Status == "fail" {
			mtuFail++
			if !strings.Contains(c.Message, "ens3.20 (tunnel) MTU 1500, want at least 9000") {
				t.Fatalf("mtu message %q", c.Message)
			}
		}
		if c.Node == "missing" && c.ID == "nic.exists" {
			missingFail++
			if !c.Blocks() {
				t.Fatal("a missing NIC does not block")
			}
		}
	}
	if mtuFail != 1 || missingFail != 2 {
		t.Fatalf("mtu fails %d (want 1), missing %d (want ens3.20 and ens8): %+v", mtuFail, missingFail, got)
	}
}

func TestSharedInterfaceNeedsTheLargerMTU(t *testing.T) {
	n := api.Network{Nodes: &api.NodeNets{
		Management: &api.Attachment{NIC: "ens3", CIDR: "10.0.0.0/24"},
		Tunnel:     &api.Attachment{NIC: "ens3", CIDR: "10.1.0.0/24", MTU: 9000},
	}}
	links := wanted(n)
	if len(links) != 1 || links[0].iface != "ens3" || links[0].mtu != 9000 || strings.Join(links[0].nets, ",") != "management,tunnel" {
		t.Fatalf("wanted %+v", links)
	}
}

func TestNoNodeNetworks(t *testing.T) {
	got := engine.Run(context.Background(), "network", api.Network{Driver: "kube-ovn"}, nil)
	if len(got) != 1 || got[0].Status != "pass" {
		t.Fatalf("no node networks %+v", got)
	}
}

func TestStorage(t *testing.T) {
	want(t, engine.Run(context.Background(), "storage", api.Storage{Driver: "existing", StorageClass: "fast-rbd"}, nil), "storage.class", "", "pass", false)
	want(t, engine.Run(context.Background(), "storage", api.Storage{Driver: "existing", StorageClass: "nope"}, nil), "storage.class", "", "fail", true)
	broken := Engine{StorageClasses: func(context.Context) ([]string, error) { return nil, errors.New("forbidden") }}
	want(t, broken.Run(context.Background(), "storage", api.Storage{Driver: "existing", StorageClass: "x"}, nil), "storage.class", "", "unknown", false)
	want(t, engine.Run(context.Background(), "storage", api.Storage{Driver: "nfs"}, nil), "storage.nfs", "", "unknown", false)
	if got := engine.Run(context.Background(), "storage", api.Storage{Driver: "rook-ceph"}, nil); len(got) != 0 {
		t.Fatalf("rook-ceph checks %+v", got)
	}
}

// The engine plugged into the API: a missing NIC makes the network step invalid and blocks the
// next step, as #139 says for fail+error.
func TestEngineBlocksTheStep(t *testing.T) {
	nodes := []cluster.Node{{Name: "a", Ready: true, InternalIPs: []string{"10.0.0.5"}, NICsKnown: true, NICs: []cluster.NIC{{Name: "ens3", MTU: 1500}}}}
	checks := engine.Run(context.Background(), "network", net(true, 0, 0), nodes)
	blocked := false
	for _, c := range checks {
		blocked = blocked || c.Blocks()
	}
	if !blocked {
		t.Fatalf("a missing external NIC does not block: %+v", checks)
	}
}
