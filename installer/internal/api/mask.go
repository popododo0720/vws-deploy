package api

import (
	"regexp"
	"sort"
	"strings"
)

// Masked replaces every secret the mask finds.
const Masked = "***"

var (
	// key=value, key: value and "key": "value" where the key names a secret.
	secretPair = regexp.MustCompile(`(?i)((?:password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?code|credential|private[_-]?key)[a-z0-9_-]*"?\s*[:=]\s*"?)([^\s"',;}]+)`)
	bearer     = regexp.MustCompile(`(?i)(bearer\s+)\S+`)
	// A URL's user:password@.
	urlUser  = regexp.MustCompile(`(://[^/\s:@]+:)[^/\s@]+@`)
	pemBegin = regexp.MustCompile(`-----BEGIN [A-Z0-9 ]*(PRIVATE KEY|CERTIFICATE REQUEST)-----`)
	pemEnd   = regexp.MustCompile(`-----END [A-Z0-9 ]*(PRIVATE KEY|CERTIFICATE REQUEST)-----`)
)

// mask removes secrets from install log lines (#145). Literal values (the access code, the
// session token, generated passwords) come from literals; key=value pairs, bearer tokens, URL
// passwords and PEM private key blocks are found by shape. It keeps state across lines for PEM
// blocks, so one mask serves one ordered stream.
type mask struct {
	literals func() []string
	inPEM    bool
}

// line masks one line. A line inside a PEM private key block becomes Masked as a whole.
func (m *mask) line(s string) string {
	if m.inPEM {
		if pemEnd.MatchString(s) {
			m.inPEM = false
		}
		return Masked
	}
	if loc := pemBegin.FindStringIndex(s); loc != nil {
		if !pemEnd.MatchString(s[loc[1]:]) {
			m.inPEM = true
		}
		return s[:loc[0]] + Masked
	}
	if m.literals != nil {
		lits := m.literals()
		// Longest first, so a secret that contains another is replaced whole.
		sort.Slice(lits, func(i, j int) bool { return len(lits[i]) > len(lits[j]) })
		for _, l := range lits {
			if len(l) >= 4 {
				s = strings.ReplaceAll(s, l, Masked)
			}
		}
	}
	s = secretPair.ReplaceAllString(s, "${1}"+Masked)
	s = bearer.ReplaceAllString(s, "${1}"+Masked)
	return urlUser.ReplaceAllString(s, "${1}"+Masked+"@")
}
