package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPrintURL(t *testing.T) {
	dir := t.TempDir()
	if err := writeSecretFile(filepath.Join(dir, "access-code"), "ABCDEFGHJKMNPQRS"); err != nil {
		t.Fatal(err)
	}
	if err := writeSecretFile(filepath.Join(dir, "fingerprint"), strings.Repeat("ab", 32)); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := runPrintURL([]string{"-state-dir", dir}, &out); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"https://localhost:8443", "port-forward svc/vws-installer 8443", "access code: ABCDEFGHJKMNPQRS", "certificate sha256: " + strings.Repeat("ab", 32)} {
		if !strings.Contains(out.String(), want) {
			t.Fatalf("print-url lacks %q:\n%s", want, out.String())
		}
	}
	if fi, _ := os.Stat(filepath.Join(dir, "access-code")); fi.Mode().Perm() != 0o600 {
		t.Fatalf("access code mode %v", fi.Mode().Perm())
	}
	if err := runPrintURL([]string{"-state-dir", filepath.Join(dir, "none")}, &out); err == nil {
		t.Fatal("print-url without a running API did not fail")
	}
}
