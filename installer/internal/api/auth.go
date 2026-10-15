package api

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"sync"
	"time"
)

// Auth holds the one-time access code and the one session (#139 authentication).
//
// The code is 16 characters from crypto/rand. It is never logged: publish hands each new code to
// the code file that only kubectl exec can read. A successful exchange consumes the code, issues
// the next one and replaces any earlier session, so at most one session exists. Five wrong codes
// in a row retire the code and issue a new one. A session idle for IdleTimeout expires.
type Auth struct {
	mu        sync.Mutex
	code      string
	failures  int
	token     string
	lastSeen  time.Time
	now       func() time.Time
	publish   func(code string) error
	idle      time.Duration
	maxFailed int
}

// Code alphabet: no 0/O, 1/l/I, so a code read off a terminal is typed back right.
const codeAlphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"

const (
	codeLength  = 16
	IdleTimeout = 30 * time.Minute
	MaxFailures = 5
)

// NewAuth issues the first code through publish.
func NewAuth(now func() time.Time, publish func(code string) error) (*Auth, error) {
	a := &Auth{now: now, publish: publish, idle: IdleTimeout, maxFailed: MaxFailures}
	if err := a.rotate(); err != nil {
		return nil, err
	}
	return a, nil
}

func (a *Auth) rotate() error {
	// Rejection sampling keeps every character equally likely.
	limit := 256 - 256%len(codeAlphabet)
	code := make([]byte, 0, codeLength)
	buf := make([]byte, codeLength)
	for len(code) < codeLength {
		if _, err := rand.Read(buf); err != nil {
			return err
		}
		for _, c := range buf {
			if int(c) < limit && len(code) < codeLength {
				code = append(code, codeAlphabet[int(c)%len(codeAlphabet)])
			}
		}
	}
	a.code, a.failures = string(code), 0
	return a.publish(a.code)
}

// Exchange trades a code for a session token.
func (a *Auth) Exchange(code string) (string, *Error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if subtle.ConstantTimeCompare([]byte(code), []byte(a.code)) != 1 {
		a.failures++
		if a.failures >= a.maxFailed {
			if err := a.rotate(); err != nil {
				return "", internal(err)
			}
			return "", fail(ReasonForbidden, "too many wrong codes; a new code was issued, read it with vws init --print-url")
		}
		return "", fail(ReasonUnauthenticated, "wrong access code")
	}
	t := make([]byte, 32)
	if _, err := rand.Read(t); err != nil {
		return "", internal(err)
	}
	a.token, a.lastSeen = hex.EncodeToString(t), a.now()
	if err := a.rotate(); err != nil {
		return "", internal(err)
	}
	return a.token, nil
}

// Check accepts the current session token and refreshes its idle timer.
func (a *Auth) Check(token string) *Error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if token == "" || a.token == "" || subtle.ConstantTimeCompare([]byte(token), []byte(a.token)) != 1 {
		return fail(ReasonUnauthenticated, "no valid session; POST /api/v1/session with the access code")
	}
	if a.now().Sub(a.lastSeen) > a.idle {
		a.token = ""
		return fail(ReasonSessionExpired, "the session was idle too long; exchange a new access code")
	}
	a.lastSeen = a.now()
	return nil
}

// Secrets returns the live code and session token, for the event stream's mask.
func (a *Auth) Secrets() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return []string{a.code, a.token}
}
