package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/popododo0720/vws-deploy/installer/internal/client"
	"github.com/popododo0720/vws-deploy/installer/internal/tui"
)

// access reads the one-time access code and the certificate fingerprint the server wrote.
func access(dir string) (code, fingerprint string, err error) {
	c, err := os.ReadFile(filepath.Join(dir, "access-code"))
	if err != nil {
		return "", "", fmt.Errorf("read the access code (is the installer API running?): %w", err)
	}
	f, err := os.ReadFile(filepath.Join(dir, "fingerprint"))
	if err != nil {
		return "", "", fmt.Errorf("read the certificate fingerprint: %w", err)
	}
	return strings.TrimSpace(string(c)), strings.TrimSpace(string(f)), nil
}

// runPrintURL is vws init --print-url: where to connect, the access code and the fingerprint.
// It writes to the exec terminal only; the server never logs them.
func runPrintURL(args []string, out io.Writer) error {
	fs := flag.NewFlagSet("print-url", flag.ContinueOnError)
	dir := fs.String("state-dir", "/tmp/vws-installer", "installer state directory")
	if err := fs.Parse(args); err != nil {
		return err
	}
	code, fp, err := access(*dir)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(out, "url: https://localhost:8443 (after: kubectl -n vws port-forward svc/vws-installer 8443)\naccess code: %s (one use; read it again after each login)\ncertificate sha256: %s\n", code, fp)
	return err
}

// runTUI is vws init in the installer pod: the TUI against the local API.
func runTUI(args []string) error {
	fs := flag.NewFlagSet("tui", flag.ContinueOnError)
	dir := fs.String("state-dir", "/tmp/vws-installer", "installer state directory")
	url := fs.String("url", "https://127.0.0.1:8443", "installer API")
	if err := fs.Parse(args); err != nil {
		return err
	}
	code, fp, err := access(*dir)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	c := &client.Client{Base: *url, HTTP: client.PinnedHTTP(fp)}
	if err := c.Login(ctx, code); err != nil {
		return fmt.Errorf("login: %w", err)
	}
	err = tui.New(c, os.Stdin, os.Stdout).Run(ctx)
	if errors.Is(err, tui.ErrAborted) {
		fmt.Fprintln(os.Stdout, "nothing installed; the draft stays in the installer, run vws init again") //nolint:errcheck // terminal
		return nil
	}
	return err
}
