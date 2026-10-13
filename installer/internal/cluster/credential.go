package cluster

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"strings"
)

// InitialAdminSecret is the only place the initial admin password is stored (#146).
const InitialAdminSecret = "vws-initial-admin"

// consumedLabel marks the Secret once POST /result/credential returned the password.
const consumedLabel = "vws-initial-admin-consumed"

// ErrCredentialExists means InitialAdminSecret was already there; the stored one stays.
var ErrCredentialExists = errors.New("the initial admin secret already exists")

// Credentials stores and reads the initial admin credential.
type Credentials interface {
	// Store creates the Secret. It returns ErrCredentialExists and changes nothing when one is
	// already there.
	Store(ctx context.Context, user, password string) error
	// Load reads it back and whether it was handed out already.
	Load(ctx context.Context) (user, password string, consumed bool, err error)
	// MarkConsumed records that the password was handed out.
	MarkConsumed(ctx context.Context) error
}

// KubeSecret keeps the credential in Secret InitialAdminSecret with kubectl. The password goes
// to kubectl on stdin and comes back on stdout, never in an argument, an error or a log.
type KubeSecret struct {
	Kubectl   string
	Namespace string
}

func (k KubeSecret) Store(ctx context.Context, user, password string) error {
	manifest, err := json.Marshal(map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"type":       "Opaque",
		"metadata":   map[string]any{"name": InitialAdminSecret, "namespace": k.Namespace},
		"data": map[string]string{
			"username": base64.StdEncoding.EncodeToString([]byte(user)),
			"password": base64.StdEncoding.EncodeToString([]byte(password)),
		},
	})
	if err != nil {
		return err
	}
	cmd := exec.CommandContext(ctx, k.Kubectl, "create", "-f", "-")
	cmd.Stdin = bytes.NewReader(manifest)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if strings.Contains(stderr.String(), "AlreadyExists") {
			return ErrCredentialExists
		}
		// kubectl's stderr names the object, not the data; it is still left out.
		return fmt.Errorf("kubectl create secret %s: %w", InitialAdminSecret, err)
	}
	return nil
}

func (k KubeSecret) Load(ctx context.Context) (string, string, bool, error) {
	out, err := exec.CommandContext(ctx, k.Kubectl, "-n", k.Namespace, "get", "secret", InitialAdminSecret, "-o", "json").Output()
	if err != nil {
		return "", "", false, fmt.Errorf("kubectl get secret %s: %w", InitialAdminSecret, err)
	}
	var s struct {
		Metadata struct {
			Labels map[string]string `json:"labels"`
		} `json:"metadata"`
		Data map[string]string `json:"data"`
	}
	if err := json.Unmarshal(out, &s); err != nil {
		return "", "", false, errors.New("secret " + InitialAdminSecret + " is not JSON")
	}
	user, err1 := base64.StdEncoding.DecodeString(s.Data["username"])
	pw, err2 := base64.StdEncoding.DecodeString(s.Data["password"])
	if err1 != nil || err2 != nil || len(pw) == 0 {
		return "", "", false, errors.New("secret " + InitialAdminSecret + " has no username and password")
	}
	return string(user), string(pw), s.Metadata.Labels[consumedLabel] == "true", nil
}

func (k KubeSecret) MarkConsumed(ctx context.Context) error {
	if err := exec.CommandContext(ctx, k.Kubectl, "-n", k.Namespace, "label", "--overwrite", "secret", InitialAdminSecret, consumedLabel+"=true").Run(); err != nil {
		return fmt.Errorf("kubectl label secret %s: %w", InitialAdminSecret, err)
	}
	return nil
}
