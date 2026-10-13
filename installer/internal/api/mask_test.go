package api

import (
	"strings"
	"testing"
)

func TestMask(t *testing.T) {
	m := &mask{literals: func() []string { return []string{"Fake-Code-7Kq2", "", "ab"} }}
	for _, c := range []struct{ in, want string }{
		{"code Fake-Code-7Kq2 issued", "code *** issued"},
		{"password=s3cr3t-fake next", "password=*** next"},
		{`{"adminPassword": "s3cr3t-fake"}`, `{"adminPassword": "***"}`},
		{"API_KEY: abc123fake", "API_KEY: ***"},
		{"Authorization: Bearer eyJfake.token", "Authorization: Bearer ***"},
		{"pull https://user:s3cr3t-fake@registry.example.invalid/x", "pull https://user:***@registry.example.invalid/x"},
		{"release vws upgraded", "release vws upgraded"},
		{"ab stays: short literals are not masked", "ab stays: short literals are not masked"},
	} {
		if got := m.line(c.in); got != c.want {
			t.Errorf("mask(%q) = %q, want %q", c.in, got, c.want)
		}
	}
	lines := []string{"key:", "-----BEGIN RSA PRIVATE KEY-----", "MIIfakebase64", "-----END RSA PRIVATE KEY-----", "after"}
	var out []string
	for _, l := range lines {
		out = append(out, m.line(l))
	}
	if strings.Join(out, "|") != "key:|***|***|***|after" {
		t.Fatalf("PEM block %q", out)
	}
}
