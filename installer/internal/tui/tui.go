// Package tui is `vws init` in the installer pod (#142): a line-based client of the installer
// API. It walks roles → network → storage → eip → profile → observability, shows each step's
// field errors and preflight, then the summary, asks for confirmation, installs and waits.
package tui

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/popododo0720/vws-deploy/installer/internal/client"
)

// TUI holds the terminal and the API client.
type TUI struct {
	C    *client.Client
	In   *bufio.Reader
	Out  io.Writer
	Poll time.Duration
}

// New returns a TUI on in and out.
func New(c *client.Client, in io.Reader, out io.Writer) *TUI {
	return &TUI{C: c, In: bufio.NewReader(in), Out: out, Poll: 2 * time.Second}
}

// ErrAborted is returned when the user declines the summary or input ends.
var ErrAborted = errors.New("aborted")

type check struct {
	ID       string `json:"id"`
	Severity string `json:"severity"`
	Status   string `json:"status"`
	Node     string `json:"node"`
	Message  string `json:"message"`
	Hint     string `json:"hint"`
}

type stepResult struct {
	Step      string          `json:"step"`
	Status    string          `json:"status"`
	Defaults  json.RawMessage `json:"defaults"`
	Value     json.RawMessage `json:"value"`
	Options   json.RawMessage `json:"options"`
	Preflight *struct {
		Checks []check `json:"checks"`
	} `json:"preflight"`
}

func (t *TUI) printf(format string, a ...any) { fmt.Fprintf(t.Out, format, a...) } //nolint:errcheck // terminal

// ask prints a prompt with its default and returns the answer, or the default on an empty line.
func (t *TUI) ask(prompt, def string) (string, error) {
	t.printf("%s [%s]: ", prompt, def)
	line, err := t.In.ReadString('\n')
	if err != nil && (line == "" || !errors.Is(err, io.EOF)) {
		return "", ErrAborted
	}
	if line = strings.TrimSpace(line); line == "" {
		return def, nil
	}
	return line, nil
}

// Steps is the wizard order; each asks for one step's value.
var Steps = []struct {
	Name string
	Ask  func(t *TUI, r stepResult) (any, error)
}{
	{"roles", askRoles},
	{"network", askNetwork},
	{"storage", askStorage},
	{"eip", askEIP},
	{"profile", askProfile},
	{"observability", func(*TUI, stepResult) (any, error) { return map[string]string{"otlpEndpoint": ""}, nil }},
}

// Run walks the wizard and the install. It returns nil when the install succeeded.
func (t *TUI) Run(ctx context.Context) error {
	for _, s := range Steps {
		if err := t.step(ctx, s.Name, s.Ask); err != nil {
			return err
		}
	}
	return t.confirmAndInstall(ctx)
}

func (t *TUI) step(ctx context.Context, name string, askFn func(*TUI, stepResult) (any, error)) error {
	for {
		var r stepResult
		if err := t.C.Do(ctx, http.MethodGet, "/steps/"+name, nil, &r); err != nil {
			return err
		}
		t.printf("\n== %s ==\n", name)
		value, err := askFn(t, r)
		if err != nil {
			return err
		}
		var saved stepResult
		err = t.C.Do(ctx, http.MethodPut, "/steps/"+name, map[string]any{"value": value}, &saved)
		var apiErr *client.Error
		if errors.As(err, &apiErr) && apiErr.Code == "validation_failed" {
			for _, f := range apiErr.Fields {
				t.printf("  ! %s: %s\n", f.Field, f.Message)
			}
			continue
		}
		if err != nil {
			return err
		}
		t.preflight(saved)
		if saved.Status == "valid" {
			return nil
		}
		t.printf("  preflight blocks this step; fix the cluster or change the value\n")
	}
}

func (t *TUI) preflight(r stepResult) {
	if r.Preflight == nil {
		return
	}
	for _, c := range r.Preflight.Checks {
		if c.Status == "pass" {
			continue
		}
		t.printf("  [%s/%s] %s %s %s\n", c.Severity, c.Status, c.ID, c.Node, c.Message)
		if c.Hint != "" {
			t.printf("      hint: %s\n", c.Hint)
		}
	}
}

func decodeJSON(what string, raw json.RawMessage, dst any) error {
	if err := json.Unmarshal(raw, dst); err != nil {
		return fmt.Errorf("%s: %w", what, err)
	}
	return nil
}

func defaultString(raw json.RawMessage) (string, error) {
	var s string
	if err := decodeJSON("profile default", raw, &s); err != nil {
		return "", err
	}
	return s, nil
}

func askRoles(t *TUI, r stepResult) (any, error) {
	var def struct {
		Nodes []string `json:"nodes"`
	}
	if err := decodeJSON("roles defaults", r.Defaults, &def); err != nil {
		return nil, err
	}
	t.printf("  nodes are used with the roles their labels already give them\n")
	answer, err := t.ask("nodes (comma separated)", strings.Join(def.Nodes, ","))
	if err != nil {
		return nil, err
	}
	nodes := []string{}
	for _, n := range strings.Split(answer, ",") {
		if n = strings.TrimSpace(n); n != "" {
			nodes = append(nodes, n)
		}
	}
	return map[string]any{"nodes": nodes}, nil
}

func (t *TUI) attachment(name string, required, gateway bool) (map[string]any, error) {
	def := ""
	if !required {
		def = "skip"
	}
	nic, err := t.ask("  "+name+" NIC", def)
	if err != nil || nic == "skip" || nic == "" {
		return nil, err
	}
	a := map[string]any{"nic": nic}
	cidr, err := t.ask("  "+name+" CIDR", "")
	if err != nil {
		return nil, err
	}
	a["cidr"] = cidr
	vlan, err := t.ask("  "+name+" VLAN (0 = untagged)", "0")
	if err != nil {
		return nil, err
	}
	vlanN, err := strconv.Atoi(vlan)
	if err != nil {
		return nil, fmt.Errorf("%s VLAN: %w", name, err)
	}
	if vlanN != 0 {
		a["vlan"] = vlanN
	}
	mtu, err := t.ask("  "+name+" MTU (0 = NIC default)", "0")
	if err != nil {
		return nil, err
	}
	mtuN, err := strconv.Atoi(mtu)
	if err != nil {
		return nil, fmt.Errorf("%s MTU: %w", name, err)
	}
	if mtuN != 0 {
		a["mtu"] = mtuN
	}
	if gateway {
		gw, err := t.ask("  "+name+" gateway", "")
		if err != nil {
			return nil, err
		}
		a["gateway"] = gw
	}
	return a, nil
}

func askNetwork(t *TUI, r stepResult) (any, error) {
	var opts struct {
		NICs []struct {
			Name  string   `json:"name"`
			MTU   int      `json:"mtu"`
			Nodes []string `json:"nodes"`
		} `json:"nics"`
	}
	if err := decodeJSON("network options", r.Options, &opts); err != nil {
		return nil, err
	}
	for _, n := range opts.NICs {
		t.printf("  NIC %s mtu %d on %s\n", n.Name, n.MTU, strings.Join(n.Nodes, ","))
	}
	nodes := map[string]any{}
	m, err := t.attachment("management", true, false)
	if err != nil {
		return nil, err
	}
	nodes["management"] = m
	for _, name := range []string{"tunnel"} {
		a, err := t.attachment(name, false, false)
		if err != nil {
			return nil, err
		}
		if a != nil {
			nodes[name] = a
		}
	}
	pub, err := t.attachment("storage public", false, false)
	if err != nil {
		return nil, err
	}
	if pub != nil {
		nodes["storage"] = map[string]any{"public": pub}
	}
	ext, err := t.attachment("external (elastic IPs)", false, true)
	if err != nil {
		return nil, err
	}
	if ext != nil {
		nodes["external"] = ext
	}
	return map[string]any{"driver": "kube-ovn", "nodes": nodes}, nil
}

func askStorage(t *TUI, r stepResult) (any, error) {
	var opts struct {
		Drivers []struct {
			Driver   string `json:"driver"`
			Disabled bool   `json:"disabled"`
			Reason   string `json:"reason"`
		} `json:"drivers"`
		VolumeTypes []string `json:"volumeTypes"`
	}
	if err := decodeJSON("storage options", r.Options, &opts); err != nil {
		return nil, err
	}
	var enabled []string
	for _, d := range opts.Drivers {
		if d.Disabled {
			t.printf("  %s: %s\n", d.Driver, d.Reason)
			continue
		}
		enabled = append(enabled, d.Driver)
	}
	var def struct {
		Driver string `json:"driver"`
	}
	if err := decodeJSON("storage defaults", r.Defaults, &def); err != nil {
		return nil, err
	}
	driver, err := t.ask("storage driver ("+strings.Join(enabled, ", ")+")", def.Driver)
	if err != nil {
		return nil, err
	}
	v := map[string]any{"driver": driver}
	switch driver {
	case "existing":
		class, err := t.ask("StorageClass", "")
		if err != nil {
			return nil, err
		}
		v["storageClass"] = class
	case "nfs":
		server, err := t.ask("NFS server", "")
		if err != nil {
			return nil, err
		}
		share, err := t.ask("NFS share", "")
		if err != nil {
			return nil, err
		}
		v["nfs"] = map[string]string{"server": server, "share": share}
	}
	if driver != "nfs" && len(opts.VolumeTypes) > 0 {
		types, err := askVolumeTypes(t, opts.VolumeTypes)
		if err != nil {
			return nil, err
		}
		if len(types) > 0 {
			v["volumeTypes"] = types
		}
	}
	return v, nil
}

// askVolumeTypes asks for the backend of each EBS volume type (spec.storage.volumeTypes, #391).
// An empty StorageClass leaves the type unmapped; the mapping is printed with every unmapped type
// named, so the gap is visible before the summary.
func askVolumeTypes(t *TUI, names []string) (map[string]any, error) {
	answer, err := t.ask("map EBS volume types to StorageClasses? (yes/no)", "no")
	if err != nil {
		return nil, err
	}
	if !slices.Contains([]string{"yes", "y"}, strings.ToLower(answer)) {
		t.printf("  EBS volume types: not mapped (spec.storage.volumeTypes left out)\n")
		return map[string]any{}, nil
	}
	types := map[string]any{}
	for _, name := range names {
		class, err := t.ask(name+" StorageClass (empty = unmapped)", "")
		if err != nil {
			return nil, err
		}
		if class == "" {
			continue
		}
		vt := map[string]string{"storageClass": class}
		mode, err := t.ask(name+" volumeMode (Block, Filesystem; empty = storage default)", "")
		if err != nil {
			return nil, err
		}
		if mode != "" {
			vt["volumeMode"] = mode
		}
		snap, err := t.ask(name+" VolumeSnapshotClass (empty = storage default)", "")
		if err != nil {
			return nil, err
		}
		if snap != "" {
			vt["volumeSnapshotClass"] = snap
		}
		types[name] = vt
	}
	t.printf("  EBS volume types:\n")
	for _, name := range names {
		if vt, ok := types[name].(map[string]string); ok {
			t.printf("    %-8s %s\n", name, vt["storageClass"])
		} else {
			t.printf("    %-8s unmapped (vws-storage refuses volumes of this type)\n", name)
		}
	}
	return types, nil
}

func askEIP(t *TUI, _ stepResult) (any, error) {
	return t.ask("elastic IP pool CIDR (required, inside the external network)", "")
}

func askProfile(t *TUI, r stepResult) (any, error) {
	def, err := defaultString(r.Defaults)
	if err != nil {
		return nil, err
	}
	return t.ask("profile (single-node, small, ha)", def)
}

func (t *TUI) confirmAndInstall(ctx context.Context) error {
	var sum struct {
		CR       json.RawMessage `json:"cr"`
		SpecHash string          `json:"specHash"`
		Warnings []check         `json:"warnings"`
	}
	if err := t.C.Do(ctx, http.MethodGet, "/summary", nil, &sum); err != nil {
		return err
	}
	var pretty any
	if err := decodeJSON("summary cr", sum.CR, &pretty); err != nil {
		return err
	}
	b, err := json.MarshalIndent(pretty, "", "  ")
	if err != nil {
		return fmt.Errorf("summary cr: %w", err)
	}
	t.printf("\n== summary ==\n%s\n", b)
	for _, w := range sum.Warnings {
		t.printf("  warning [%s/%s] %s %s\n", w.Severity, w.Status, w.ID, w.Message)
	}
	answer, err := t.ask("install this? (yes/no)", "no")
	if err != nil {
		return err
	}
	if !slices.Contains([]string{"yes", "y"}, strings.ToLower(answer)) {
		return ErrAborted
	}
	if err := t.C.Do(ctx, http.MethodPost, "/install", map[string]any{"confirm": true, "specHash": sum.SpecHash}, nil); err != nil {
		return err
	}
	t.printf("installing")
	// The progress stream is #145; until then the TUI polls the result.
	for {
		var res struct {
			State               string `json:"state"`
			ConsoleURL          string `json:"consoleURL"`
			AdminUser           string `json:"adminUser"`
			CredentialAvailable bool   `json:"credentialAvailable"`
			Error               string `json:"error"`
		}
		if err := t.C.Do(ctx, http.MethodGet, "/result", nil, &res); err != nil {
			return err
		}
		switch res.State {
		case "succeeded", "finished":
			t.printf("\ninstalled. console: %s, admin user: %s\n", res.ConsoleURL, res.AdminUser)
			if !res.CredentialAvailable {
				t.printf("the initial password is not handed out yet (vws-deploy#146)\n")
			}
			return nil
		case "failed":
			t.printf("\n")
			return fmt.Errorf("install failed: %s", res.Error)
		}
		t.printf(".")
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(t.Poll):
		}
	}
}
