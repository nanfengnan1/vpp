#!/usr/bin/env python3
"""SDN Agent's local VPP 26.06 L2 EVPN adapter (Apache-2.0).

Only explicitly managed Linux mirror networks are synchronized. FRR remains
the authority for remote FDB/VTEP selection. Config RPCs and reconciliation
share one execution thread; the socket is root-only. No shell commands.
"""

import argparse
import copy
import http.server
import ipaddress
import json
import logging
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import time
import zlib

ALL = 0xFFFFFFFF
PREFIX = "sdn-agent/"
ZERO = "00:00:00:00:00:00"
LOG = logging.getLogger("sdn-vpp")


def command(*args):
    p = subprocess.run(args, text=True, capture_output=True, timeout=8)
    if p.returncode:
        raise RuntimeError(" ".join(args) + ": " + p.stderr.strip())
    return p.stdout


def plain(value):
    if hasattr(value, "_asdict"):
        return {
            k: plain(v)
            for k, v in value._asdict().items()
            if k not in ("_0", "context")
        }
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def empty():
    return {
        k: {}
        for k in (
            "bridges",
            "vxlans",
            "ports",
            "tenants",
            "bindings",
            "l3vnis",
            "addresses",
            "neighborKeepalive",
        )
    }


def normalized(model):
    if not isinstance(model, dict) or set(model) - set(empty()):
        raise ValueError("invalid saved VPP model")
    return {**empty(), **copy.deepcopy(model)}


def bd_id(name):
    # VPP L2_BD_ID_MAX is 2^24-1; zero is reserved.
    return zlib.crc32(name.encode()) % 0xFFFFFF + 1


def route_network(route):
    prefix = route["prefix"]
    if prefix == "default":
        prefix = (
            "::/0"
            if ipaddress.ip_address(route["gateway"]).version == 6
            else "0.0.0.0/0"
        )
    return ipaddress.ip_network(prefix, strict=False)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        os.chmod(tmp, 0o600)
        json.dump(data, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def updated(model, op):
    """Materialize only managed L2 configuration, not learned remote state."""
    result = normalized(model)
    code, action = op["code"], op["action"]
    if code == "NEIGHBOR_KEEPALIVE":
        obj = op.get("neighborKeepalive", {})
        if action != "SET" or type(obj.get("enabled")) is not bool:
            raise ValueError("neighbor keepalive requires SET with explicit enabled")
        result["neighborKeepalive"] = {"enabled": obj["enabled"]}
    if code in (
        "BD",
        "VXLAN",
        "BD_PORT",
        "TENANT",
        "BD_TENANT",
        "TENANT_L3VNI",
        "ADDRESS",
    ):
        field, collection, key = {
            "BD": ("bridge", "bridges", "name"),
            "VXLAN": ("vxlan", "vxlans", "name"),
            "BD_PORT": ("bridgePort", "ports", "port"),
            "TENANT": ("tenant", "tenants", "name"),
            "BD_TENANT": ("binding", "bindings", "bridge"),
            "TENANT_L3VNI": ("l3vni", "l3vnis", "tenant"),
            "ADDRESS": ("address", "addresses", "interface"),
        }[code]
        obj = op[field]
        if code == "VXLAN" and obj.get("local"):
            obj = {**obj, "local": str(ipaddress.ip_address(obj["local"]))}
        objkey = (
            obj[key]
            if code != "ADDRESS"
            else obj["interface"] + "|" + str(ipaddress.ip_interface(obj["prefix"]))
        )
        if action in ("DELETE", "UNBIND"):
            result[collection].pop(objkey, None)
        else:
            result[collection][objkey] = {
                **result[collection].get(objkey, {}),
                **obj,
            }
    if code == "INTERFACE":
        obj = op["interface"]
        for collection in ("bridges", "vxlans", "tenants"):
            if obj["name"] in result[collection]:
                result[collection][obj["name"]].update(obj)
    return result


class VPP:
    def __init__(self, path, apidir):
        from vpp_papi import VPPApiClient

        self.client = VPPApiClient(server_address=path, apidir=apidir, read_timeout=5)
        self.client.connect("sdn-agent-evpn")
        # Refuse the unpatched API rather than silently flood withdrawn VTEPs.
        if not hasattr(self.client.api, "vxlan_mp_remote_add_del_v2"):
            raise RuntimeError(
                "VPP lacks native multipoint VXLAN; build the matching SDN VPP source first"
            )

    def call(self, method, **kwargs):
        value = getattr(self.client.api, method)(**kwargs)
        reply = (
            value[0]
            if isinstance(value, tuple) and not hasattr(value, "_fields")
            else value
        )
        rv = getattr(reply, "retval", 0)
        if rv:
            raise RuntimeError(f"{method}: VPP retval {rv}")
        return value

    def snapshot(self):
        interfaces = {r.sw_if_index: plain(r) for r in self.call("sw_interface_dump")}
        bridges = {r.bd_id: plain(r) for r in self.call("bridge_domain_dump")}
        tunnels = {
            r.sw_if_index: plain(r)
            for r in self.call("vxlan_tunnel_v2_dump", sw_if_index=ALL)
        }
        pairs = {}
        cursor = 0
        while cursor != ALL:
            reply, rows = self.call("lcp_itf_pair_get", cursor=cursor)
            pairs.update(
                {r.host_if_name: r.phy_sw_if_index for r in rows if not r.netns}
            )
            if not rows:
                break
            if reply.cursor == cursor:
                raise RuntimeError("LCP API returned a non-advancing cursor")
            cursor = reply.cursor
        return interfaces, bridges, tunnels, pairs


class Engine:
    def __init__(self, api, state_file, run=command):
        self.api, self.state_file, self.run = api, Path(state_file), run
        saved = (
            json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        )
        if saved and saved.get("version") not in (1, 2, 3):
            raise RuntimeError("unsupported VPP adapter state version")
        self.model = normalized(saved.get("desired", empty()))
        self.previous = saved.get("previous")
        self.paused = self.previous is not None
        # (bridge, MAC) -> kernel mirror port; remote entries are excluded.
        self.local = {}
        self.local_initialized = False
        self.local_changed = {}
        self.last_error = ""
        self.last_success = 0
        self.cycles = 0
        self.running = True

    def save(self):
        atomic_json(
            self.state_file,
            {"version": 3, "desired": self.model, "previous": self.previous},
        )

    def restore_mirrors(self):
        """Rebuild owned L2 mirrors on a new namespace; never flush other links.

        Underlay/LCP interfaces belong to the startup configuration. Their
        restoration must finish before Agent/FRR are started.
        """
        if self.paused:
            return  # the Agent's saved transaction Undo must decide recovery
        links = {
            r["ifname"]: r
            for r in json.loads(self.run("ip", "-j", "-d", "link", "show"))
        }
        for collection, kind in (("bridges", "bridge"), ("vxlans", "vxlan")):
            for name, obj in self.model[collection].items():
                row = links.get(name)
                if row:
                    info = row.get("linkinfo", {})
                    if info.get("info_kind") != kind:
                        raise RuntimeError(
                            f"mirror name {name} belongs to another link type"
                        )
                    if kind == "vxlan" and (
                        info.get("info_data", {}).get("id") != obj["vni"]
                        or info.get("info_data", {}).get(
                            "local", info.get("info_data", {}).get("local6")
                        )
                        != obj["local"]
                    ):
                        raise RuntimeError(
                            f"VXLAN mirror {name} conflicts with persisted configuration"
                        )
                else:
                    args = ["ip", "link", "add", name, "type", kind]
                    if kind == "vxlan":
                        args += [
                            "id",
                            str(obj["vni"]),
                            "local",
                            obj["local"],
                            "dstport",
                            str(obj.get("port", 4789)),
                            "nolearning",
                        ]
                        if obj.get("underlay"):
                            args += ["dev", obj["underlay"]]
                    self.run(*args)
                if kind == "bridge":
                    if obj.get("mac"):
                        self.run("ip", "link", "set", name, "address", obj["mac"])
                    if obj.get("mtu"):
                        self.run("ip", "link", "set", name, "mtu", str(obj["mtu"]))
                    self.run(
                        "ip",
                        "link",
                        "set",
                        name,
                        "type",
                        "bridge",
                        "stp_state",
                        "0",
                        "ageing_time",
                        str(obj.get("ageingTime", 300)),
                    )
                if obj.get("up"):
                    self.run("ip", "link", "set", name, "up")
        _, _, _, pairs = self.api.snapshot()
        for name, p in self.model["ports"].items():
            if name not in self.model["vxlans"] and name not in pairs:
                raise RuntimeError(
                    f"restore LCP access pair {name} in the VPP startup configuration first"
                )
            self.run("ip", "link", "set", name, "master", p["bridge"])
            if "learning" in p:
                self.run(
                    "bridge",
                    "link",
                    "set",
                    "dev",
                    name,
                    "learning",
                    "on" if p["learning"] else "off",
                )

    def validate(self, op):
        code = op["code"]
        interfaces, bridges, tunnels, pairs = self.api.snapshot()
        if code in ("TENANT", "BD_TENANT", "TENANT_L3VNI", "EVPN_TYPE5"):
            raise ValueError("VPP IRB/tenant routing is not implemented")
        if code == "BD":
            b = op["bridge"]
            if b.get("stp"):
                raise ValueError("STP is not supported")
            age = b.get("ageingTime", 300)
            if age % 60 or age > 15300:
                raise ValueError("MAC ageing must be a multiple of 60 seconds")
            existing = bridges.get(bd_id(b["name"]))
            if existing and existing.get("bd_tag") != PREFIX + "bd/" + b["name"]:
                raise ValueError("BD ID collision with an unmanaged VPP bridge domain")
            if op["action"] == "DELETE" and any(
                p["bridge"] == b["name"] for p in self.model["ports"].values()
            ):
                raise ValueError("unbind managed ports before deleting a BD")
        if code == "VXLAN":
            x = op["vxlan"]
            if op["action"] != "DELETE":
                ip = ipaddress.ip_address(x["local"])
                if ip.is_multicast or ip.is_unspecified or ip.is_link_local:
                    raise ValueError(
                        "VTEP requires an unicast IPv4/IPv6 address without a scope zone"
                    )
                if x.get("learning"):
                    raise ValueError("remote VXLAN learning must be disabled with EVPN")
                if x.get("underlay") and x["underlay"] not in pairs:
                    raise ValueError("VXLAN underlay must be an LCP-paired interface")
            if op["action"] == "DELETE" and x["name"] in self.model["ports"]:
                raise ValueError("unbind VXLAN before deleting it")
        if code == "BD_PORT":
            p = op["bridgePort"]
            if p.get("neighSuppress"):
                raise ValueError("ARP/ND suppression is not implemented")
            if op["action"] not in ("DELETE", "UNBIND"):
                if p["bridge"] not in self.model["bridges"]:
                    raise ValueError("create the managed BD first")
                if p["port"] not in self.model["vxlans"] and p["port"] not in pairs:
                    raise ValueError("access port must be an LCP-paired VPP interface")
                # One VNI per BD; IDs may differ, association is explicit.
                members = [
                    n
                    for n, v in self.model["ports"].items()
                    if v["bridge"] == p["bridge"]
                    and n in self.model["vxlans"]
                    and n != p["port"]
                ]
                if p["port"] in self.model["vxlans"] and members:
                    raise ValueError("one L2VNI per bridge domain is supported")
        if code in ("ADDRESS", "INTERFACE"):
            obj = op["address"] if code == "ADDRESS" else op["interface"]
            name = obj.get("interface", obj.get("name"))
            if name in self.model["bridges"] or name in self.model["vxlans"]:
                if code == "ADDRESS":
                    raise ValueError("SVI/BVI addresses require IRB support")
                if obj.get("up", True) is False or "mtu" in obj or "mac" in obj:
                    raise ValueError(
                        "only enabling the Linux mirror is supported on L2 virtual objects"
                    )
            elif name not in pairs:
                raise ValueError(
                    "configure only an existing LCP-paired interface in VPP mode"
                )
        if code == "STATIC_ROUTE":
            r = op["route"]
            if r.get("table", 0) not in (0, 254):
                raise ValueError("tenant FIB routes are not supported")
            if not r.get("interface") or r["interface"] not in pairs:
                raise ValueError(
                    "route requires an explicit LCP-paired output interface"
                )
        return interfaces, bridges, tunnels, pairs

    def set_model(self, target):
        self.previous = copy.deepcopy(self.model)
        self.model = copy.deepcopy(target)
        self.paused = True
        self.save()  # journal intent before the first VPP mutation
        self.reconcile()
        self.previous = None
        self.paused = False
        self.save()

    def member(self, index, bd, shg=0, enable=True):
        self.api.call(
            "sw_interface_set_l2_bridge",
            rx_sw_if_index=index,
            bd_id=bd,
            shg=shg,
            port_type=0,
            enable=enable,
        )

    def remote(self):
        """Consume installed kernel state, not an unfiltered BGP RIB."""
        rows = json.loads(self.run("bridge", "-j", "fdb", "show"))
        # VXLAN self FDB entries may outlive a move back to a local port.
        # Require the bridge master FDB to select the VXLAN too.
        selected = {
            (r.get("ifname", r.get("dev")), r.get("mac", "").lower(), r.get("master"))
            for r in rows
            if r.get("master") and "extern_learn" in r.get("flags", [])
        }
        result = {}
        for row in rows:
            name, dst, mac = (
                row.get("ifname", row.get("dev")),
                row.get("dst"),
                row.get("mac", "").lower(),
            )
            if (
                name not in self.model["vxlans"]
                or name not in self.model["ports"]
                or not dst
            ):
                continue
            x = self.model["vxlans"][name]
            ip = ipaddress.ip_address(dst)
            if ip.is_multicast or ip.is_unspecified or ip.is_link_local:
                continue
            if ip.version != ipaddress.ip_address(
                x["local"]
            ).version or ip == ipaddress.ip_address(x["local"]):
                continue
            dst = str(ip)
            if (
                mac != ZERO
                and (name, mac, self.model["ports"][name]["bridge"]) not in selected
            ):
                continue
            item = result.setdefault((name, dst), {"bum": False, "macs": set()})
            if mac == ZERO:
                item["bum"] = True
            elif len(mac.split(":")) == 6 and not (int(mac[:2], 16) & 1):
                item["macs"].add(mac)
        return result

    def reconcile(self):
        interfaces, bridges, tunnels, pairs = self.api.snapshot()
        desired = self.model
        # Detach only ports currently inside owned BDs. No global flush.
        wanted_ports = {
            pairs[n]: bd_id(p["bridge"])
            for n, p in desired["ports"].items()
            if n in pairs
        }
        owned_bds = {
            i: b
            for i, b in bridges.items()
            if b.get("bd_tag", "").startswith(PREFIX + "bd/")
        }
        owned_tunnels = {
            i: t
            for i, t in tunnels.items()
            if interfaces.get(i, {}).get("tag", "").startswith(PREFIX + "vx/")
        }
        for i, b in owned_bds.items():
            for member in b["sw_if_details"]:
                index = member["sw_if_index"]
                if interfaces.get(index, {}).get("tag", "").startswith(PREFIX + "bvi/"):
                    continue  # lifecycle is owned by the IRB extension
                if index not in owned_tunnels and wanted_ports.get(index) != i:
                    # An unmanaged member is a conflict, not permission to steal it.
                    old_ports = (self.previous or desired)["ports"]
                    if index not in {pairs[n] for n in old_ports if n in pairs}:
                        raise RuntimeError(f"unmanaged port {index} in managed BD {i}")
                    self.member(index, i, enable=False)
        for name, b in desired["bridges"].items():
            i = bd_id(name)
            if i in bridges and bridges[i].get("bd_tag") != PREFIX + "bd/" + name:
                raise RuntimeError("managed BD collides with another owner")
            if i not in bridges:
                self.api.call(
                    "bridge_domain_add_del_v2",
                    bd_id=i,
                    is_add=True,
                    flood=True,
                    uu_flood=True,
                    forward=True,
                    learn=True,
                    arp_term=False,
                    arp_ufwd=False,
                    mac_age=b.get("ageingTime", 300) // 60,
                    bd_tag=PREFIX + "bd/" + name,
                )
            else:
                self.api.call("bridge_flags", bd_id=i, is_set=True, flags=0xF)
                self.api.call("bridge_flags", bd_id=i, is_set=False, flags=0x30)
                if bridges[i]["mac_age"] != b.get("ageingTime", 300) // 60:
                    self.api.call(
                        "bridge_domain_set_mac_age",
                        bd_id=i,
                        mac_age=b.get("ageingTime", 300) // 60,
                    )
        for name, p in desired["ports"].items():
            if name in desired["vxlans"]:
                continue
            if name not in pairs:
                raise RuntimeError(
                    f"missing LCP pair {name}; restore startup configuration"
                )
            index = pairs[name]
            current = next(
                (
                    i
                    for i, b in bridges.items()
                    if any(m["sw_if_index"] == index for m in b["sw_if_details"])
                ),
                None,
            )
            target = bd_id(p["bridge"])
            if current is not None and current not in owned_bds:
                raise RuntimeError(
                    f"access port {name} already belongs to an unmanaged BD"
                )
            if current != target:
                self.member(index, target)
            self.api.call(
                "l2_interface_feat_flags_set",
                sw_if_index=index,
                flags=1,
                is_set=p.get("learning", True),
            )
        # Snapshot VPP learned local MACs and publish only access-port entries.
        self.sync_local(pairs)
        remote = self.remote()
        wanted_tunnels = {}
        for name, x in desired["vxlans"].items():
            target = (
                bd_id(desired["ports"][name]["bridge"])
                if name in desired["ports"]
                else None
            )
            dst = "::" if ipaddress.ip_address(x["local"]).version == 6 else "0.0.0.0"
            tag = PREFIX + "vx/" + name + "/mp"
            matches = [i for i in owned_tunnels if interfaces[i].get("tag") == tag]
            if len(matches) > 1:
                raise RuntimeError("duplicate owned VXLAN tunnel")
            if matches:
                index = matches[0]
                t = tunnels[index]
                if (
                    str(t["src_address"]) != x["local"]
                    or t["vni"] != x["vni"]
                    or t["dst_port"] != x.get("port", 4789)
                    or t["src_port"] != x.get("port", 4789)
                ):
                    self.member(index, 0, enable=False)
                    self.api.call(
                        "vxlan_add_del_tunnel_v3",
                        is_add=False,
                        src_address=t["src_address"],
                        dst_address=t["dst_address"],
                        vni=t["vni"],
                        src_port=t["src_port"],
                        dst_port=t["dst_port"],
                        encap_vrf_id=t["encap_vrf_id"],
                        instance=ALL,
                        mcast_sw_if_index=ALL,
                        decap_next_index=ALL,
                        is_l3=False,
                    )
                    owned_tunnels.pop(index)
                    tunnels.pop(index)
                    matches = []
            if not matches:
                # Reject unmanaged same-key tunnels instead of adopting them.
                for i, t in tunnels.items():
                    if (
                        i not in owned_tunnels
                        and str(t["src_address"]) == x["local"]
                        and ipaddress.ip_address(str(t["dst_address"])).is_unspecified
                        and t["vni"] == x["vni"]
                    ):
                        raise RuntimeError("VXLAN key belongs to an unmanaged tunnel")
                r = self.api.call(
                    "vxlan_add_del_tunnel_v3",
                    is_add=True,
                    instance=ALL,
                    src_address=x["local"],
                    dst_address=dst,
                    src_port=x.get("port", 4789),
                    dst_port=x.get("port", 4789),
                    encap_vrf_id=0,
                    mcast_sw_if_index=ALL,
                    decap_next_index=ALL,
                    vni=x["vni"],
                    is_l3=False,
                )
                index = r.sw_if_index
                self.api.call(
                    "sw_interface_tag_add_del", is_add=True, sw_if_index=index, tag=tag
                )
                self.api.call("sw_interface_set_flags", sw_if_index=index, flags=1)
                if target is not None:
                    self.member(index, target, shg=1)
            else:
                current_bd = next(
                    (
                        i
                        for i, b in bridges.items()
                        if any(m["sw_if_index"] == index for m in b["sw_if_details"])
                    ),
                    None,
                )
                if current_bd != target:
                    self.member(index, target or 0, shg=1, enable=target is not None)
            if target is not None:
                self.api.call(
                    "l2_interface_feat_flags_set",
                    sw_if_index=index,
                    flags=1,
                    is_set=False,
                )
            # Type-2 MAC mappings and Type-3 BUM destinations have separate
            # lifetimes inside ONE native interface, including before peers exist.
            entries = self.sync_remotes(index, name, remote)
            wanted_tunnels[index] = (target, entries, tag)
        # Install selected remote MACs without blocking later local MAC movement.
        for name in desired["bridges"]:
            i = bd_id(name)
            fdb = {str(r.mac): r for r in self.api.call("l2_fib_table_dump", bd_id=i)}
            wanted = {
                mac: index
                for index, (bd, macs, _) in wanted_tunnels.items()
                if bd == i
                for mac in macs
            }
            for mac, index in wanted.items():
                existing = fdb.get(mac)
                if existing and existing.sw_if_index == index:
                    continue
                if time.monotonic() - self.local_changed.get((name, mac), 0) < 2:
                    continue  # give Zebra time to process a local move
                self.api.call(
                    "l2fib_add_del",
                    bd_id=i,
                    mac=mac,
                    sw_if_index=index,
                    is_add=True,
                    static_mac=False,
                    filter_mac=False,
                    bvi_mac=False,
                )
            for mac, row in fdb.items():
                if row.sw_if_index in owned_tunnels and mac not in wanted:
                    self.api.call(
                        "l2fib_add_del",
                        bd_id=i,
                        mac=mac,
                        sw_if_index=row.sw_if_index,
                        is_add=False,
                        static_mac=False,
                        filter_mac=False,
                        bvi_mac=False,
                    )
        # Remove unused owned tunnels AFTER their forwarding entries.
        for index, t in owned_tunnels.items():
            if index in wanted_tunnels:
                continue
            self.member(index, 0, enable=False)
            self.api.call(
                "vxlan_add_del_tunnel_v3",
                is_add=False,
                src_address=t["src_address"],
                dst_address=t["dst_address"],
                vni=t["vni"],
                src_port=t["src_port"],
                dst_port=t["dst_port"],
                encap_vrf_id=t["encap_vrf_id"],
                instance=ALL,
                mcast_sw_if_index=ALL,
                decap_next_index=ALL,
                is_l3=False,
            )
        for i in owned_bds:
            if i not in {bd_id(n) for n in desired["bridges"]}:
                self.api.call("bridge_domain_add_del_v2", bd_id=i, is_add=False)
        self.last_error = ""
        self.last_success = time.time()
        self.cycles += 1

    def sync_remotes(self, index, name, remote):
        wanted = set()
        selected = {}
        for (vxname, dst), entries in remote.items():
            if vxname != name:
                continue
            if entries["bum"]:
                wanted.add((ZERO, dst, True))
            for mac in entries["macs"]:
                if mac in selected and selected[mac] != dst:
                    raise RuntimeError(f"ambiguous selected remote for {name}/{mac}")
                selected[mac] = dst
                wanted.add((mac, dst, False))
        actual = {
            (str(r.mac), str(r.remote), bool(r.bum))
            for r in self.api.call("vxlan_mp_remote_v2_dump", sw_if_index=index)
        }
        # Install the new MAC destination before withdrawing the old one.
        # Native deletion compares remote, so a stale withdrawal cannot undo a move.
        for add, entries in ((True, wanted - actual), (False, actual - wanted)):
            for mac, dst, bum in sorted(entries):
                self.api.call(
                    "vxlan_mp_remote_add_del_v2",
                    sw_if_index=index,
                    mac=mac,
                    remote=dst,
                    bum=bum,
                    is_add=add,
                )
        return set(selected)

    def sync_local(self, pairs):
        access = {
            pairs[n]: (p["bridge"], n)
            for n, p in self.model["ports"].items()
            if n in pairs and n not in self.model["vxlans"]
        }
        current = {}
        for name in self.model["bridges"]:
            for row in self.api.call("l2_fib_table_dump", bd_id=bd_id(name)):
                if row.sw_if_index not in access or row.bvi_mac or row.filter_mac:
                    continue
                bridge, port = access[row.sw_if_index]
                if bridge == name:
                    current[(name, str(row.mac))] = port
        # Inspect the kernel too: replay after Zebra/kernel restart without
        # constantly re-announcing the same MAC (which would break mobility).
        kernel = json.loads(self.run("bridge", "-j", "fdb", "show"))
        # Recover the local publication ledger after an adapter-only restart.
        # Access mirror FDBs are owned by this adapter. Never touch permanent
        # entries, VXLAN remote entries, or unmanaged kernel ports.
        access_names = {port: bridge for bridge, port in access.values()}
        for row in kernel:
            port = row.get("ifname", row.get("dev"))
            bridge = access_names.get(port)
            if (
                bridge
                and row.get("master") == bridge
                and "extern_learn" in row.get("flags", [])
                and row.get("state") != "permanent"
            ):
                self.local.setdefault((bridge, row["mac"]), port)
        for key, port in current.items():
            present = any(
                r.get("ifname", r.get("dev")) == port
                and r.get("mac") == key[1]
                and r.get("master") == key[0]
                for r in kernel
            )
            selected_remote = any(
                r.get("ifname", r.get("dev")) in self.model["vxlans"]
                and r.get("mac") == key[1]
                and r.get("master") == key[0]
                and "extern_learn" in r.get("flags", [])
                for r in kernel
            )
            if selected_remote and (
                not self.local_initialized or self.local.get(key) == port
            ):
                # An unchanged old local VPP entry must yield to a newer EVPN
                # winner. Only newly observed local learning initiates a move.
                continue
            if self.local.get(key) != port or not present:
                self.run(
                    "bridge",
                    "fdb",
                    "replace",
                    key[1],
                    "dev",
                    port,
                    "master",
                    "dynamic",
                    "extern_learn",
                )
                if self.local.get(key) != port:
                    self.local_changed[key] = time.monotonic()
        for key, port in self.local.items():
            if key not in current and any(
                r.get("ifname", r.get("dev")) == port
                and r.get("mac") == key[1]
                and r.get("master") == key[0]
                for r in kernel
            ):
                self.run("bridge", "fdb", "del", key[1], "dev", port, "master")
        self.local = current
        self.local_initialized = True

    def verify(self, op):
        expected = updated(self.model, op)
        if expected != self.model:
            raise RuntimeError("adapter desired state does not contain the operation")
        interfaces, bridges, tunnels, pairs = self.api.snapshot()
        for name in self.model["bridges"]:
            b = bridges.get(bd_id(name))
            if not b or b.get("bd_tag") != PREFIX + "bd/" + name:
                raise RuntimeError(f"VPP bridge {name} missing")
        vx_indices = {}
        for name, obj in self.model["vxlans"].items():
            matches = [
                i
                for i in tunnels
                if interfaces[i].get("tag") == PREFIX + "vx/" + name + "/mp"
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"native multipoint interface {name} missing or duplicated"
                )
            index = vx_indices[name] = matches[0]
            row = tunnels[index]
            if (
                str(row["src_address"]) != obj["local"]
                or not ipaddress.ip_address(str(row["dst_address"])).is_unspecified
                or row["vni"] != obj["vni"]
                or row["src_port"] != obj.get("port", 4789)
                or row["dst_port"] != obj.get("port", 4789)
            ):
                raise RuntimeError(f"native multipoint interface {name} differs")
        if op["code"] == "VXLAN" and op["action"] == "DELETE":
            tag = PREFIX + "vx/" + op["vxlan"]["name"] + "/"
            if any(interfaces[i].get("tag", "").startswith(tag) for i in tunnels):
                raise RuntimeError("deleted native VXLAN interface remains")
        for name, p in self.model["ports"].items():
            if not any(
                m["sw_if_index"] == vx_indices.get(name, pairs.get(name))
                for m in bridges.get(bd_id(p["bridge"]), {}).get("sw_if_details", [])
            ):
                raise RuntimeError(f"VPP member {name} missing")
        if op["code"] == "INTERFACE":
            obj = op["interface"]
            if obj["name"] in pairs:
                row = interfaces[pairs[obj["name"]]]
                if "up" in obj and bool(row["flags"] & 1) != obj["up"]:
                    raise RuntimeError("LCP interface state has not synchronized")
                if "mtu" in obj and row["mtu"][0] != obj["mtu"]:
                    raise RuntimeError("LCP interface MTU has not synchronized")
                if "mac" in obj and row["l2_address"].lower() != obj["mac"].lower():
                    raise RuntimeError("LCP interface MAC has not synchronized")
        if op["code"] == "ADDRESS":
            obj = op["address"]
            index = pairs.get(obj["interface"])
            if index is None:
                raise RuntimeError("address interface lost its LCP pair")
            prefix = ipaddress.ip_interface(obj["prefix"])
            rows = self.api.call(
                "ip_address_dump", sw_if_index=index, is_ipv6=prefix.version == 6
            )
            present = any(str(r.prefix) == str(prefix) for r in rows)
            if present != (op["action"] != "DELETE"):
                raise RuntimeError("LCP address has not synchronized into VPP")
        if op["code"] == "STATIC_ROUTE":
            obj = op["route"]
            prefix = route_network(obj)
            rows = self.api.call(
                "ip_route_dump", table={"table_id": 0, "is_ip6": prefix.version == 6}
            )
            present = False
            for row in rows:
                route = plain(row)["route"]
                if route["prefix"] != str(prefix):
                    continue
                for path in route["paths"]:
                    address = path.get("nh", {}).get("address", {})
                    gateway = (
                        address.get("ip4" if prefix.version == 4 else "ip6")
                        if isinstance(address, dict)
                        else address
                    )
                    if path["sw_if_index"] == pairs.get(obj["interface"]) and (
                        not obj.get("gateway") or str(gateway) == obj["gateway"]
                    ):
                        present = True
            if present != (op["action"] != "DELETE"):
                raise RuntimeError("kernel route has not synchronized into VPP")
        return {"verified": True}

    def query(self, q):
        interfaces, bridges, tunnels, pairs = self.api.snapshot()
        names = {bd_id(n): n for n in self.model["bridges"]}
        reverse = {i: n for n, i in pairs.items()}
        for i, row in interfaces.items():
            tag = row.get("tag", "")
            if tag.startswith(PREFIX + "vx/"):
                reverse[i] = tag.split("/")[2]
            elif tag.startswith(PREFIX + "bvi/"):
                reverse[i] = tag.split("/")[2]
        kind = q.get("kind")
        rows = []
        if kind == "BRIDGES":
            rows = [
                {"name": names[i], "bdId": i, **b}
                for i, b in bridges.items()
                if i in names
            ]
        elif kind == "INTERFACES":
            rows = [
                {
                    "name": name,
                    "vppName": interfaces[i]["interface_name"],
                    "swIfIndex": i,
                    "up": bool(interfaces[i]["flags"] & 1),
                    "mtu": interfaces[i]["mtu"][0],
                    "mac": interfaces[i]["l2_address"],
                }
                for name, i in pairs.items()
                if i in interfaces
            ]
        elif kind == "PORTS":
            rows = [
                {
                    "bridge": names[i],
                    "interface": reverse.get(
                        m["sw_if_index"],
                        interfaces.get(m["sw_if_index"], {}).get("interface_name"),
                    ),
                    **m,
                }
                for i, b in bridges.items()
                if i in names
                for m in b["sw_if_details"]
            ]
        elif kind == "FDB":
            for i, name in names.items():
                if i in bridges:
                    rows += [
                        {
                            "bridge": name,
                            "mac": str(r.mac),
                            "swIfIndex": r.sw_if_index,
                            "interface": reverse.get(
                                r.sw_if_index,
                                interfaces.get(r.sw_if_index, {}).get("interface_name"),
                            ),
                            "origin": (
                                "bvi"
                                if r.bvi_mac
                                else ("remote" if r.sw_if_index in tunnels else "local")
                            ),
                            "static": bool(r.static_mac),
                        }
                        for r in self.api.call("l2_fib_table_dump", bd_id=i)
                    ]
        elif kind == "VXLAN_TUNNELS":
            for i, t in tunnels.items():
                tag = interfaces.get(i, {}).get("tag", "")
                if tag.startswith(PREFIX + "vx/"):
                    name = tag.split("/")[2]
                    remotes = [
                        plain(r)
                        for r in self.api.call("vxlan_mp_remote_v2_dump", sw_if_index=i)
                    ]
                    rows.append(
                        {
                            "name": name,
                            "interface": interfaces[i]["interface_name"],
                            "vppName": interfaces[i]["interface_name"],
                            "swIfIndex": i,
                            "bridge": self.model["ports"].get(name, {}).get("bridge"),
                            "local": str(t["src_address"]),
                            "port": t["dst_port"],
                            "family": (
                                "IPV6"
                                if ipaddress.ip_address(str(t["src_address"])).version
                                == 6
                                else "IPV4"
                            ),
                            "multipoint": ipaddress.ip_address(
                                str(t["dst_address"])
                            ).is_unspecified,
                            "remotes": remotes,
                            "remoteCount": len({r["remote"] for r in remotes}),
                            "bumCount": sum(bool(r["bum"]) for r in remotes),
                            "bum": any(r["bum"] for r in remotes),
                            **t,
                        }
                    )
        else:
            raise ValueError("unsupported VPP query")
        for row in rows:
            bridge = row.get("bridge", row.get("name"))
            row["vrf"] = self.model["bindings"].get(bridge, {}).get("tenant", "")
        if q.get("prefix"):
            raise ValueError("prefix filter is not supported for this VPP L2 query")
        if q.get("family") not in (None, "AF_UNSPECIFIED"):
            if kind != "VXLAN_TUNNELS":
                raise ValueError("address family filter requires an IP query")
            rows = [r for r in rows if r["family"] == q["family"]]
        if q.get("neighbor"):
            if kind != "VXLAN_TUNNELS":
                raise ValueError("neighbor filter requires a neighbor or VXLAN query")
            remote = str(ipaddress.ip_address(q["neighbor"]))
            rows = [r for r in rows if any(x["remote"] == remote for x in r["remotes"])]
        if q.get("vrf"):
            rows = [r for r in rows if r.get("vrf") == q["vrf"]]
        if q.get("bridge"):
            rows = [r for r in rows if r.get("bridge", r.get("name")) == q["bridge"]]
        if q.get("interface"):
            rows = [
                r
                for r in rows
                if q["interface"]
                in (r.get("interface"), r.get("name"), r.get("vppName"))
            ]
        return rows

    def rollback_expectation(self, op):
        """Read restored Linux state, then verify that LCP delivered it to VPP."""
        result = {"code": "ROLLBACK", "action": "SET"}
        _, _, _, pairs = self.api.snapshot()
        code = op["code"]
        if code == "ADDRESS" and op["address"]["interface"] in pairs:
            obj = op["address"]
            rows = json.loads(
                self.run("ip", "-j", "addr", "show", "dev", obj["interface"])
            )
            wanted = ipaddress.ip_interface(obj["prefix"])
            present = any(
                ipaddress.ip_interface(str(a["local"]) + "/" + str(a["prefixlen"]))
                == wanted
                for row in rows
                for a in row.get("addr_info", [])
            )
            result = {
                "code": code,
                "action": "CREATE" if present else "DELETE",
                "address": obj,
            }
        elif code == "INTERFACE" and op["interface"]["name"] in pairs:
            obj = op["interface"]
            row = json.loads(self.run("ip", "-j", "link", "show", "dev", obj["name"]))[
                0
            ]
            fields = {"name": obj["name"]}
            if "up" in obj:
                fields["up"] = "UP" in row["flags"]
            if "mtu" in obj:
                fields["mtu"] = row["mtu"]
            if "mac" in obj:
                fields["mac"] = row["address"]
            result = {"code": code, "action": "SET", "interface": fields}
        elif code == "STATIC_ROUTE":
            obj = op["route"]
            family = "-6" if route_network(obj).version == 6 else "-4"
            rows = json.loads(
                self.run(
                    "ip",
                    family,
                    "-j",
                    "route",
                    "show",
                    "exact",
                    obj["prefix"],
                    "table",
                    "main",
                )
            )
            present = any(
                r.get("dev") == obj["interface"]
                and r.get("gateway", "") == obj.get("gateway", "")
                for r in rows
            )
            result = {
                "code": code,
                "action": "CREATE" if present else "DELETE",
                "route": obj,
            }
        return result

    def request(self, method, body):
        if method == "health":
            self.api.call("show_version")
            return {"ready": True, "paused": self.paused, "error": self.last_error}
        if method == "prepare":
            if self.paused:
                raise RuntimeError("an interrupted VPP change requires Agent rollback")
            self.validate(body)
            return copy.deepcopy(self.model)
        if method == "apply":
            self.validate(body)
            self.set_model(updated(self.model, body))
            return {"applied": True}
        if method == "rollback":
            if (
                not isinstance(body, dict)
                or not {"bridges", "vxlans", "ports"} <= set(body)
                or set(body) - set(empty())
            ):
                raise ValueError("invalid saved VPP Undo")
            self.set_model(normalized(body))
            return {"restored": True}
        if method in ("verify", "verify_rollback"):
            if method == "verify_rollback":
                body = self.rollback_expectation(body)
            # LCP delivery is asynchronous; do not report a queued update as success.
            deadline = time.monotonic() + 5
            while True:
                try:
                    return self.verify(body)
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
        if method == "query":
            return self.query(body)
        if method == "status":
            self.api.call("show_version")
            return {
                "dataplane": "vpp",
                "scope": "l2-evpn",
                "paused": self.paused,
                "lastError": self.last_error,
                "lastSuccess": self.last_success,
                "cycles": self.cycles,
                "managedNetworks": len(self.model["vxlans"]),
                "localMacs": len(self.local),
            }
        raise ValueError("unknown adapter method")


class Server(socketserver.UnixStreamServer):
    allow_reuse_address = True
    timeout = 0.25


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 1 << 20:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length)) if length else None
            value = self.server.engine.request(self.path.lstrip("/"), body)
            status = 200
        except Exception as exc:
            LOG.error("request %s: %s", self.path, exc)
            self.server.engine.last_error = str(exc)
            value = {"error": str(exc)}
            status = 409
        data = json.dumps(plain(value)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # caller canceled; journaled operation/recovery state remains

    def log_message(self, *_):
        pass


def health(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(path)
        client.sendall(b"POST /health HTTP/1.0\r\nContent-Length: 4\r\n\r\nnull")
        if b" 200 " not in client.recv(4096).split(b"\r\n", 1)[0]:
            raise RuntimeError("adapter health check failed")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--socket", default="/run/sdn-agent/vpp.sock")
    p.add_argument("--api", default="/run/vpp/api.sock")
    p.add_argument("--api-dir", default="/usr/share/vpp/api")
    p.add_argument("--state", default="/var/lib/sdn-agent/vpp.json")
    p.add_argument("--health", action="store_true")
    args = p.parse_args()
    if args.health:
        health(args.socket)
        return
    logging.basicConfig(level=logging.INFO)
    api = VPP(args.api, args.api_dir)
    from irb import IRBEngine

    engine = IRBEngine(api, args.state)
    engine.restore_mirrors()
    path = Path(args.socket)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        try:
            health(args.socket)
        except (OSError, RuntimeError):
            path.unlink()
        else:
            raise RuntimeError("another adapter owns the socket")

    def stop(*_):
        engine.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with Server(args.socket, Handler) as server:
        os.chmod(args.socket, 0o600)
        server.engine = engine
        next_sync = 0
        while engine.running:
            server.handle_request()
            if time.monotonic() >= next_sync:
                if not engine.paused:
                    try:
                        engine.reconcile()
                    except Exception as exc:
                        engine.last_error = str(exc)
                        LOG.error("reconcile: %s", exc)
                next_sync = time.monotonic() + 1
    api.client.disconnect()
    path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
