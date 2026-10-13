// Package cluster is what the installer API needs from Kubernetes: the nodes it offers in the
// wizard and the install it runs at the end. The API depends on the interface; tests pass a fake.
package cluster

import "context"

// NIC is one network interface a node reports.
type NIC struct {
	Name string `json:"name"`
	MTU  int    `json:"mtu"`
}

// Node is one cluster node as the wizard shows it.
type Node struct {
	Name        string            `json:"name"`
	Labels      map[string]string `json:"labels,omitempty"`
	Ready       bool              `json:"ready"`
	InternalIPs []string          `json:"internalIPs,omitempty"`
	// NICs come from a links file (talosctl get links), never from the host (R10). NICsKnown is
	// false when no links file names this node: preflight reports the NIC checks as unknown.
	NICs      []NIC `json:"nics,omitempty"`
	NICsKnown bool  `json:"nicsKnown"`
}

// Result is what a finished install hands to GET /result.
type Result struct {
	ConsoleURL string
	AdminUser  string
}

// Cluster reads the nodes and runs the install of one VWSCluster CR (JSON).
type Cluster interface {
	Nodes(ctx context.Context) ([]Node, error)
	// Install applies cr and returns when the install ends. log receives progress lines.
	Install(ctx context.Context, cr []byte, log func(line string)) (Result, error)
}
