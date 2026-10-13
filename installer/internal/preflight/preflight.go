// Package preflight is the installer preflight engine (#143). It turns one step value into the
// #139 PreflightReport checks and takes over the node network checks of #128
// (scripts/vws.d/lib/nodenet.py): node IPs inside the management CIDR, and every NIC with at
// least the CR's MTU. It reads only the Kubernetes API and the optional links file (R10).
package preflight

import (
	"context"
	"fmt"
	"net/netip"
	"slices"
	"sort"
	"strings"

	"github.com/popododo0720/vws-deploy/installer/internal/api"
	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

// MinNodes is the Ready node count each profile needs (VWS_PROFILE_MIN_NODES in
// scripts/vws.d/lib/checks.sh).
var MinNodes = map[string]int{"single-node": 1, "small": 3, "ha": 6}

// DefaultMTU is the MTU a link without one must carry, as in nodenet.py.
const DefaultMTU = 1500

// Engine runs the checks. StorageClasses lists the StorageClass names (cluster.VWS.StorageClasses).
type Engine struct {
	StorageClasses func(ctx context.Context) ([]string, error)
}

// Run is an api.Preflight.
func (e Engine) Run(ctx context.Context, step string, value any, nodes []cluster.Node) []api.Check {
	switch v := value.(type) {
	case api.Roles:
		return roles(v, nodes)
	case api.Network:
		return network(v, nodes)
	case api.Storage:
		return e.storage(ctx, v)
	}
	if step == "profile" {
		if p, ok := value.(string); ok {
			return profile(p, nodes)
		}
	}
	return nil
}

func check(id, severity string, pass bool, node, field, message, hint string) api.Check {
	status := "fail"
	if pass {
		status = "pass"
	}
	return api.Check{ID: id, Severity: severity, Status: status, Node: node, Field: field, Message: message, Hint: hint}
}

func roles(v api.Roles, nodes []cluster.Node) []api.Check {
	var out []api.Check
	for _, name := range v.Nodes {
		i := slices.IndexFunc(nodes, func(n cluster.Node) bool { return n.Name == name })
		if i < 0 {
			continue // validation already rejected it
		}
		out = append(out, check("node.ready", "error", nodes[i].Ready, name, "roles.nodes", name+" Ready", "wait for the node or leave it out"))
	}
	return out
}

func profile(p string, nodes []cluster.Node) []api.Check {
	min, ok := MinNodes[p]
	if !ok {
		return nil
	}
	ready := 0
	for _, n := range nodes {
		if n.Ready {
			ready++
		}
	}
	return []api.Check{check("profile.nodes", "error", ready >= min, "", "spec.profile",
		fmt.Sprintf("%d Ready nodes, profile %s needs %d", ready, p, min), "pick a smaller profile or add nodes")}
}

// link is one interface every node must have.
type link struct {
	iface string
	mtu   int
	nets  []string
}

// wanted mirrors nodenet.wanted: host networks are <nic> or <nic>.<vlan>, underlay networks
// (external, direct) the bare NIC. One interface used twice needs the larger MTU.
func wanted(n api.Network) []link {
	type entry struct {
		name     string
		a        *api.Attachment
		underlay bool
	}
	var entries []entry
	if nn := n.Nodes; nn != nil {
		entries = append(entries, entry{"management", nn.Management, false}, entry{"tunnel", nn.Tunnel, false}, entry{"external", nn.External, true})
		if nn.Storage != nil {
			entries = append(entries, entry{"storage.public", nn.Storage.Public, false}, entry{"storage.cluster", nn.Storage.Cluster, false})
		}
	}
	for i := range n.Direct {
		entries = append(entries, entry{"direct." + n.Direct[i].Name, &n.Direct[i].Attachment, true})
	}
	byIface := map[string]*link{}
	for _, e := range entries {
		if e.a == nil {
			continue
		}
		iface := e.a.NIC
		if !e.underlay && e.a.VLAN != 0 {
			iface = fmt.Sprintf("%s.%d", e.a.NIC, e.a.VLAN)
		}
		mtu := e.a.MTU
		if mtu == 0 {
			mtu = DefaultMTU
		}
		l := byIface[iface]
		if l == nil {
			l = &link{iface: iface}
			byIface[iface] = l
		}
		l.mtu = max(l.mtu, mtu)
		l.nets = append(l.nets, e.name)
	}
	out := make([]link, 0, len(byIface))
	for _, l := range byIface {
		out = append(out, *l)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].iface < out[j].iface })
	return out
}

func network(n api.Network, nodes []cluster.Node) []api.Check {
	if n.Nodes == nil || n.Nodes.Management == nil {
		return []api.Check{check("network.nodes", "info", true, "", "spec.network.nodes", "spec.network.nodes is not set: one NIC, nothing to check", "")}
	}
	mgmt, err := netip.ParsePrefix(n.Nodes.Management.CIDR)
	if err != nil {
		return nil // validation already rejected it
	}
	var out []api.Check
	for _, node := range nodes {
		var inside string
		for _, ip := range node.InternalIPs {
			if a, err := netip.ParseAddr(ip); err == nil && mgmt.Contains(a) {
				inside = ip
				break
			}
		}
		msg := fmt.Sprintf("InternalIP %s is outside management %s", strings.Join(node.InternalIPs, ",")+orNone(node.InternalIPs), mgmt)
		if inside != "" {
			msg = fmt.Sprintf("%s is inside management %s", inside, mgmt)
		}
		out = append(out, check("node.ip", "error", inside != "", node.Name, "spec.network.nodes.management.cidr", msg, "the management CIDR must hold every node InternalIP"))
	}
	want := wanted(n)
	for _, node := range nodes {
		if !node.NICsKnown {
			out = append(out, api.Check{ID: "nic.links", Severity: "warning", Status: "unknown", Node: node.Name, Field: "spec.network.nodes",
				Message: "NICs and MTUs are not verified: no links for this node", Hint: "start vws-installer with -links FILE (talosctl -n <nodes> get links -o json)"})
			continue
		}
		for _, l := range want {
			i := slices.IndexFunc(node.NICs, func(c cluster.NIC) bool { return c.Name == l.iface })
			nets := strings.Join(l.nets, ",")
			if i < 0 {
				out = append(out, check("nic.exists", "error", false, node.Name, "spec.network.nodes", fmt.Sprintf("interface %s (%s) does not exist", l.iface, nets), "create the interface or pick another NIC"))
				continue
			}
			got := node.NICs[i].MTU
			out = append(out, check("nic.mtu", "error", got >= l.mtu, node.Name, "spec.network.nodes",
				fmt.Sprintf("interface %s (%s) MTU %d, want at least %d", l.iface, nets, got, l.mtu), "raise the NIC MTU or lower the CR mtu"))
		}
	}
	return out
}

func orNone(ips []string) string {
	if len(ips) == 0 {
		return "none"
	}
	return ""
}

func (e Engine) storage(ctx context.Context, s api.Storage) []api.Check {
	switch s.Driver {
	case "existing":
		classes, err := e.StorageClasses(ctx)
		if err != nil {
			return []api.Check{{ID: "storage.class", Severity: "warning", Status: "unknown", Field: "spec.storage.storageClass", Message: "cannot list StorageClasses"}}
		}
		return []api.Check{check("storage.class", "error", slices.Contains(classes, s.StorageClass), "", "spec.storage.storageClass",
			"StorageClass "+s.StorageClass+" exists", "create it first or pick another driver")}
	case "nfs":
		return []api.Check{{ID: "storage.nfs", Severity: "info", Status: "unknown", Field: "spec.storage.nfs.server",
			Message: "the NFS server is not reached from the installer", Hint: "the CSI driver mounts it after the install"}}
	}
	return nil
}
