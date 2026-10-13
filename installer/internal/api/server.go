// Package api is the installer API of vws-deploy#139 (#141): the one draft session, its steps,
// the summary and the install, behind a one-time access code.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

// Preflight runs the checks of one step on a decoded value. The engine is #143; until then
// NoPreflight reports no checks.
type Preflight func(ctx context.Context, step string, value any, nodes []cluster.Node) []Check

// NoPreflight runs no checks.
func NoPreflight(context.Context, string, any, []cluster.Node) []Check { return nil }

// Server serves /api/v1.
type Server struct {
	mu        sync.Mutex
	s         *session
	auth      *Auth
	cluster   cluster.Cluster
	preflight Preflight
	now       func() time.Time
	// installCtx bounds the install goroutine; the server's own context.
	installCtx context.Context
	installed  chan struct{}
	apiVersion string
	// credentials is where the initial admin password lives (#146); nil hands none out.
	credentials cluster.Credentials
}

// Config wires a Server.
type Config struct {
	Auth      *Auth
	Cluster   cluster.Cluster
	Preflight Preflight
	Now       func() time.Time
	// Context bounds a running install.
	Context context.Context
	// APIVersion is the VWSCluster group/version. It comes from the CRD (R18: the group is
	// written only in charts/vws/crds/vwsclusters.yaml), never from a constant here.
	APIVersion string
	// Credentials stores the initial admin password (#146). Nil: none is made.
	Credentials cluster.Credentials
}

// New returns a Server with an empty draft.
func New(c Config) *Server {
	if c.Preflight == nil {
		c.Preflight = NoPreflight
	}
	if c.Now == nil {
		c.Now = time.Now
	}
	if c.Context == nil {
		c.Context = context.Background()
	}
	return &Server{s: newSession(), auth: c.Auth, cluster: c.Cluster, preflight: c.Preflight, now: c.Now, installCtx: c.Context, apiVersion: c.APIVersion, credentials: c.Credentials}
}

// SessionCookie carries the session token for browsers.
const SessionCookie = "vws_installer_session"

const maxBody = 1 << 20

// Handler routes /api/v1.
func (srv *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/v1/session", srv.postSession)
	authed := func(pattern string, h func(http.ResponseWriter, *http.Request) error) {
		mux.HandleFunc(pattern, func(w http.ResponseWriter, r *http.Request) {
			if err := srv.auth.Check(token(r)); err != nil {
				writeError(w, r, err)
				return
			}
			r.Body = http.MaxBytesReader(w, r.Body, maxBody)
			if err := h(w, r); err != nil {
				writeError(w, r, err)
			}
		})
	}
	authed("GET /api/v1/state", srv.getState)
	authed("GET /api/v1/steps/{step}", srv.getStep)
	authed("PUT /api/v1/steps/{step}", srv.putStep)
	authed("POST /api/v1/steps/{step}/preflight", srv.postPreflight)
	authed("GET /api/v1/summary", srv.getSummary)
	authed("POST /api/v1/install", srv.postInstall)
	authed("GET /api/v1/result", srv.getResult)
	authed("GET /api/v1/cr", srv.getCR)
	authed("POST /api/v1/cr", srv.postCR)
	authed("POST /api/v1/result/credential", srv.postCredential)
	for pattern, issue := range map[string]string{
		"GET /api/v1/install/events": "#145",
	} {
		authed(pattern, func(http.ResponseWriter, *http.Request) error {
			return fail(ReasonNotImplemented, "not implemented yet (vws-deploy"+issue+")")
		})
	}
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		writeError(w, r, fail(ReasonNotFound, "no such endpoint"))
	})
	return mux
}

// Route is the route template for metrics: the matched pattern, never the raw path.
func Route(r *http.Request) string {
	if r.Pattern == "" {
		return "unknown"
	}
	return r.Pattern
}

func token(r *http.Request) string {
	if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
		return strings.TrimPrefix(h, "Bearer ")
	}
	if c, err := r.Cookie(SessionCookie); err == nil {
		return c.Value
	}
	return ""
}

// decode reads one JSON object into v, rejecting unknown fields, trailing data and big bodies.
func decode(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			return fail(ReasonBadRequest, "request body is larger than 1 MiB")
		}
		return fail(ReasonBadRequest, "request body: "+err.Error())
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return fail(ReasonBadRequest, "request body has data after the JSON object")
	}
	return nil
}

func (srv *Server) postSession(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 4<<10)
	var body struct {
		Code string `json:"code"`
	}
	if err := decode(r, &body); err != nil {
		writeError(w, r, err)
		return
	}
	tok, err := srv.auth.Exchange(body.Code)
	if err != nil {
		writeError(w, r, err)
		return
	}
	http.SetCookie(w, &http.Cookie{Name: SessionCookie, Value: tok, Path: "/api/v1", HttpOnly: true, Secure: true, SameSite: http.SameSiteStrictMode})
	writeJSON(w, http.StatusOK, map[string]any{"token": tok, "idleTimeoutSeconds": int(IdleTimeout.Seconds())})
}

type stepSummary struct {
	Step   string     `json:"step"`
	Status StepStatus `json:"status"`
}

type stateView struct {
	State State          `json:"state"`
	Steps []stepSummary  `json:"steps"`
	CR    map[string]any `json:"cr"`
}

func (srv *Server) getState(w http.ResponseWriter, _ *http.Request) error {
	srv.mu.Lock()
	defer srv.mu.Unlock()
	writeJSON(w, http.StatusOK, srv.stateView())
	return nil
}

// stateView needs srv.mu.
func (srv *Server) stateView() stateView {
	v := stateView{State: srv.s.state, CR: srv.s.cr(srv.apiVersion)}
	for i, st := range srv.s.steps {
		v.Steps = append(v.Steps, stepSummary{Step: Steps[i].Name, Status: st.status})
	}
	return v
}

// StepResult is the #139 StepResult.
type StepResult struct {
	Step      string           `json:"step"`
	Status    StepStatus       `json:"status"`
	Defaults  any              `json:"defaults"`
	Value     json.RawMessage  `json:"value"`
	Options   any              `json:"options"`
	Preflight *PreflightReport `json:"preflight"`
	Errors    []FieldError     `json:"errors"`
}

func (srv *Server) stepIn(r *http.Request) (int, error) {
	i := stepIndex(r.PathValue("step"))
	if i < 0 {
		return 0, fail(ReasonNotFound, "no step "+r.PathValue("step")+"; steps are roles, network, storage, eip, profile, observability")
	}
	return i, nil
}

func (srv *Server) result(i int, nodes []cluster.Node) StepResult {
	st := srv.s.steps[i]
	errs := st.errors
	if errs == nil {
		errs = []FieldError{}
	}
	return StepResult{Step: Steps[i].Name, Status: st.status, Defaults: Steps[i].Defaults(nodes), Value: st.raw,
		Options: Steps[i].Options(nodes), Preflight: st.preflight, Errors: errs}
}

func (srv *Server) nodes(ctx context.Context) ([]cluster.Node, error) {
	nodes, err := srv.cluster.Nodes(ctx)
	if err != nil {
		return nil, internal(err)
	}
	return nodes, nil
}

func (srv *Server) getStep(w http.ResponseWriter, r *http.Request) error {
	i, err := srv.stepIn(r)
	if err != nil {
		return err
	}
	nodes, err := srv.nodes(r.Context())
	if err != nil {
		return err
	}
	srv.mu.Lock()
	defer srv.mu.Unlock()
	writeJSON(w, http.StatusOK, srv.result(i, nodes))
	return nil
}

// evaluate decodes and preflights the saved value of step i and sets its status.
func (srv *Server) evaluate(ctx context.Context, i int, nodes []cluster.Node) {
	st := &srv.s.steps[i]
	if st.raw == nil {
		return
	}
	value, fields := Steps[i].Decode(st.raw, Input{Nodes: nodes, Network: srv.s.network()})
	st.value, st.errors, st.preflight = value, fields, nil
	if len(fields) > 0 {
		st.status = StepInvalid
		return
	}
	checks := srv.preflight(ctx, Steps[i].Name, value, nodes)
	if checks == nil {
		checks = []Check{}
	}
	st.preflight = &PreflightReport{RanAt: srv.now().UTC(), Checks: checks}
	st.status = StepValid
	for _, c := range checks {
		if c.Blocks() {
			st.status = StepInvalid
		}
	}
}

func (srv *Server) putStep(w http.ResponseWriter, r *http.Request) error {
	i, err := srv.stepIn(r)
	if err != nil {
		return err
	}
	var body struct {
		Value json.RawMessage `json:"value"`
	}
	if err := decode(r, &body); err != nil {
		return err
	}
	if len(body.Value) == 0 {
		return fail(ReasonBadRequest, `request body needs "value"`)
	}
	nodes, err := srv.nodes(r.Context())
	if err != nil {
		return err
	}
	srv.mu.Lock()
	defer srv.mu.Unlock()
	if err := srv.s.editable(); err != nil {
		return err
	}
	if j := srv.s.blockedBy(i); j >= 0 {
		return fail(ReasonStepOutOfOrder, "step "+Steps[j].Name+" must be valid before "+Steps[i].Name)
	}
	srv.s.steps[i].raw = body.Value
	srv.evaluate(r.Context(), i, nodes)
	// A later step may depend on this one (eip on network): check the saved later steps again.
	for j := i + 1; j < len(Steps); j++ {
		srv.evaluate(r.Context(), j, nodes)
	}
	res := srv.result(i, nodes)
	if len(res.Errors) > 0 {
		return fail(ReasonValidationFailed, "step "+Steps[i].Name+" has invalid fields", res.Errors...)
	}
	writeJSON(w, http.StatusOK, res)
	return nil
}

func (srv *Server) postPreflight(w http.ResponseWriter, r *http.Request) error {
	i, err := srv.stepIn(r)
	if err != nil {
		return err
	}
	nodes, err := srv.nodes(r.Context())
	if err != nil {
		return err
	}
	srv.mu.Lock()
	defer srv.mu.Unlock()
	if srv.s.steps[i].raw == nil {
		return fail(ReasonStepOutOfOrder, "step "+Steps[i].Name+" has no saved value to check")
	}
	if srv.s.state == StateInstalling {
		return fail(ReasonInstallInProgress, "an install is running")
	}
	srv.evaluate(r.Context(), i, nodes)
	writeJSON(w, http.StatusOK, srv.result(i, nodes))
	return nil
}

type summaryView struct {
	CR       map[string]any `json:"cr"`
	SpecHash string         `json:"specHash"`
	Warnings []Check        `json:"warnings"`
}

func (srv *Server) getSummary(w http.ResponseWriter, _ *http.Request) error {
	srv.mu.Lock()
	defer srv.mu.Unlock()
	if j := srv.s.blockedBy(len(Steps)); j >= 0 {
		return fail(ReasonStepOutOfOrder, "step "+Steps[j].Name+" is not valid; every step must be valid before the summary")
	}
	if err := srv.s.editable(); err != nil {
		return err
	}
	cr := srv.s.cr(srv.apiVersion)
	hash, _, err := specHash(cr)
	if err != nil {
		return internal(err)
	}
	warnings := []Check{}
	for _, st := range srv.s.steps {
		if st.preflight == nil {
			continue
		}
		for _, c := range st.preflight.Checks {
			if c.Status != "pass" {
				warnings = append(warnings, c)
			}
		}
	}
	srv.s.state = StateConfirming
	writeJSON(w, http.StatusOK, summaryView{CR: cr, SpecHash: hash, Warnings: warnings})
	return nil
}

func (srv *Server) postInstall(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		Confirm  bool   `json:"confirm"`
		SpecHash string `json:"specHash"`
	}
	if err := decode(r, &body); err != nil {
		return err
	}
	if !body.Confirm {
		return fail(ReasonBadRequest, `"confirm": true is required`)
	}
	srv.mu.Lock()
	defer srv.mu.Unlock()
	switch srv.s.state {
	case StateConfirming:
	case StateInstalling:
		return fail(ReasonInstallInProgress, "an install is running")
	case StateSucceeded, StateFinished:
		return fail(ReasonAlreadyInstalled, "already installed")
	default:
		return fail(ReasonNotConfirmed, "read GET /api/v1/summary before the install")
	}
	hash, cr, err := specHash(srv.s.cr(srv.apiVersion))
	if err != nil {
		return internal(err)
	}
	if body.SpecHash != hash {
		return fail(ReasonSpecChanged, "the draft changed after the summary; read the summary again")
	}
	srv.s.state, srv.s.lastErr = StateInstalling, ""
	done := make(chan struct{})
	srv.installed = done
	go srv.install(cr, done)
	writeJSON(w, http.StatusAccepted, map[string]any{"state": StateInstalling, "specHash": hash})
	return nil
}

// install runs the install. Its log lines are dropped: the masked stream is #145, and the pod
// log must not carry them unmasked.
func (srv *Server) install(cr []byte, done chan struct{}) {
	defer close(done)
	res, err := srv.cluster.Install(srv.installCtx, cr, func(string) {})
	if err == nil && srv.credentials != nil {
		if cerr := srv.storeCredential(res.AdminUser); cerr != nil {
			srv.mu.Lock()
			defer srv.mu.Unlock()
			srv.s.state, srv.s.lastErr = StateFailed, "the install ran but the initial admin Secret "+cluster.InitialAdminSecret+" could not be created; install again"
			return
		}
	}
	srv.mu.Lock()
	defer srv.mu.Unlock()
	if err != nil {
		srv.s.state, srv.s.lastErr = StateFailed, "install failed; fix the draft or the cluster and install again"
		return
	}
	srv.s.state = StateSucceeded
	srv.s.result = resultView{ConsoleURL: res.ConsoleURL, AdminUser: res.AdminUser}
}

// Wait blocks until a started install ends. Tests and shutdown use it.
func (srv *Server) Wait() {
	srv.mu.Lock()
	done := srv.installed
	srv.mu.Unlock()
	if done != nil {
		<-done
	}
}

type resultView struct {
	State               State  `json:"state"`
	ConsoleURL          string `json:"consoleURL"`
	AdminUser           string `json:"adminUser"`
	CredentialAvailable bool   `json:"credentialAvailable"`
	Error               string `json:"error,omitempty"`
}

func (srv *Server) getResult(w http.ResponseWriter, _ *http.Request) error {
	srv.mu.Lock()
	defer srv.mu.Unlock()
	v := srv.s.result
	v.State, v.Error = srv.s.state, srv.s.lastErr
	v.CredentialAvailable = srv.credentials != nil && srv.s.state == StateSucceeded
	writeJSON(w, http.StatusOK, v)
	return nil
}
