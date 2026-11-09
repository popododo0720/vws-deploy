package cluster

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
)

// AdminFieldManager is the field manager of every write the admin page makes, so the managed
// fields of VWSCluster vws and ConfigMap vws-volume-types show who changed the mapping.
const AdminFieldManager = "vws-admin"

// requestTimeout bounds each kubectl call of the page; a failed save is shown and the operator
// saves again, so there is no retry here.
const requestTimeout = "20s"

// Kube is the admin page's kubectl adapter in one namespace (docs/design/admin-volume-types.md).
type Kube struct {
	Kubectl   string
	Namespace string
}

// Get returns `kubectl get KIND NAME -o json` in the namespace.
func (k Kube) Get(ctx context.Context, kind, name string) ([]byte, error) {
	return k.run(ctx, "get", kind, name, "-o", "json")
}

// Names lists the names of a cluster-scoped kind, such as storageclasses.
func (k Kube) Names(ctx context.Context, kind string) ([]string, error) {
	out, err := k.run(ctx, "get", kind, "-o", "jsonpath={.items[*].metadata.name}")
	return strings.Fields(string(out)), err
}

// Patch applies a JSON patch to KIND NAME in the namespace as AdminFieldManager.
func (k Kube) Patch(ctx context.Context, kind, name string, patch []byte) error {
	_, err := k.run(ctx, "patch", kind, name, "--type=json", "--field-manager="+AdminFieldManager,
		"-p", string(patch))
	return err
}

func (k Kube) run(ctx context.Context, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, k.Kubectl,
		append([]string{"-n", k.Namespace, "--request-timeout=" + requestTimeout}, args...)...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("kubectl %s %s: %w: %s", args[0], args[1], err, strings.TrimSpace(stderr.String()))
	}
	return out, nil
}
