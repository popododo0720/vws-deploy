package cluster

import (
	"context"
	"encoding/base64"
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
	// A fake kubectl that answers get from the Secret file vws apply would have made.
	kubectl := fakeBin(t, dir, "kubectl", `echo "$*" >> `+args+`
case "$1 $2 $3" in
"-n vws get") [ -f `+secret+` ] || {
    echo 'Error from server (NotFound): secrets "vws-initial-admin" not found' >&2; exit 1; }
  [ -f `+secret+`.broken ] && { echo 'Unable to connect to the server' >&2; exit 1; }
  if [ -f `+labeled+` ]; then
    sed 's/"metadata":{/"metadata":{"labels":{"vws-initial-admin-consumed":"true"},/' `+secret+`
  else cat `+secret+`; fi ;;
"-n vws label") touch `+labeled+` ;;
*) exit 9 ;;
esac
`)
	k := KubeSecret{Kubectl: kubectl, Namespace: "vws"}
	ctx := context.Background()
	const pw = "Fake-Password-4nT8zQ2wXr"
	if _, _, _, err := k.Load(ctx); !errors.Is(err, ErrNoCredential) {
		t.Fatalf("Load without the Secret: %v", err)
	}
	data := `{"metadata":{"name":"vws-initial-admin"},"data":{"username":"` +
		base64.StdEncoding.EncodeToString([]byte("root")) + `","password":"` +
		base64.StdEncoding.EncodeToString([]byte(pw)) + `"}}`
	if err := os.WriteFile(secret, []byte(data), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(secret+".broken", nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := k.Load(ctx); err == nil || errors.Is(err, ErrNoCredential) {
		t.Fatalf("an unreachable apiserver must not read as no Secret: %v", err)
	}
	if err := os.Remove(secret + ".broken"); err != nil {
		t.Fatal(err)
	}
	user, got, consumed, err := k.Load(ctx)
	if err != nil || user != "root" || got != pw || consumed {
		t.Fatalf("Load %q %v %v", user, consumed, err)
	}
	legacy := `{"metadata":{"name":"vws-initial-admin"},"data":{"username":"","password":"` +
		base64.StdEncoding.EncodeToString([]byte(pw)) + `"}}`
	if err := os.WriteFile(secret, []byte(legacy), 0o600); err != nil {
		t.Fatal(err)
	}
	if user, got, _, err := k.Load(ctx); err != nil || user != RootUser || got != pw {
		t.Fatalf("a #146 Secret with an empty username: %q %v", user, err)
	}
	if err := k.MarkConsumed(ctx); err != nil {
		t.Fatal(err)
	}
	if _, _, consumed, err := k.Load(ctx); err != nil || !consumed {
		t.Fatalf("after MarkConsumed: %v %v", consumed, err)
	}
	raw, err := os.ReadFile(args)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), pw) {
		t.Fatalf("the password reached kubectl's arguments:\n%s", raw)
	}
}
