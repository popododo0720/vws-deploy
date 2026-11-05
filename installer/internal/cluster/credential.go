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

// InitialAdminSecret is the only place the initial admin password is stored (#146). vws apply
// creates it before the chart when console-api serves sign-in, and console-api reads its password
// as -root-password-file (#208). The installer only reads it.
const InitialAdminSecret = "vws-initial-admin"

// RootUser is the console-api account root that signs in with the Secret's password (#208).
const RootUser = "root"

// consumedLabel marks the Secret once POST /result/credential returned the password.
const consumedLabel = "vws-initial-admin-consumed"

// ErrNoCredential means InitialAdminSecret does not exist: console sign-in is off (-iam static).
var ErrNoCredential = errors.New("no initial admin secret")

// Credentials reads the initial admin credential.
type Credentials interface {
	// Load reads it and whether it was handed out already. ErrNoCredential when there is none.
	Load(ctx context.Context) (user, password string, consumed bool, err error)
	// MarkConsumed records that the password was handed out.
	MarkConsumed(ctx context.Context) error
}

// KubeSecret reads the credential in Secret InitialAdminSecret with kubectl. The password comes
// back on stdout, never in an argument, an error or a log.
type KubeSecret struct {
	Kubectl   string
	Namespace string
}

func (k KubeSecret) Load(ctx context.Context) (string, string, bool, error) {
	cmd := exec.CommandContext(ctx, k.Kubectl, "-n", k.Namespace, "get", "secret", InitialAdminSecret, "-o", "json")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		if strings.Contains(stderr.String(), "NotFound") {
			return "", "", false, ErrNoCredential
		}
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
	// The #146 installer wrote an empty username, and vws apply keeps an existing Secret; the
	// password still signs in as RootUser.
	if len(user) == 0 {
		user = []byte(RootUser)
	}
	return string(user), string(pw), s.Metadata.Labels[consumedLabel] == "true", nil
}

func (k KubeSecret) MarkConsumed(ctx context.Context) error {
	label := exec.CommandContext(ctx, k.Kubectl, "-n", k.Namespace, "label", "--overwrite", "secret",
		InitialAdminSecret, consumedLabel+"=true")
	if err := label.Run(); err != nil {
		return fmt.Errorf("kubectl label secret %s: %w", InitialAdminSecret, err)
	}
	return nil
}
