# Retired platform bundle

VWS runs on Proxmox without Kubernetes. Operators register regions and AWS accounts
as described in [operator/regions.md](operator/regions.md).

Issue #773 removes the platform manifests, renderer, component pins, installer
bundle copy, bundle version drift checks and Kube-OVN gateway image generation.
Issue #774 removes the Helm chart and its CRDs; #775 removes the Kubernetes installer, CLI and this page.
