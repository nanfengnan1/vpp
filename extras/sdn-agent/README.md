# SDN Agent EVPN / symmetric IRB adapter

This directory contains a local adapter for SDN Agent's VPP + FRR mode.
It uses this checkout's `vpp_papi` and the `l2_egress_flood` extension in
`src/vnet/l2` and the native `vxlan_mp_remote_*` APIs in `src/plugins/vxlan`.
Unmodified VPP 26.06 is intentionally rejected.

FRR owns BGP selection. The adapter consumes Zebra-installed FDB entries on
explicitly managed Linux VXLAN mirrors, creates VPP VXLAN tunnels and remote
MAC forwarding, and publishes VPP access-port MACs to the Linux bridge.
Type-3 BUM membership and Type-2 unicast references have independent lifetimes.
Managed objects carry ownership tags; other VPP objects are not adopted.

Configuration RPCs and periodic reconciliation use one execution thread and
a root-only Unix HTTP socket. State updates use an fsync'd atomic journal.
Interrupted changes pause reconciliation until Agent's saved Undo is applied.
`irb.py` adds owned tenant FIBs, BVIs, gateway addresses, neighbor publication,
selected Type-2/Type-5 routes and native BD ARP/ND termination. Native VXLAN
interfaces are now native source-only multipoint interfaces, one per VNI.
MAC-to-VTEP and BUM membership updates allocate no per-remote interfaces.
An empty remote list retains the interface. L3VNI
uses Ethernet VXLAN and Router MACs, not the native raw-IP `is_l3` flag.
Linux mirror ports retain carrier but drop packets in both tc directions
(reserved priority 49150). They cannot provide a kernel forwarding shortcut.
Version-2 desired state migrates version-1 L2 state and saved Undo.
This is not an HA, scale, or performance claim. Tenant BGP/OSPF punt to Linux
is not implemented; the default underlay continues to use LCP.
The underlay and LCP pairs must be recreated by startup configuration.
Multipoint supports IPv4/IPv6 VTEPs and Ethernet payload (IPv4/IPv6).
Remote VTEPs must match the local address family. The v2 remote API carries
generic IP addresses; the IPv4-only v1 API remains available for old callers.
The encapsulation node performs an underlay FIB lookup after choosing a remote
by inner destination MAC, including the Router MAC selected by symmetric IRB.
TCP GSO is segmented in software, including IPv6 transport offset/checksum
fixups for extension headers. Outer IPv6 UDP checksums are mandatory. Input
validates the outer checksum independently of potentially inner NIC flags.
This is software fallback, not hardware tunnel TSO/LRO certification. Keep RX
LRO disabled; the legacy exact IPv4 RX flow rule is rejected for multipoint.
Native query rows expose interface identity, VTEP family and remote/BUM maps.

In this directory:

    python3 -m unittest discover -p 'test_*.py'

The companion `sdn-agent` repository contains `docker/build-vpp.sh`,
`docker/Dockerfile.vpp`, `lab/vpp_evpn.py`, `lab/vpp_irb.py` and
`docs/{user,developer}/vpp.rst`.
Its VPP base image packages this adapter with the matching PAPI/native/API JSON;
the Agent bundle copies all of them from that same image. It no longer reads a
second VPP checkout while assembling the bundle. Rebuild older base images once.
Keep both repositories' changes together when rebuilding or distributing.

User guide: [SDN Agent with VPP/FRR](../../docs/usecases/sdn_agent.rst).
Developer guide: [architecture, LCP nodes, and packet paths](../../docs/developer/extras/sdn_agent.rst).
The companion build accepts `VPP_REF` / `FRR_REF` (tag, branch or commit) from
`nanfengnan1/vpp` / `nanfengnan1/frr`, or local `VPP_SOURCE` / `FRR_SOURCE`.
Use `BUILD_BASES=1` initially. The selected VPP ref must include these changes;
the full GitHub-source image build remains pending until they are pushed.
Selectable refs do not imply compatibility with arbitrary upstream versions.
