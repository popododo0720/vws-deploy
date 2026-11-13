// Build inside the pinned console-api module so the fixture uses its real
// Proxmox backend and EC2 Query surface, with only the PVE transport faked.
package main

import (
	"context"
	"fmt"
	"net/http/httptest"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/popododo0720/vws-console-api/internal/backend"
	"github.com/popododo0720/vws-console-api/internal/backend/proxmox"
	"github.com/popododo0720/vws-console-api/internal/backend/proxmox/pveclient"
	"github.com/popododo0720/vws-console-api/internal/backend/proxmox/pveclient/pvefake"
	"github.com/popododo0720/vws-console-api/internal/cloud"
	"github.com/popododo0720/vws-console-api/internal/iam"
	"github.com/popododo0720/vws-console-api/internal/resource"
	"github.com/popododo0720/vws-console-api/internal/server"
	"github.com/popododo0720/vws-console-api/internal/sigv4"
	"github.com/popododo0720/vws-console-api/internal/surface/ec2query"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	scenario := pvefake.Scenario{
		Version: pvefake.Version{Version: "9.2", Release: "9.2"}, Cluster: "test",
		Nodes:    []pvefake.Node{{Name: "pve1", MaxCPU: 8, MaxMem: 32 << 30}},
		Storages: []pvefake.Storage{{Node: "pve1", Name: "local", MaxDisk: 256 << 30}},
		Networks: []pvefake.Network{{Node: "pve1", Iface: "vmbr0", Type: "bridge", CIDR: "192.0.2.1/24"}},
		Pools:    []string{"123456789012", "images"},
		Roles: []pvefake.Role{
			{ID: "Audit", Privs: []string{"Sys.Audit", "VM.Audit", "Datastore.Audit", "Pool.Audit"}},
			{ID: "Compat", Privs: []string{
				"VM.Audit", "Pool.Audit", "VM.Clone", "VM.Allocate", "VM.PowerMgmt", "VM.Config.Disk", "VM.Config.Options",
			}},
			{ID: "Storage", Privs: []string{"Datastore.Audit", "Datastore.AllocateSpace"}},
		},
		Tokens: []pvefake.Token{{ID: "vws@pve!compat", Secret: "fake-secret",
			ACL: []pvefake.ACE{
				{Path: "/", Role: "Audit"}, {Path: "/pool/123456789012", Role: "Compat"},
				{Path: "/pool/images", Role: "Compat"}, {Path: "/storage/local", Role: "Storage"},
			}}},
		VMs: []pvefake.VM{{
			VMID: 100, Node: "pve1", Name: "compat-template", Pool: "images", Template: true,
			Config: map[string]string{
				"smbios1": "uuid=5f0c3e9a-8b5d-4b8e-9a51-0c2d6c1f7a10",
				"cores":   "1", "memory": "1024", "scsi0": "local:vm-100-disk-0,size=8G",
			},
		}},
	}
	fake, err := pvefake.New(scenario)
	if err != nil {
		return fmt.Errorf("pvefake scenario: %w", err)
	}
	pve := httptest.NewTLSServer(fake)
	defer pve.Close()
	transport := pve.Client()
	transport.Timeout = 10 * time.Second
	client, err := pveclient.New(pveclient.Config{
		BaseURL: pve.URL, TokenID: scenario.Tokens[0].ID,
		Secret: pveclient.Secret(scenario.Tokens[0].Secret), HTTPClient: transport,
		PollInterval: time.Millisecond,
	})
	if err != nil {
		return fmt.Errorf("pve client: %w", err)
	}
	region := backend.ProxmoxRegion{Name: "pve-test-1", Zones: []backend.Zone{{Letter: "a", Nodes: []string{"pve1"}}}}
	ids, err := proxmox.NewIDs("test")
	if err != nil {
		return err
	}
	image, err := ids.Image("5f0c3e9a-8b5d-4b8e-9a51-0c2d6c1f7a10")
	if err != nil {
		return err
	}
	fmt.Printf("VWS_COMPAT_IMAGE_ID=%s\n", image)
	b, err := proxmox.New(ctx, client, proxmox.Registration{Region: region, ClusterName: "test"},
		proxmox.RegionConfig{
			Storages: map[string]string{"gp2": "local"}, Templates: proxmox.TemplateConfig{Pool: "images"},
			DefaultBridge: "vmbr0",
		})
	if err != nil {
		return fmt.Errorf("proxmox backend: %w", err)
	}
	account, err := resource.Parse[resource.AccountID]("123456789012")
	if err != nil {
		return err
	}
	auth := iam.NewStatic("AKIDEXAMPLE", "secret", account)
	regions := backend.NewStaticRegions([]backend.ProxmoxRegion{region}, nil)
	svc := cloud.New(cloud.Config{Backends: b.Set(), Regions: regions, Capabilities: b})
	handler := ec2query.New(ec2query.Config{
		Service: svc, Authorizer: auth,
		Verifier: sigv4.NewVerifier(sigv4.Verifier{
			Service: sigv4.ServiceEC2, Regions: regions, Auth: iam.Authenticator(auth),
		}),
	})
	return server.Serve(ctx, server.Options{
		OpsAddr:  "127.0.0.1:18083",
		Surfaces: []server.Surface{{Name: "ec2", Addr: "127.0.0.1:18081", Handler: handler}},
	})
}
