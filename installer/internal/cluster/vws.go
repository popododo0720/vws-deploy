package cluster

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// VWS is the real cluster: kubectl for the nodes, and the vws CLI for the install
// (vws init --from FILE, then vws apply --unattended), the same path as docs/quickstart.md.
type VWS struct {
	Kubectl string
	VWS     string
	WorkDir string
	// Links is an optional `talosctl -n <every node> get links -o json` file (#128). Empty: the
	// NICs are unknown.
	Links string
}

// Nodes lists the nodes with kubectl, with their Ready condition and InternalIPs, and their
// NICs from the Links file when there is one.
func (v VWS) Nodes(ctx context.Context) ([]Node, error) {
	out, err := exec.CommandContext(ctx, v.Kubectl, "get", "nodes", "-o", "json").Output()
	if err != nil {
		return nil, fmt.Errorf("kubectl get nodes: %w", err)
	}
	var list struct {
		Items []struct {
			Metadata struct {
				Name   string            `json:"name"`
				Labels map[string]string `json:"labels"`
			} `json:"metadata"`
			Status struct {
				Conditions []struct {
					Type   string `json:"type"`
					Status string `json:"status"`
				} `json:"conditions"`
				Addresses []struct {
					Type    string `json:"type"`
					Address string `json:"address"`
				} `json:"addresses"`
			} `json:"status"`
		} `json:"items"`
	}
	if err := json.Unmarshal(out, &list); err != nil {
		return nil, fmt.Errorf("kubectl get nodes: %w", err)
	}
	var links map[string][]NIC
	if v.Links != "" {
		raw, err := os.ReadFile(v.Links)
		if err != nil {
			return nil, fmt.Errorf("links file: %w", err)
		}
		if links, err = ParseLinks(raw); err != nil {
			return nil, err
		}
	}
	nodes := make([]Node, 0, len(list.Items))
	for _, item := range list.Items {
		n := Node{Name: item.Metadata.Name, Labels: item.Metadata.Labels}
		for _, c := range item.Status.Conditions {
			n.Ready = n.Ready || (c.Type == "Ready" && c.Status == "True")
		}
		for _, a := range item.Status.Addresses {
			if a.Type == "InternalIP" {
				n.InternalIPs = append(n.InternalIPs, a.Address)
			}
		}
		for _, key := range append([]string{n.Name}, n.InternalIPs...) {
			if nics, ok := links[key]; ok {
				n.NICs, n.NICsKnown = nics, true
				break
			}
		}
		nodes = append(nodes, n)
	}
	return nodes, nil
}

// ParseLinks reads a talosctl JSON stream (one object per link, with "node", metadata.id and
// spec.mtu) into node name or address -> NICs, the same input as scripts/vws.d/lib/nodenet.py.
func ParseLinks(raw []byte) (map[string][]NIC, error) {
	out := map[string][]NIC{}
	dec := json.NewDecoder(bytes.NewReader(raw))
	for dec.More() {
		var obj struct {
			Node     string `json:"node"`
			Metadata struct {
				ID string `json:"id"`
			} `json:"metadata"`
			Spec struct {
				MTU int `json:"mtu"`
			} `json:"spec"`
		}
		if err := dec.Decode(&obj); err != nil {
			return nil, fmt.Errorf("links file: %w", err)
		}
		out[obj.Node] = append(out[obj.Node], NIC{Name: obj.Metadata.ID, MTU: obj.Spec.MTU})
	}
	return out, nil
}

// StorageClasses lists the StorageClass names with kubectl.
func (v VWS) StorageClasses(ctx context.Context) ([]string, error) {
	out, err := exec.CommandContext(ctx, v.Kubectl, "get", "storageclasses", "-o", "jsonpath={.items[*].metadata.name}").Output()
	if err != nil {
		return nil, fmt.Errorf("kubectl get storageclasses: %w", err)
	}
	return strings.Fields(string(out)), nil
}

// Install writes cr to WorkDir and runs the two vws commands, streaming their output to log.
func (v VWS) Install(ctx context.Context, cr []byte, log func(string)) (Result, error) {
	path := filepath.Join(v.WorkDir, "vwscluster.json")
	if err := os.WriteFile(path, cr, 0o600); err != nil {
		return Result{}, err
	}
	for _, args := range [][]string{{"init", "--from", path}, {"apply", "--unattended"}} {
		if err := v.run(ctx, log, args...); err != nil {
			return Result{}, err
		}
	}
	return Result{}, nil
}

func (v VWS) run(ctx context.Context, log func(string), args ...string) error {
	cmd := exec.CommandContext(ctx, v.VWS, args...)
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = pw, pw
	done := make(chan struct{})
	go func() {
		scan := bufio.NewScanner(pr)
		for scan.Scan() {
			log(scan.Text())
		}
		close(done)
	}()
	err := cmd.Run()
	pw.Close()
	<-done
	if err != nil {
		return fmt.Errorf("vws %s: %w", args[0], err)
	}
	return nil
}
