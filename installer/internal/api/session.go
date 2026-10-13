package api

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"
)

// State is the draft session state (#139 상태 기계):
//
//	editing -> confirming -> installing -> succeeded | failed
//	confirming -> editing (a step is saved again)
//	failed -> editing (retry; apply is idempotent)
//	succeeded -> finished is the credential hand-off of #146.
type State string

// The session states.
const (
	StateEditing    State = "editing"
	StateConfirming State = "confirming"
	StateInstalling State = "installing"
	StateSucceeded  State = "succeeded"
	StateFailed     State = "failed"
	StateFinished   State = "finished"
)

// StepStatus is pending until a value is saved, then valid or invalid.
type StepStatus string

// The step statuses.
const (
	StepPending StepStatus = "pending"
	StepValid   StepStatus = "valid"
	StepInvalid StepStatus = "invalid"
)

// Check is one preflight result (#139 PreflightReport).
type Check struct {
	ID       string `json:"id"`
	Severity string `json:"severity"`
	Status   string `json:"status"`
	Node     string `json:"node,omitempty"`
	Field    string `json:"field,omitempty"`
	Message  string `json:"message"`
	Hint     string `json:"hint,omitempty"`
}

// Blocks reports whether the check stops the next step: fail with severity error.
func (c Check) Blocks() bool { return c.Status == "fail" && c.Severity == "error" }

// PreflightReport is the preflight result of one step.
type PreflightReport struct {
	RanAt  time.Time `json:"ranAt"`
	Checks []Check   `json:"checks"`
}

// step is the saved state of one wizard step.
type step struct {
	status    StepStatus
	raw       json.RawMessage
	value     any
	errors    []FieldError
	preflight *PreflightReport
}

// session is the one draft (a singleton). Server.mu guards it.
type session struct {
	state   State
	steps   []step
	lastErr string
	result  resultView
}

func newSession() *session {
	s := &session{state: StateEditing, steps: make([]step, len(Steps))}
	for i := range s.steps {
		s.steps[i].status = StepPending
	}
	return s
}

// editable answers whether steps may change, moving confirming and failed back to editing.
func (s *session) editable() *Error {
	switch s.state {
	case StateEditing:
	case StateConfirming, StateFailed:
		s.state = StateEditing
	case StateInstalling:
		return fail(ReasonInstallInProgress, "an install is running")
	default:
		return fail(ReasonAlreadyInstalled, "the install has finished; the draft can no longer change")
	}
	return nil
}

// blockedBy returns the first step before i that is not valid, or -1.
func (s *session) blockedBy(i int) int {
	for j := 0; j < i; j++ {
		if s.steps[j].status != StepValid {
			return j
		}
	}
	return -1
}

func (s *session) network() Network {
	n, _ := s.steps[stepIndex("network")].value.(Network)
	return n
}

// cr builds the VWSCluster CR from every valid step.
func (s *session) cr(apiVersion string) map[string]any {
	spec := map[string]any{}
	for i, st := range s.steps {
		if st.status == StepValid {
			Steps[i].Apply(spec, st.value)
		}
	}
	return map[string]any{
		"apiVersion": apiVersion,
		"kind":       "VWSCluster",
		"metadata":   map[string]any{"name": "vws", "namespace": "vws"},
		"spec":       spec,
	}
}

// specHash is the sha256 of the canonical CR JSON: marshalled once, read back into maps and
// marshalled again, so struct field order does not matter and encoding/json sorts every key.
// A client that hashes the summary CR the same way gets the same value.
func specHash(cr map[string]any) (string, []byte, error) {
	b, err := json.Marshal(cr)
	if err != nil {
		return "", nil, err
	}
	var plain any
	if err := json.Unmarshal(b, &plain); err != nil {
		return "", nil, err
	}
	if b, err = json.Marshal(plain); err != nil {
		return "", nil, err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), b, nil
}
