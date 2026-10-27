package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"

	"github.com/popododo0720/vws-lib/errs"
)

// Reason is the wire error code of the #139 error model.
type Reason string

// The reasons and the HTTP status and errs.Code each one carries.
const (
	ReasonValidationFailed  Reason = "validation_failed"
	ReasonBadRequest        Reason = "bad_request"
	ReasonUnauthenticated   Reason = "unauthenticated"
	ReasonSessionExpired    Reason = "session_expired"
	ReasonForbidden         Reason = "forbidden"
	ReasonNotFound          Reason = "not_found"
	ReasonMethodNotAllowed  Reason = "method_not_allowed"
	ReasonStepOutOfOrder    Reason = "step_out_of_order"
	ReasonSpecChanged       Reason = "spec_changed"
	ReasonInstallInProgress Reason = "install_in_progress"
	ReasonAlreadyInstalled  Reason = "already_installed"
	ReasonNotConfirmed      Reason = "not_confirmed"
	ReasonNotImplemented    Reason = "not_implemented"
	ReasonInternal          Reason = "internal"
	// ReasonCredentialConsumed: POST /result/credential after the one hand-out (#146).
	ReasonCredentialConsumed Reason = "credential_consumed"
)

var reasons = map[Reason]struct {
	status int
	code   errs.Code
}{
	ReasonValidationFailed:   {http.StatusBadRequest, errs.CodeInvalidArgument},
	ReasonBadRequest:         {http.StatusBadRequest, errs.CodeInvalidArgument},
	ReasonUnauthenticated:    {http.StatusUnauthorized, errs.CodeUnauthenticated},
	ReasonSessionExpired:     {http.StatusUnauthorized, errs.CodeUnauthenticated},
	ReasonForbidden:          {http.StatusForbidden, errs.CodePermissionDenied},
	ReasonNotFound:           {http.StatusNotFound, errs.CodeNotFound},
	ReasonMethodNotAllowed:   {http.StatusMethodNotAllowed, errs.CodeInvalidArgument},
	ReasonStepOutOfOrder:     {http.StatusConflict, errs.CodeConflict},
	ReasonSpecChanged:        {http.StatusConflict, errs.CodeConflict},
	ReasonInstallInProgress:  {http.StatusConflict, errs.CodeConflict},
	ReasonAlreadyInstalled:   {http.StatusConflict, errs.CodeConflict},
	ReasonNotConfirmed:       {http.StatusConflict, errs.CodeConflict},
	ReasonNotImplemented:     {http.StatusNotImplemented, errs.CodeUnimplemented},
	ReasonInternal:           {http.StatusInternalServerError, errs.CodeInternal},
	ReasonCredentialConsumed: {http.StatusGone, errs.CodeConflict},
}

// FieldError names one CR field that failed validation.
type FieldError struct {
	Field   string `json:"field"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Error is an errs.E with the wire reason and field errors.
type Error struct {
	*errs.E
	Reason Reason
	Fields []FieldError
}

func fail(reason Reason, message string, fields ...FieldError) *Error {
	return &Error{E: errs.New(reasons[reason].code, message), Reason: reason, Fields: fields}
}

func internal(cause error) *Error {
	return &Error{E: errs.Internal(cause), Reason: ReasonInternal}
}

type wireError struct {
	Error struct {
		Code      Reason       `json:"code"`
		Message   string       `json:"message"`
		Fields    []FieldError `json:"fields,omitempty"`
		Retryable bool         `json:"retryable"`
	} `json:"error"`
}

func writeError(w http.ResponseWriter, r *http.Request, err error) {
	var e *Error
	if !errors.As(err, &e) {
		e = internal(err)
	}
	if e.Reason == ReasonInternal {
		slog.LogAttrs(r.Context(), slog.LevelError, "installer api", slog.String("err", e.E.Error()))
	}
	var body wireError
	body.Error.Code, body.Error.Message, body.Error.Fields = e.Reason, e.Message, e.Fields
	body.Error.Retryable = e.Code.Retryable()
	if err := writeJSON(w, reasons[e.Reason].status, body); err != nil {
		slog.LogAttrs(r.Context(), slog.LevelError, "installer api", slog.String("err", err.Error()))
	}
}

// responseWriter records whether the handler already sent the response header, so the router
// does not write an error over a response that started (vws-deploy#387).
type responseWriter struct {
	http.ResponseWriter
	sent bool
}

func (w *responseWriter) WriteHeader(status int) {
	w.sent = true
	w.ResponseWriter.WriteHeader(status)
}

func (w *responseWriter) Write(p []byte) (int, error) {
	w.sent = true
	n, err := w.ResponseWriter.Write(p)
	if err != nil {
		return n, fmt.Errorf("write response: %w", err)
	}
	return n, nil
}

// Flush keeps the event stream's http.Flusher assertion working through the wrapper.
func (w *responseWriter) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		w.sent = true
		f.Flush()
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) error {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		return fmt.Errorf("write JSON response: %w", err)
	}
	return nil
}
