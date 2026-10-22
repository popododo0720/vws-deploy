package api

import (
	"encoding/json"
	"net/http"
	"slices"
	"sort"
)

// GET /cr exports the draft as the VWSCluster CR file for `vws apply --unattended -f` (#144).
// Every step must be valid, so the file is the one the summary would install.
func (srv *Server) getCR(w http.ResponseWriter, _ *http.Request) error {
	srv.mu.Lock()
	defer srv.mu.Unlock()
	if j := srv.s.blockedBy(len(Steps)); j >= 0 {
		return fail(ReasonStepOutOfOrder, "step "+Steps[j].Name+" is not valid; every step must be valid to export the CR")
	}
	w.Header().Set("Content-Disposition", `attachment; filename="vwscluster.json"`)
	writeJSON(w, http.StatusOK, srv.s.cr(srv.apiVersion))
	return nil
}

// crSpecKeys are the spec fields the draft holds. version, services and secrets are not wizard
// steps; a CR with them goes through `vws apply --unattended -f` directly.
var crSpecKeys = []string{"profile", "storage", "network", "observability"}

// splitCR turns a CR into one raw value per step: spec.network.nodes.external.eipPool becomes
// the eip step, and roles keeps the saved value or defaults to every node (roles are not in the CR).
func splitCR(cr map[string]json.RawMessage, apiVersion string, roles json.RawMessage) (map[string]json.RawMessage, []FieldError) {
	var errs []FieldError
	var gotVersion string
	if raw := cr["apiVersion"]; len(raw) == 0 {
		errs = append(errs, FieldError{Field: "apiVersion", Code: "invalid", Message: "want " + apiVersion})
	} else if err := json.Unmarshal(raw, &gotVersion); err != nil {
		errs = append(errs, FieldError{Field: "apiVersion", Code: "invalid", Message: err.Error()})
	} else if gotVersion != apiVersion {
		errs = append(errs, FieldError{Field: "apiVersion", Code: "invalid", Message: "want " + apiVersion})
	}
	var kind string
	if raw := cr["kind"]; len(raw) == 0 {
		errs = append(errs, FieldError{Field: "kind", Code: "invalid", Message: "want VWSCluster"})
	} else if err := json.Unmarshal(raw, &kind); err != nil {
		errs = append(errs, FieldError{Field: "kind", Code: "invalid", Message: err.Error()})
	} else if kind != "VWSCluster" {
		errs = append(errs, FieldError{Field: "kind", Code: "invalid", Message: "want VWSCluster"})
	}
	if raw := cr["metadata"]; len(raw) > 0 {
		var meta struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(raw, &meta); err != nil {
			errs = append(errs, FieldError{Field: "metadata", Code: "invalid", Message: err.Error()})
		} else if meta.Name != "" && meta.Name != "vws" {
			errs = append(errs, FieldError{Field: "metadata.name", Code: "invalid", Message: "the VWSCluster is named vws"})
		}
	}
	var spec map[string]json.RawMessage
	if err := json.Unmarshal(cr["spec"], &spec); err != nil || spec == nil {
		return nil, append(errs, required("spec"))
	}
	var extra []string
	for k := range spec {
		if !slices.Contains(crSpecKeys, k) {
			extra = append(extra, k)
		}
	}
	sort.Strings(extra)
	for _, k := range extra {
		errs = append(errs, FieldError{Field: "spec." + k, Code: "unsupported", Message: "the installer draft has no spec." + k + "; use vws apply --unattended -f for this CR"})
	}
	out := map[string]json.RawMessage{"roles": roles}
	for _, k := range []string{"profile", "storage", "observability"} {
		if v, ok := spec[k]; ok {
			out[k] = v
		}
	}
	if out["observability"] == nil {
		out["observability"] = json.RawMessage(`{"otlpEndpoint":""}`)
	}
	if raw, ok := spec["network"]; ok {
		var network map[string]any
		if err := json.Unmarshal(raw, &network); err != nil {
			errs = append(errs, FieldError{Field: "spec.network", Code: "invalid", Message: err.Error()})
		} else {
			if nodes, _ := network["nodes"].(map[string]any); nodes != nil {
				if ext, _ := nodes["external"].(map[string]any); ext != nil {
					if pool, ok := ext["eipPool"]; ok {
						poolRaw, err := json.Marshal(pool)
						if err != nil {
							errs = append(errs, FieldError{
								Field: "spec.network.nodes.external.eipPool", Code: "invalid", Message: err.Error(),
							})
						} else {
							out["eip"] = poolRaw
						}
						delete(ext, "eipPool")
					}
				}
			}
			networkRaw, err := json.Marshal(network)
			if err != nil {
				errs = append(errs, FieldError{Field: "spec.network", Code: "invalid", Message: err.Error()})
			} else {
				out["network"] = networkRaw
			}
		}
	}
	return out, errs
}

// validateCR decodes every CR step except roles. It needs no cluster, so a body error is found
// before the node lookup and a failing lookup cannot turn a 400 into a 500.
func validateCR(raws map[string]json.RawMessage) []FieldError {
	var fields []FieldError
	var network Network
	for _, s := range Steps {
		if s.Name == "roles" {
			continue
		}
		raw := raws[s.Name]
		if raw == nil {
			fields = append(fields, required(stepField(s.Name)))
			continue
		}
		v, errs := s.Decode(raw, Input{Network: network})
		fields = append(fields, errs...)
		if s.Name == "network" {
			if n, ok := v.(Network); ok {
				network = n
			}
		}
	}
	return fields
}

// POST /cr fills every step from a CR file and checks them all (#144). A body with an invalid
// field is refused before the node lookup and changes no step; the answer lists every field error.
func (srv *Server) postCR(w http.ResponseWriter, r *http.Request) error {
	var cr map[string]json.RawMessage
	if err := decode(r, &cr); err != nil {
		return err
	}
	srv.mu.Lock()
	defer srv.mu.Unlock()
	if err := srv.s.editable(); err != nil {
		return err
	}
	roles := srv.s.steps[stepIndex("roles")].raw
	raws, fields := splitCR(cr, srv.apiVersion, roles)
	if raws == nil {
		return fail(ReasonValidationFailed, "the CR is not a VWSCluster", fields...)
	}
	if fields = append(fields, validateCR(raws)...); len(fields) > 0 {
		return fail(ReasonValidationFailed, "the CR has invalid or missing fields", fields...)
	}
	nodes, err := srv.nodes(r.Context())
	if err != nil {
		return err
	}
	if srv.s.steps[stepIndex("roles")].status != StepValid {
		roles, err = json.Marshal(defaultRoles(nodes))
		if err != nil {
			return fail(ReasonInternal, "default roles: "+err.Error())
		}
		raws["roles"] = roles
	}
	for i, s := range Steps {
		srv.s.steps[i] = step{status: StepPending, raw: raws[s.Name]}
	}
	for i := range Steps {
		srv.evaluate(r.Context(), i, nodes)
		fields = append(fields, srv.s.steps[i].errors...)
	}
	if len(fields) > 0 {
		return fail(ReasonValidationFailed, "the CR has invalid or missing fields", fields...)
	}
	writeJSON(w, http.StatusOK, srv.stateView())
	return nil
}

func stepField(step string) string {
	switch step {
	case "eip":
		return "spec.network.nodes.external.eipPool"
	case "roles":
		return "roles.nodes"
	}
	return "spec." + step
}
