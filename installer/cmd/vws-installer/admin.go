package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/popododo0720/vws-lib/obs"
	"github.com/popododo0720/vws-lib/obs/httpobs"

	"github.com/popododo0720/vws-deploy/installer/internal/admin"
	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

// runAdmin serves the admin page (vws-installer admin, docs/design/admin-volume-types.md) on a
// loopback address only. /healthz and /readyz are on VWS_HEALTH_ADDR (obs.Setup), not here.
func runAdmin(args []string) error {
	fs := flag.NewFlagSet("vws-installer admin", flag.ContinueOnError)
	listen := fs.String("listen", "127.0.0.1:8080", "page listen address; must be a loopback IP")
	namespace := fs.String("namespace", "vws", "namespace of VWSCluster vws and ConfigMap "+admin.ConfigMap)
	kubectl := fs.String("kubectl", "kubectl", "kubectl binary")
	if err := fs.Parse(args); err != nil {
		return fmt.Errorf("flags: %w", err)
	}
	if err := loopback(*listen); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	shutdown, err := obs.Setup(ctx, obs.ForService("vws-admin"))
	if err != nil {
		return fmt.Errorf("obs setup: %w", err)
	}
	defer shutdown(context.Background()) //nolint:errcheck // exit path
	hs := &http.Server{
		Addr: *listen,
		Handler: httpobs.Middleware("vws-admin", func(*http.Request) string { return "/" })(
			admin.Handler(cluster.Kube{Kubectl: *kubectl, Namespace: *namespace})),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		<-ctx.Done()
		if err := hs.Shutdown(context.WithoutCancel(ctx)); err != nil {
			slog.LogAttrs(ctx, slog.LevelWarn, "admin page shutdown", slog.String("err", err.Error()))
		}
	}()
	slog.LogAttrs(ctx, slog.LevelInfo, "admin page listening", slog.String("addr", *listen))
	if err := hs.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("serve: %w", err)
	}
	return nil
}

// loopback refuses a listen address that another pod could reach: the page has no auth of its
// own, kubectl port-forward to the pod loopback is the boundary.
func loopback(listen string) error {
	ap, err := netip.ParseAddrPort(listen)
	if err != nil || !ap.Addr().IsLoopback() {
		return fmt.Errorf("-listen %s: must be a loopback IP and port, such as 127.0.0.1:8080", listen)
	}
	return nil
}
