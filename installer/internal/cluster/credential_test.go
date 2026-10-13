package cluster

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestKubeSecret(t *testing.T) {
	dir := t.TempDir()
	args := filepath.Join(dir, "args")
	secret := filepath.Join(dir, "secret.json")
	labeled := filepath.Join(dir, "labeled")
	// A fake kubectl that keeps the created object and answers get from it.
	kubectl := fakeBin(t, dir, "kubectl", `echo "$*" >> `+args+`
case "$1 $2" in
"create -f") [ -f `+secret+` ] && { echo 'Error from server (AlreadyExists): secrets "vws-initial-admin" already exists' >&2; exit 1; }
  cat > `+secret+` ;;
"-n vws") case "$3" in
  get) [ -f `+secret+` ] || exit 1
    if [ -f `+labeled+` ]; then sed 's/"metadata":{/"metadata":{"labels":{"vws-initial-admin-consumed":"true"},/' `+secret+`; else cat `+secret+`; fi ;;
  label) touch `+labeled+` ;;
  *) exit 9 ;;
  esac ;;
*) exit 9 ;;
esac
`)
	k := KubeSecret{Kubectl: kubectl, Namespace: "vws"}
	ctx := context.Background()
	const pw = "Fake-Password-4nT8zQ2wXr"
	if _, _, _, err := k.Load(ctx); err == nil {
		t.Fatal("Load before Store succeeded")
	}
	if err := k.Store(ctx, "admin", pw); err != nil {
		t.Fatal(err)
	}
	if err := k.Store(ctx, "admin", "Other-Fake-Password"); !errors.Is(err, ErrCredentialExists) {
		t.Fatalf("second Store: %v", err)
	}
	user, got, consumed, err := k.Load(ctx)
	if err != nil || user != "admin" || got != pw || consumed {
		t.Fatalf("Load %q %v %v", user, consumed, err)
	}
	if err := k.MarkConsumed(ctx); err != nil {
		t.Fatal(err)
	}
	if _, _, consumed, err := k.Load(ctx); err != nil || !consumed {
		t.Fatalf("after MarkConsumed: %v %v", consumed, err)
	}
	raw, _ := os.ReadFile(args)
	if strings.Contains(string(raw), pw) || strings.Contains(string(raw), "Other-Fake") {
		t.Fatalf("the password reached kubectl's arguments:\n%s", raw)
	}
	stored, _ := os.ReadFile(secret)
	if !strings.Contains(string(stored), `"name":"vws-initial-admin"`) || !strings.Contains(string(stored), `"namespace":"vws"`) || strings.Contains(string(stored), pw) {
		t.Fatalf("secret %s (the value is base64 in data)", stored)
	}
}
