package api

import (
	"crypto/rand"
	"errors"
	"net/http"

	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

// PasswordLength is the initial admin password length (#146).
const PasswordLength = 24

const passwordAlphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

// newPassword draws PasswordLength characters from crypto/rand without modulo bias.
func newPassword() (string, error) {
	limit := 256 - 256%len(passwordAlphabet)
	out := make([]byte, 0, PasswordLength)
	buf := make([]byte, PasswordLength)
	for len(out) < PasswordLength {
		if _, err := rand.Read(buf); err != nil {
			return "", err
		}
		for _, c := range buf {
			if int(c) < limit && len(out) < PasswordLength {
				out = append(out, passwordAlphabet[int(c)%len(passwordAlphabet)])
			}
		}
	}
	return string(out), nil
}

// storeCredential creates the initial admin password in the Secret only. The server keeps no
// copy: POST /result/credential reads it back. An existing Secret is kept as it is.
func (srv *Server) storeCredential(user string) error {
	pw, err := newPassword()
	if err != nil {
		return err
	}
	if err := srv.credentials.Store(srv.installCtx, user, pw); err != nil && !errors.Is(err, cluster.ErrCredentialExists) {
		return err
	}
	return nil
}

// postCredential hands the initial admin password out once. It marks the Secret before it
// answers, so a failed mark returns no password; afterwards 410 credential_consumed, and the
// state is finished.
func (srv *Server) postCredential(w http.ResponseWriter, r *http.Request) error {
	srv.mu.Lock()
	defer srv.mu.Unlock()
	if srv.credentials == nil {
		return fail(ReasonNotImplemented, "this installer has no credential store")
	}
	switch srv.s.state {
	case StateSucceeded:
	case StateFinished:
		return fail(ReasonCredentialConsumed, "the initial admin password was already handed out; it is in Secret "+cluster.InitialAdminSecret)
	case StateInstalling:
		return fail(ReasonInstallInProgress, "an install is running")
	default:
		return fail(ReasonStepOutOfOrder, "no install has succeeded")
	}
	user, pw, consumed, err := srv.credentials.Load(r.Context())
	if err != nil {
		return internal(err)
	}
	if consumed {
		srv.s.state = StateFinished
		return fail(ReasonCredentialConsumed, "the initial admin password was already handed out; it is in Secret "+cluster.InitialAdminSecret)
	}
	if err := srv.credentials.MarkConsumed(r.Context()); err != nil {
		return internal(err)
	}
	srv.s.state = StateFinished
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]string{"adminUser": user, "password": pw})
	return nil
}
