// Command vws-installer serves the installer API (vws-deploy#139, #141) on TLS with a
// self-signed certificate. The one-time access code and the certificate fingerprint go to files
// that only kubectl exec reaches (vws init --print-url, #142), never to the log.
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"math/big"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/popododo0720/vws-lib/obs"
	"github.com/popododo0720/vws-lib/obs/httpobs"

	"github.com/popododo0720/vws-deploy/installer/internal/api"
	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
	"github.com/popododo0720/vws-deploy/installer/internal/preflight"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "vws-installer:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	fs := flag.NewFlagSet("vws-installer", flag.ContinueOnError)
	listen := fs.String("listen", "0.0.0.0:8443", "API listen address (the Service port api)")
	dir := fs.String("state-dir", "/tmp/vws-installer", "directory for the access code, the fingerprint and the CR file")
	kubectl := fs.String("kubectl", "kubectl", "kubectl binary")
	vws := fs.String("vws", "vws", "vws CLI binary")
	links := fs.String("links", "", "optional talosctl get links -o json file for the NIC checks (#128, #143)")
	apiVersion := fs.String("api-version", "", "VWSCluster apiVersion (group/version from the CRD); required")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if !strings.Contains(*apiVersion, "/") {
		return errors.New("-api-version GROUP/VERSION is required")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	shutdown, err := obs.Setup(ctx, obs.ForService("vws-installer"))
	if err != nil {
		return err
	}
	defer shutdown(context.Background()) //nolint:errcheck // exit path
	if err := os.MkdirAll(*dir, 0o700); err != nil {
		return err
	}
	auth, err := api.NewAuth(time.Now, func(code string) error {
		return writeSecretFile(filepath.Join(*dir, "access-code"), code)
	})
	if err != nil {
		return fmt.Errorf("access code: %w", err)
	}
	cert, fingerprint, err := selfSigned()
	if err != nil {
		return fmt.Errorf("certificate: %w", err)
	}
	if err := writeSecretFile(filepath.Join(*dir, "fingerprint"), fingerprint); err != nil {
		return err
	}
	cl := cluster.VWS{Kubectl: *kubectl, VWS: *vws, WorkDir: *dir, Links: *links}
	srv := api.New(api.Config{
		Auth:       auth,
		Cluster:    cl,
		Preflight:  preflight.Engine{StorageClasses: cl.StorageClasses}.Run,
		Context:    ctx,
		APIVersion: *apiVersion,
	})
	hs := &http.Server{
		Addr:              *listen,
		Handler:           httpobs.Middleware("vws-installer", api.Route)(srv.Handler()),
		TLSConfig:         &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12},
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		<-ctx.Done()
		_ = hs.Shutdown(context.Background())
	}()
	slog.LogAttrs(ctx, slog.LevelInfo, "installer api listening", slog.String("addr", *listen))
	if err := hs.ListenAndServeTLS("", ""); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	srv.Wait()
	return nil
}

// writeSecretFile replaces path atomically with mode 0600.
func writeSecretFile(path, value string) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(value+"\n"), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// selfSigned makes a fresh ECDSA P-256 certificate for vws-installer and returns its SHA-256
// fingerprint, which vws init --print-url shows so the user can check the browser warning.
func selfSigned() (tls.Certificate, string, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 127))
	if err != nil {
		return tls.Certificate{}, "", err
	}
	tpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: "vws-installer"},
		DNSNames:     []string{"localhost", "vws-installer", "vws-installer.vws", "vws-installer.vws.svc"},
		NotBefore:    time.Now().Add(-time.Minute),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	sum := sha256.Sum256(der)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, hex.EncodeToString(sum[:]), nil
}
