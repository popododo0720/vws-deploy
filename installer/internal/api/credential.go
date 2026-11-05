package api

import (
	"errors"
	"net/http"

	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

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
		return fail(ReasonCredentialConsumed,
			"the initial admin password was already handed out; it is in Secret "+cluster.InitialAdminSecret)
	case StateInstalling:
		return fail(ReasonInstallInProgress, "an install is running")
	default:
		return fail(ReasonStepOutOfOrder, "no install has succeeded")
	}
	user, pw, consumed, err := srv.credentials.Load(r.Context())
	if errors.Is(err, cluster.ErrNoCredential) {
		return fail(ReasonNotFound, "console sign-in is off (-iam static), so there is no initial admin password")
	}
	if err != nil {
		return internal(err)
	}
	if consumed {
		srv.s.state = StateFinished
		return fail(ReasonCredentialConsumed,
			"the initial admin password was already handed out; it is in Secret "+cluster.InitialAdminSecret)
	}
	if err := srv.credentials.MarkConsumed(r.Context()); err != nil {
		return internal(err)
	}
	srv.s.state = StateFinished
	w.Header().Set("Cache-Control", "no-store")
	return writeJSON(w, http.StatusOK, map[string]string{"adminUser": user, "password": pw})
}
