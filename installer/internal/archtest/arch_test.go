package archtest

import (
	"fmt"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"sort"
	"strconv"
	"strings"
	"testing"
)

const module = "github.com/popododo0720/vws-deploy/installer"

// layers lists, for every production package, the module packages it may
// import. A package missing from this map fails, so a new package declares
// its layer here.
var layers = map[string][]string{
	"cmd/vws-installer": {
		"internal/admin", "internal/api", "internal/client", "internal/cluster", "internal/preflight", "internal/tui",
	},
	"internal/admin":     {"internal/api"},
	"internal/api":       {"internal/cluster"},
	"internal/preflight": {"internal/api", "internal/cluster"},
	"internal/cluster":   {},
	"internal/client":    {},
	"internal/tui":       {"internal/client"},
}

// owners confine an import to the directories that own it; no owners means
// no production package may import it.
var owners = []struct {
	match func(imp string) bool
	dirs  []string
	why   string
}{
	{hasPrefix("os/exec"), []string{"internal/cluster"}, "only the cluster adapter runs kubectl and vws"},
	{
		hasPrefix("k8s.io", "sigs.k8s.io"),
		[]string{"internal/cluster"},
		"Kubernetes client types stay in the cluster adapter",
	},
	{func(imp string) bool { return imp == "log" }, nil, "logs go through log/slog"},
	{func(imp string) bool { return imp == "testing" }, nil, "production code does not import testing"},
}

func hasPrefix(prefixes ...string) func(string) bool {
	return func(s string) bool {
		for _, p := range prefixes {
			if s == p || strings.HasPrefix(s, p+"/") {
				return true
			}
		}
		return false
	}
}

// archViolations applies layers and owners to imports, keyed by the
// slash-separated package directory relative to the module root.
func archViolations(imports map[string][]string) []string {
	var bad []string
	for dir, imps := range imports {
		allowed, ok := layers[dir]
		if !ok {
			bad = append(bad, dir+" has no entry in layers")
		}
		for _, imp := range imps {
			if rel, inModule := strings.CutPrefix(imp, module+"/"); inModule && ok && !slices.Contains(allowed, rel) {
				bad = append(bad, fmt.Sprintf("%s imports %s: not in its layers entry", dir, rel))
			}
			for _, o := range owners {
				if o.match(imp) && !slices.Contains(o.dirs, dir) {
					bad = append(bad, fmt.Sprintf("%s imports %s: %s", dir, imp, o.why))
				}
			}
		}
	}
	sort.Strings(bad)
	return bad
}

// productionImports parses the non-test Go files under root, skipping
// testdata, and returns each package directory's imports.
func productionImports(t *testing.T, root string) map[string][]string {
	t.Helper()
	fset := token.NewFileSet()
	imports := map[string][]string{}
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && d.Name() == "testdata" {
			return filepath.SkipDir
		}
		if d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		f, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if err != nil {
			return fmt.Errorf("parse %s: %w", path, err)
		}
		rel, err := filepath.Rel(root, filepath.Dir(path))
		if err != nil {
			return fmt.Errorf("relative path of %s: %w", path, err)
		}
		var imps []string
		for _, spec := range f.Imports {
			imp, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				return fmt.Errorf("%s import %s: %w", path, spec.Path.Value, err)
			}
			imps = append(imps, imp)
		}
		dir := filepath.ToSlash(rel)
		imports[dir] = append(imports[dir], imps...)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return imports
}

func TestArchitectureRules(t *testing.T) {
	probe := t.TempDir()
	for rel, src := range map[string]string{
		"internal/cluster/c.go":        `package cluster; import _ "` + module + `/internal/api"`,
		"internal/api/a.go":            `package api; import _ "` + module + `/internal/cluster"; import _ "os/exec"`,
		"internal/tui/t.go":            `package tui; import _ "` + module + `/internal/cluster"; import _ "log"`,
		"internal/client/c.go":         `package client; import _ "k8s.io/client-go/kubernetes"; import _ "testing"`,
		"internal/widget/w.go":         `package widget`,
		"internal/preflight/p_test.go": `package preflight; import _ "os/exec"; import "testing"`,
		"internal/tui/testdata/x.go":   `package x; import _ "os/exec"`,
	} {
		path := filepath.Join(probe, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(src), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	want := []string{
		"internal/api imports os/exec: only the cluster adapter runs kubectl and vws",
		"internal/client imports k8s.io/client-go/kubernetes: Kubernetes client types stay in the cluster adapter",
		"internal/client imports testing: production code does not import testing",
		"internal/cluster imports internal/api: not in its layers entry",
		"internal/tui imports internal/cluster: not in its layers entry",
		"internal/tui imports log: logs go through log/slog",
		"internal/widget has no entry in layers",
	}
	if got := archViolations(productionImports(t, probe)); !reflect.DeepEqual(got, want) {
		t.Fatalf("probe violations:\n got %q\nwant %q", got, want)
	}

	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Fatalf("module root %s: %v", root, err)
	}
	for dir := range layers {
		if _, err := os.Stat(filepath.Join(root, dir)); err != nil {
			t.Errorf("layers entry %s: %v", dir, err)
		}
	}
	for _, v := range archViolations(productionImports(t, root)) {
		t.Error(v)
	}
}
