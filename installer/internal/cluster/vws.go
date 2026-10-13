package cluster

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
)

// VWS is the real cluster: kubectl for the nodes, and the vws CLI for the install
// (vws init --from FILE, then vws apply --unattended), the same path as docs/quickstart.md.
type VWS struct {
	Kubectl string
	VWS     string
	WorkDir string
}

// Nodes lists the nodes with kubectl. Node NICs are not in the Node object (R10: no host
// access); the preflight engine (#143) fills them, so NICs stay empty here.
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
		} `json:"items"`
	}
	if err := json.Unmarshal(out, &list); err != nil {
		return nil, fmt.Errorf("kubectl get nodes: %w", err)
	}
	nodes := make([]Node, 0, len(list.Items))
	for _, item := range list.Items {
		nodes = append(nodes, Node{Name: item.Metadata.Name, Labels: item.Metadata.Labels})
	}
	return nodes, nil
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
