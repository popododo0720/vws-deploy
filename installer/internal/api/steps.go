package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/netip"
	"regexp"
	"slices"
	"sort"

	"github.com/popododo0720/vws-deploy/installer/internal/cluster"
)

// Step is one wizard step. The order of Steps is the order the wizard walks; a step can be saved
// only when every step before it is valid. Each step's value is the CR part it maps to (#139).
type Step struct {
	Name string
	// Decode parses and checks a value. It returns the normalized value and field errors.
	Decode func(raw json.RawMessage, in Input) (any, []FieldError)
	// Apply writes a valid value into the CR spec.
	Apply    func(spec map[string]any, value any)
	Defaults func(nodes []cluster.Node) any
	Options  func(nodes []cluster.Node) any
}

// Input is what a step checks a value against: the cluster nodes and the saved network step.
type Input struct {
	Nodes   []cluster.Node
	Network Network
}

// Steps is the fixed step order.
var Steps = []Step{
	{Name: "roles", Decode: decodeRoles, Apply: func(map[string]any, any) {}, Defaults: defaultRoles, Options: nodeOptions},
	{Name: "network", Decode: decodeNetwork, Apply: setPath("network"), Defaults: constant(Network{Driver: "kube-ovn"}), Options: nicOptions},
	{Name: "storage", Decode: decodeStorage, Apply: setPath("storage"), Defaults: constant(Storage{Driver: "rook-ceph"}), Options: storageOptions},
	{Name: "eip", Decode: decodeEIP, Apply: applyEIP, Defaults: constant(""), Options: constant(nil)},
	{Name: "profile", Decode: decodeProfile, Apply: setPath("profile"), Defaults: defaultProfile, Options: constant([]string{"single-node", "small", "ha"})},
	{Name: "observability", Decode: decodeObservability, Apply: setPath("observability"), Defaults: constant(Observability{}), Options: constant(nil)},
}

func stepIndex(name string) int {
	return slices.IndexFunc(Steps, func(s Step) bool { return s.Name == name })
}

func constant(v any) func([]cluster.Node) any { return func([]cluster.Node) any { return v } }

func setPath(key string) func(map[string]any, any) {
	return func(spec map[string]any, value any) { spec[key] = value }
}

// strict decodes raw into v and rejects unknown fields and trailing data.
func strict(raw json.RawMessage, v any) []FieldError {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return []FieldError{{Field: "value", Code: "invalid", Message: err.Error()}}
	}
	if dec.More() {
		return []FieldError{{Field: "value", Code: "invalid", Message: "trailing data after the value"}}
	}
	return nil
}

func required(field string) FieldError {
	return FieldError{Field: field, Code: "required", Message: field + " is required"}
}

// Roles confirms the nodes the install uses. Roles are read from the node labels the cluster
// already has and are not stored in the CR (#192, option B); the step only checks that every
// named node exists.
type Roles struct {
	Nodes []string `json:"nodes"`
}

func decodeRoles(raw json.RawMessage, in Input) (any, []FieldError) {
	nodes := in.Nodes
	var v Roles
	if errs := strict(raw, &v); errs != nil {
		return nil, errs
	}
	if len(v.Nodes) == 0 {
		return v, []FieldError{required("roles.nodes")}
	}
	var out []FieldError
	for i, name := range v.Nodes {
		if !slices.ContainsFunc(nodes, func(n cluster.Node) bool { return n.Name == name }) {
			out = append(out, FieldError{Field: fmt.Sprintf("roles.nodes[%d]", i), Code: "not_found", Message: "no node " + name + " in the cluster"})
		}
	}
	return v, out
}

func defaultRoles(nodes []cluster.Node) any {
	v := Roles{Nodes: []string{}}
	for _, n := range nodes {
		v.Nodes = append(v.Nodes, n.Name)
	}
	return v
}

func nodeOptions(nodes []cluster.Node) any { return map[string]any{"nodes": nodes} }

// Attachment is one node network (#125).
type Attachment struct {
	NIC        string   `json:"nic"`
	VLAN       int      `json:"vlan,omitempty"`
	CIDR       string   `json:"cidr"`
	MTU        int      `json:"mtu,omitempty"`
	Gateway    string   `json:"gateway,omitempty"`
	ExcludeIPs []string `json:"excludeIPs,omitempty"`
	EIPPool    string   `json:"eipPool,omitempty"`
}

// StorageNets is spec.network.nodes.storage.
type StorageNets struct {
	Public  *Attachment `json:"public"`
	Cluster *Attachment `json:"cluster,omitempty"`
}

// NodeNets is spec.network.nodes.
type NodeNets struct {
	Management *Attachment  `json:"management"`
	Tunnel     *Attachment  `json:"tunnel,omitempty"`
	Storage    *StorageNets `json:"storage,omitempty"`
	External   *Attachment  `json:"external,omitempty"`
}

// Direct is one spec.network.direct entry.
type Direct struct {
	Attachment
	Name string `json:"name"`
	Mode string `json:"mode,omitempty"`
}

// Network is spec.network.
type Network struct {
	Driver string    `json:"driver,omitempty"`
	Nodes  *NodeNets `json:"nodes,omitempty"`
	Direct []Direct  `json:"direct,omitempty"`
}

func checkAttachment(field string, a *Attachment, gateway bool) []FieldError {
	var out []FieldError
	if a.NIC == "" {
		out = append(out, required(field+".nic"))
	}
	prefix, err := netip.ParsePrefix(a.CIDR)
	if a.CIDR == "" {
		out = append(out, required(field+".cidr"))
	} else if err != nil {
		out = append(out, FieldError{Field: field + ".cidr", Code: "invalid", Message: "not a CIDR"})
	}
	if a.VLAN < 0 || a.VLAN > 4094 {
		out = append(out, FieldError{Field: field + ".vlan", Code: "out_of_range", Message: "vlan is 0 (untagged) to 4094"})
	}
	if a.MTU != 0 && (a.MTU < 1280 || a.MTU > 9216) {
		out = append(out, FieldError{Field: field + ".mtu", Code: "out_of_range", Message: "mtu is 1280 to 9216"})
	}
	if gateway {
		gw, gerr := netip.ParseAddr(a.Gateway)
		switch {
		case a.Gateway == "":
			out = append(out, required(field+".gateway"))
		case gerr != nil:
			out = append(out, FieldError{Field: field + ".gateway", Code: "invalid", Message: "not an IP address"})
		case err == nil && !prefix.Contains(gw):
			out = append(out, FieldError{Field: field + ".gateway", Code: "invalid", Message: "gateway is outside " + a.CIDR})
		}
	}
	return out
}

func decodeNetwork(raw json.RawMessage, _ Input) (any, []FieldError) {
	var v Network
	if errs := strict(raw, &v); errs != nil {
		return nil, errs
	}
	var out []FieldError
	if v.Driver == "" {
		v.Driver = "kube-ovn"
	}
	if v.Driver != "kube-ovn" {
		out = append(out, FieldError{Field: "spec.network.driver", Code: "unsupported", Message: "only kube-ovn"})
	}
	if n := v.Nodes; n != nil {
		if n.Management == nil {
			out = append(out, required("spec.network.nodes.management"))
		} else {
			out = append(out, checkAttachment("spec.network.nodes.management", n.Management, false)...)
		}
		if n.Tunnel != nil {
			out = append(out, checkAttachment("spec.network.nodes.tunnel", n.Tunnel, false)...)
		}
		if n.Storage != nil {
			if n.Storage.Public == nil {
				out = append(out, required("spec.network.nodes.storage.public"))
			} else {
				out = append(out, checkAttachment("spec.network.nodes.storage.public", n.Storage.Public, false)...)
			}
			if n.Storage.Cluster != nil {
				out = append(out, checkAttachment("spec.network.nodes.storage.cluster", n.Storage.Cluster, false)...)
			}
		}
		if n.External != nil {
			out = append(out, checkAttachment("spec.network.nodes.external", n.External, true)...)
			if n.External.EIPPool != "" {
				out = append(out, FieldError{Field: "spec.network.nodes.external.eipPool", Code: "wrong_step", Message: "set the elastic IP pool in the eip step"})
			}
		}
	}
	seen := map[string]bool{}
	for i := range v.Direct {
		d := &v.Direct[i]
		field := fmt.Sprintf("spec.network.direct[%d]", i)
		if d.Name == "" {
			out = append(out, required(field+".name"))
		} else if seen[d.Name] {
			out = append(out, FieldError{Field: field + ".name", Code: "duplicate", Message: "direct network names are unique"})
		}
		seen[d.Name] = true
		if d.Mode != "" && d.Mode != "ovn-underlay" {
			out = append(out, FieldError{Field: field + ".mode", Code: "unsupported", Message: "only ovn-underlay"})
		}
		if d.EIPPool != "" {
			out = append(out, FieldError{Field: field + ".eipPool", Code: "invalid", Message: "direct networks have no elastic IP pool"})
		}
		out = append(out, checkAttachment(field, &d.Attachment, true)...)
	}
	return v, out
}

func nicOptions(nodes []cluster.Node) any {
	type nic struct {
		Name  string   `json:"name"`
		MTU   int      `json:"mtu"`
		Nodes []string `json:"nodes"`
	}
	var nics []*nic
	for _, n := range nodes {
		for _, c := range n.NICs {
			i := slices.IndexFunc(nics, func(x *nic) bool { return x.Name == c.Name && x.MTU == c.MTU })
			if i < 0 {
				nics = append(nics, &nic{Name: c.Name, MTU: c.MTU})
				i = len(nics) - 1
			}
			nics[i].Nodes = append(nics[i].Nodes, n.Name)
		}
	}
	return map[string]any{"nics": nics}
}

// Storage is spec.storage.
type Storage struct {
	Driver       string          `json:"driver"`
	StorageClass string          `json:"storageClass,omitempty"`
	VolumeMode   string          `json:"volumeMode,omitempty"`
	NFS          *NFS            `json:"nfs,omitempty"`
	S3           json.RawMessage `json:"s3,omitempty"`
	// VolumeTypes is spec.storage.volumeTypes: the backend of each EBS volume type (#391). A type
	// that is not a key is unmapped.
	VolumeTypes map[string]VolumeType `json:"volumeTypes,omitempty"`
}

// VolumeType is spec.storage.volumeTypes.<type>. An empty volumeMode or volumeSnapshotClass takes
// the storage default.
type VolumeType struct {
	StorageClass        string `json:"storageClass"`
	VolumeMode          string `json:"volumeMode,omitempty"`
	VolumeSnapshotClass string `json:"volumeSnapshotClass,omitempty"`
}

// EBSVolumeTypes are the keys spec.storage.volumeTypes accepts, in the order the wizard shows them.
var EBSVolumeTypes = []string{"standard", "gp2", "gp3", "io1", "io2", "st1", "sc1"}

// dns1123 is the CRD pattern for a StorageClass or VolumeSnapshotClass name.
var dns1123 = regexp.MustCompile(`^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$`)

// NFS is spec.storage.nfs.
type NFS struct {
	Server string `json:"server"`
	Share  string `json:"share"`
}

// StorageOption is one driver in the wizard. Disabled drivers show "Coming soon".
type StorageOption struct {
	Driver   string `json:"driver"`
	Disabled bool   `json:"disabled,omitempty"`
	Reason   string `json:"reason,omitempty"`
}

var storageDrivers = []StorageOption{
	{Driver: "rook-ceph"},
	{Driver: "existing"},
	{Driver: "nfs"},
	{Driver: "minio", Disabled: true, Reason: "Coming soon (popododo0720/vws-storage#70)"},
	{Driver: "local", Disabled: true, Reason: "Coming soon (popododo0720/vws-storage#71)"},
}

func storageOptions([]cluster.Node) any {
	return map[string]any{
		"drivers":     storageDrivers,
		"volumeTypes": EBSVolumeTypes,
		"volumeModes": []string{"Block", "Filesystem"},
	}
}

func decodeStorage(raw json.RawMessage, _ Input) (any, []FieldError) {
	var v Storage
	if errs := strict(raw, &v); errs != nil {
		return nil, errs
	}
	i := slices.IndexFunc(storageDrivers, func(o StorageOption) bool { return o.Driver == v.Driver })
	switch {
	case v.Driver == "":
		return v, []FieldError{required("spec.storage.driver")}
	case i < 0:
		return v, []FieldError{{Field: "spec.storage.driver", Code: "unsupported", Message: "unknown storage driver " + v.Driver}}
	case storageDrivers[i].Disabled:
		return v, []FieldError{{Field: "spec.storage.driver", Code: "not_available", Message: storageDrivers[i].Reason}}
	}
	var out []FieldError
	if v.Driver == "existing" && v.StorageClass == "" {
		out = append(out, required("spec.storage.storageClass"))
	}
	if v.Driver == "nfs" {
		if v.NFS == nil || v.NFS.Server == "" {
			out = append(out, required("spec.storage.nfs.server"))
		}
		if v.NFS == nil || v.NFS.Share == "" {
			out = append(out, required("spec.storage.nfs.share"))
		}
	}
	if v.VolumeMode != "" && v.VolumeMode != "Block" && v.VolumeMode != "Filesystem" {
		out = append(out, FieldError{Field: "spec.storage.volumeMode", Code: "unsupported", Message: "Block or Filesystem"})
	}
	return v, append(out, checkVolumeTypes(v)...)
}

// checkVolumeTypes applies the CRD rules of spec.storage.volumeTypes, so the wizard reports a bad
// mapping on its step instead of at install. Whether the classes exist is checked by vws apply.
func checkVolumeTypes(v Storage) []FieldError {
	if len(v.VolumeTypes) == 0 {
		return nil
	}
	if v.Driver == "nfs" {
		return []FieldError{{
			Field: "spec.storage.volumeTypes", Code: "unsupported",
			Message: "not for driver nfs; nfs serves every type from vws-nfs",
		}}
	}
	keys := make([]string, 0, len(v.VolumeTypes))
	for k := range v.VolumeTypes {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var out []FieldError
	for _, k := range keys {
		vt, field := v.VolumeTypes[k], "spec.storage.volumeTypes."+k
		if !slices.Contains(EBSVolumeTypes, k) {
			out = append(out, FieldError{Field: field, Code: "unsupported", Message: "unknown EBS volume type " + k})
			continue
		}
		switch {
		case vt.StorageClass == "":
			out = append(out, required(field+".storageClass"))
		case !dns1123.MatchString(vt.StorageClass):
			out = append(out, FieldError{Field: field + ".storageClass", Code: "invalid", Message: "not a DNS-1123 name"})
		}
		if vt.VolumeMode != "" && vt.VolumeMode != "Block" && vt.VolumeMode != "Filesystem" {
			out = append(out, FieldError{Field: field + ".volumeMode", Code: "unsupported", Message: "Block or Filesystem"})
		}
		if vt.VolumeSnapshotClass != "" && !dns1123.MatchString(vt.VolumeSnapshotClass) {
			out = append(out, FieldError{Field: field + ".volumeSnapshotClass", Code: "invalid", Message: "not a DNS-1123 name"})
		}
	}
	return out
}

// decodeEIP needs the saved network step: the pool lives on spec.network.nodes.external and must be
// inside its CIDR. It has no default (#139: no default, required).
func decodeEIP(raw json.RawMessage, in Input) (any, []FieldError) {
	network := in.Network
	var pool string
	if errs := strict(raw, &pool); errs != nil {
		return nil, errs
	}
	field := "spec.network.nodes.external.eipPool"
	if pool == "" {
		return pool, []FieldError{required(field)}
	}
	p, err := netip.ParsePrefix(pool)
	if err != nil {
		return pool, []FieldError{{Field: field, Code: "invalid", Message: "not a CIDR"}}
	}
	if network.Nodes == nil || network.Nodes.External == nil {
		return pool, []FieldError{{Field: "spec.network.nodes.external", Code: "required", Message: "the elastic IP pool lives on the external network; set it in the network step"}}
	}
	ext, err := netip.ParsePrefix(network.Nodes.External.CIDR)
	if err == nil && (p.Bits() < ext.Bits() || !ext.Contains(p.Addr())) {
		return pool, []FieldError{{Field: field, Code: "invalid", Message: "the pool is outside the external network " + ext.String()}}
	}
	return pool, nil
}

func applyEIP(spec map[string]any, value any) {
	network, ok := spec["network"].(Network)
	if !ok || network.Nodes == nil || network.Nodes.External == nil {
		return
	}
	ext := *network.Nodes.External
	ext.EIPPool = value.(string)
	nodes := *network.Nodes
	nodes.External = &ext
	network.Nodes = &nodes
	spec["network"] = network
}

func decodeProfile(raw json.RawMessage, _ Input) (any, []FieldError) {
	var v string
	if errs := strict(raw, &v); errs != nil {
		return nil, errs
	}
	if !slices.Contains([]string{"single-node", "small", "ha"}, v) {
		return v, []FieldError{{Field: "spec.profile", Code: "unsupported", Message: "single-node, small or ha"}}
	}
	return v, nil
}

func defaultProfile(nodes []cluster.Node) any {
	if len(nodes) <= 1 {
		return "single-node"
	}
	return "small"
}

// Observability is spec.observability. vws render rejects an OTLP endpoint until the chart takes one.
type Observability struct {
	OTLPEndpoint string `json:"otlpEndpoint"`
}

func decodeObservability(raw json.RawMessage, _ Input) (any, []FieldError) {
	var v Observability
	if errs := strict(raw, &v); errs != nil {
		return nil, errs
	}
	if v.OTLPEndpoint != "" {
		return v, []FieldError{{Field: "spec.observability.otlpEndpoint", Code: "not_available", Message: "the chart cannot take an OTLP endpoint yet; leave it empty"}}
	}
	return v, nil
}
