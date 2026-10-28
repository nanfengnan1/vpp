"""VPP-owned tenant FIBs/BVIs and selected FRR EVPN route synchronization.

Linux objects are control-plane mirrors. Their bridge ports drop packets with
tc, preserving carrier for Zebra without a Linux bridge/VXLAN forwarding path.
No remote VTEP, MAC or route is synthesized from an unselected BGP advertisement.
"""

import copy
import ipaddress
import json
import time

from adapter import ALL, PREFIX, Engine, bd_id, plain, route_network, updated
import keepalive

SOURCE = PREFIX + "evpn"


def external(row):
    return "extern_learn" in row or "extern_learn" in row.get("flags", [])


def valid_neighbor(row):
    return row.get("lladdr") and not {"FAILED", "INCOMPLETE"}.intersection(
        row.get("state", [])
    )


def expand_hops(row, lookup, seen=()):
    """iproute2 may return inline paths or nexthop-object/group references."""
    if "nexthops" in row:
        return [p for h in row["nexthops"] for p in expand_hops(h, lookup, seen)]
    if row.get("dev"):
        return [] if "dead" in row.get("flags", []) else [row]
    ident = row.get("nhid", row.get("id"))
    if ident is None or ident in seen:
        raise RuntimeError("unresolvable or cyclic selected nexthop")
    obj = lookup(ident)
    if "group" in obj:
        result = []
        for member in obj["group"]:
            for path in expand_hops({"nhid": member["id"]}, lookup, (*seen, ident)):
                result.append({**path, "weight": member.get("weight", 1)})
        return result
    return expand_hops(obj, lookup, (*seen, ident))


class IRBEngine(Engine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = None
        self.published_neighbors = {}

    def tables(self):
        return {
            (r.table.table_id, bool(r.table.is_ip6)): plain(r.table)
            for r in self.api.call("ip_table_dump")
        }

    def ip6_enabled(self, index):
        try:
            self.api.call("sw_interface_ip6_get_link_local_address", sw_if_index=index)
            return True
        except RuntimeError as exc:
            if str(exc) != "sw_interface_ip6_get_link_local_address: VPP retval -62":
                raise
            return False

    def bvis(self, interfaces=None):
        if interfaces is None:
            interfaces = self.api.snapshot()[0]
        return {
            v["tag"][len(PREFIX + "bvi/") :]: i
            for i, v in interfaces.items()
            if v.get("tag", "").startswith(PREFIX + "bvi/")
        }

    def table_for(self, bridge):
        tenant = self.model["bindings"].get(bridge, {}).get("tenant")
        return int(self.model["tenants"][tenant]["tableId"]) if tenant else 0

    def loopback(self):
        interfaces = self.api.snapshot()[0]
        matches = [
            i for i, r in interfaces.items() if r.get("tag") == PREFIX + "loopback/lo"
        ]
        if len(matches) > 1:
            raise RuntimeError("duplicate managed loopback")
        return matches[0] if matches else None

    def sync_loopback(self):
        wanted = {
            str(ipaddress.ip_interface(a["prefix"]))
            for a in self.model["addresses"].values()
            if a["interface"] == "lo"
        }
        index = self.loopback()
        if index is None and not wanted:
            return
        if index is None:
            index = self.api.call(
                "create_loopback", mac_address="02:00:00:00:00:01"
            ).sw_if_index
            self.api.call(
                "sw_interface_tag_add_del",
                sw_if_index=index,
                is_add=True,
                tag=PREFIX + "loopback/lo",
            )
            self.api.call("sw_interface_set_flags", sw_if_index=index, flags=1)
        for ipv6 in (False, True):
            current = {
                str(r.prefix)
                for r in self.api.call(
                    "ip_address_dump", sw_if_index=index, is_ipv6=ipv6
                )
            }
            want = {
                p for p in wanted if (ipaddress.ip_interface(p).version == 6) == ipv6
            }
            for prefix in current - want:
                self.api.call(
                    "sw_interface_add_del_address",
                    sw_if_index=index,
                    is_add=False,
                    prefix=prefix,
                )
            for prefix in want - current:
                self.api.call(
                    "sw_interface_add_del_address",
                    sw_if_index=index,
                    is_add=True,
                    prefix=prefix,
                )
        if not wanted:
            self.api.call("delete_loopback", sw_if_index=index)

    def validate(self, op):
        keepalive.validate(updated(self.model, op))
        if op["code"] == "NEIGHBOR_KEEPALIVE":
            self.api.call("neighbor_keepalive_status")
            return
        code, action = op["code"], op["action"]
        deleting = action in ("DELETE", "UNBIND")
        if code == "TENANT":
            obj = op["tenant"]
            name = obj["name"]
            if deleting:
                if (
                    any(
                        x.get("tenant") == name for x in self.model["bindings"].values()
                    )
                    or name in self.model["l3vnis"]
                ):
                    raise ValueError(
                        "unbind tenant bridges and L3VNI before deleting the tenant"
                    )
            else:
                table = int(obj["tableId"])
                if table in (0, 253, 254, 255) or table >= ALL:
                    raise ValueError(
                        "tenant requires a dedicated non-reserved table ID"
                    )
                for (tid, _), row in self.tables().items():
                    if tid == table and row["name"] != PREFIX + "vrf/" + name:
                        raise ValueError("tenant FIB collides with an unmanaged table")
                if any(
                    n != name and int(t["tableId"]) == table
                    for n, t in self.model["tenants"].items()
                ):
                    raise ValueError("tenant table ID is already owned")
                if (
                    name in self.model["tenants"]
                    and int(self.model["tenants"][name]["tableId"]) != table
                ):
                    raise ValueError(
                        "delete and recreate a tenant to change its table ID"
                    )
            return
        if code == "BD_TENANT":
            b = op["binding"]
            if not deleting and (
                b["bridge"] not in self.model["bridges"]
                or b["tenant"] not in self.model["tenants"]
            ):
                raise ValueError("create the managed BD and tenant before binding")
            return
        if code == "TENANT_L3VNI":
            x = op["l3vni"]
            if not deleting:
                matches = [
                    n for n, v in self.model["vxlans"].items() if v["vni"] == x["vni"]
                ]
                if (
                    len(matches) != 1
                    or self.model["bindings"]
                    .get(self.model["ports"].get(matches[0], {}).get("bridge"), {})
                    .get("tenant")
                    != x["tenant"]
                ):
                    raise ValueError(
                        "L3VNI must reference a VXLAN BD bound to this tenant"
                    )
                if any(
                    n != x["tenant"] and v["vni"] == x["vni"]
                    for n, v in self.model["l3vnis"].items()
                ):
                    raise ValueError("L3VNI already belongs to another tenant")
            return
        if code in ("ADDRESS", "INTERFACE"):
            obj = op["address"] if code == "ADDRESS" else op["interface"]
            name = obj.get("interface", obj.get("name"))
            if (
                code == "INTERFACE"
                and name == "lo"
                and (obj.get("up", True) is False or "mac" in obj or "mtu" in obj)
            ):
                raise ValueError(
                    "managed VPP loopback supports addresses and up=true only"
                )
            if (
                name in self.model["bridges"]
                or name in self.model["tenants"]
                or name in self.model["vxlans"]
                or name == "lo"
            ):
                if (
                    code == "ADDRESS"
                    and name not in self.model["bridges"]
                    and name != "lo"
                ):
                    raise ValueError(
                        "put gateway addresses on the BD/SVI, not on VRF or VXLAN"
                    )
                if (
                    code == "INTERFACE"
                    and name in self.model["tenants"]
                    and any(k in obj for k in ("mac", "mtu"))
                ):
                    raise ValueError("set tenant MAC/MTU on its BD/SVI")
                return
        if code == "BD_PORT":
            candidate = copy.deepcopy(op)
            candidate["bridgePort"].pop("neighSuppress", None)
            return super().validate(candidate)
        if code == "BD" and deleting:
            name = op["bridge"]["name"]
            if name in self.model["bindings"] or any(
                a["interface"] == name for a in self.model["addresses"].values()
            ):
                raise ValueError(
                    "remove gateway addresses and tenant binding before deleting BD"
                )
        if code == "STATIC_ROUTE" and op["route"].get("table", 0) not in (0, 254):
            r = op["route"]
            if int(r["table"]) not in {
                int(t["tableId"]) for t in self.model["tenants"].values()
            }:
                raise ValueError("route table must belong to a managed tenant")
            if r.get("interface") not in self.model["bridges"] or self.table_for(
                r["interface"]
            ) != int(r["table"]):
                raise ValueError(
                    "tenant route output must be a BD bound to that tenant"
                )
            return
        return super().validate(op)

    def ensure_source(self):
        if self.source is None:
            found = [
                r.src.id
                for r in self.api.call("fib_source_dump")
                if r.src.name == SOURCE
            ]
            self.source = (
                found[0]
                if found
                else self.api.call(
                    "fib_source_add", src={"name": SOURCE, "priority": 0x80}
                ).id
            )

    def route_rows(self, table, ipv6):
        return [
            plain(r.route)
            for r in self.api.call(
                "ip_route_v2_dump",
                src=self.source,
                table={"table_id": table, "is_ip6": ipv6},
            )
        ]

    def write_route(self, route, add):
        paths = [
            {**p, "label_stack": p.get("label_stack", [{} for _ in range(16)])}
            for p in route["paths"]
        ]
        self.api.call(
            "ip_route_add_del_v2",
            is_add=add,
            is_multipath=False,
            route={
                "table_id": route["table_id"],
                "prefix": route["prefix"],
                "src": self.source,
                "n_paths": len(paths),
                "paths": paths,
            },
        )

    def clean_bvis(self):
        interfaces, _, _, _ = self.api.snapshot()
        obsolete = {
            n: i
            for n, i in self.bvis(interfaces).items()
            if n not in self.model["bridges"]
        }
        for name, i in obsolete.items():
            for ipv6 in (False, True):
                for table, family in self.tables():
                    if family != ipv6:
                        continue
                    for route in self.route_rows(table, ipv6):
                        if any(p["sw_if_index"] == i for p in route["paths"]):
                            self.write_route(route, False)
                for a in self.api.call("ip_address_dump", sw_if_index=i, is_ipv6=ipv6):
                    self.api.call(
                        "sw_interface_add_del_address",
                        sw_if_index=i,
                        is_add=False,
                        prefix=a.prefix,
                    )
            self.member(i, bd_id(name), enable=False)
            self.api.call("bvi_delete", sw_if_index=i)

    def ensure_bvis(self):
        interfaces, bridges, _, _ = self.api.snapshot()
        bvis = self.bvis(interfaces)
        links = {
            r["ifname"]: r for r in json.loads(self.run("ip", "-j", "link", "show"))
        }
        for name, obj in self.model["bridges"].items():
            if name not in links:
                raise RuntimeError("missing Linux control mirror " + name)
            mac = obj.get("mac", links[name]["address"])
            if "mac" not in obj:
                obj["mac"] = mac
                self.save()
            # Restore drift without setting an unchanged MAC each cycle: Linux
            # flushes permanent neighbors even for a same-address link update.
            if links[name]["address"].lower() != mac.lower():
                self.run("ip", "link", "set", "dev", name, "address", mac)
            index = bvis.get(name)
            if index is None:
                existing = bridges[bd_id(name)].get("bvi_sw_if_index", ALL)
                if existing != ALL:
                    raise RuntimeError("unmanaged BVI already belongs to BD " + name)
                index = self.api.call(
                    "bvi_create", mac=mac, user_instance=ALL
                ).sw_if_index
                self.api.call(
                    "sw_interface_tag_add_del",
                    sw_if_index=index,
                    is_add=True,
                    tag=PREFIX + "bvi/" + name,
                )
                self.api.call(
                    "sw_interface_set_l2_bridge",
                    rx_sw_if_index=index,
                    bd_id=bd_id(name),
                    shg=0,
                    port_type=1,
                    enable=True,
                )
                bvis[name] = index
            elif interfaces[index]["l2_address"].lower() != mac.lower():
                self.api.call(
                    "sw_interface_set_mac_address", sw_if_index=index, mac_address=mac
                )
            table = self.table_for(name)
            for ipv6 in (False, True):
                have = {
                    str(r.prefix)
                    for r in self.api.call(
                        "ip_address_dump", sw_if_index=index, is_ipv6=ipv6
                    )
                }
                want = {
                    str(ipaddress.ip_interface(a["prefix"]))
                    for a in self.model["addresses"].values()
                    if a["interface"] == name
                    and (ipaddress.ip_interface(a["prefix"]).version == 6) == ipv6
                }
                oldtable = self.api.call(
                    "sw_interface_get_table", sw_if_index=index, is_ipv6=ipv6
                ).vrf_id
                for prefix in have - want if oldtable == table else have:
                    self.api.call(
                        "sw_interface_add_del_address",
                        sw_if_index=index,
                        is_add=False,
                        prefix=prefix,
                    )
                if oldtable != table:
                    if ipv6:
                        # Link enable holds MFIB membership in the old table.
                        # Release it before rebinding this exclusively owned BVI.
                        for _ in range(4096):
                            if not self.ip6_enabled(index):
                                break
                            self.api.call(
                                "sw_interface_ip6_enable_disable",
                                sw_if_index=index,
                                enable=False,
                            )
                        else:
                            raise RuntimeError("BVI IPv6 link references did not drain")
                    self.api.call(
                        "sw_interface_set_table",
                        sw_if_index=index,
                        is_ipv6=ipv6,
                        vrf_id=table,
                    )
                    have = set()
                for prefix in want - have:
                    self.api.call(
                        "sw_interface_add_del_address",
                        sw_if_index=index,
                        is_add=True,
                        prefix=prefix,
                    )
                # L3VNI BVI is unnumbered but must route received inner IP packets.
                if ipv6:
                    if not self.ip6_enabled(index):
                        self.api.call(
                            "sw_interface_ip6_enable_disable",
                            sw_if_index=index,
                            enable=True,
                        )
                else:
                    # The 26.06 ip4_enable API enables the *drop* feature and
                    # does not convert sw_if_index to host byte order. Use the
                    # public feature API to enable unnumbered L3VNI reception.
                    self.api.call(
                        "feature_enable_disable",
                        sw_if_index=index,
                        arc_name="ip4-unicast",
                        feature_name="ip4-not-enabled",
                        enable=False,
                    )
            tenant = self.model["bindings"].get(name, {}).get("tenant")
            up = obj.get("up", False) and (
                not tenant or self.model["tenants"][tenant].get("up", True)
            )
            self.api.call(
                "sw_interface_set_flags", sw_if_index=index, flags=1 if up else 0
            )
            self.api.call(
                "sw_interface_set_mtu",
                sw_if_index=index,
                mtu=[obj.get("mtu", links[name]["mtu"]), 0, 0, 0],
            )
        return bvis

    def isolate_mirrors(self):
        for name in self.model["ports"]:
            qdiscs = json.loads(self.run("tc", "-j", "qdisc", "show", "dev", name))
            if not any(q["kind"] == "clsact" for q in qdiscs):
                self.run("tc", "qdisc", "add", "dev", name, "clsact")
            for direction in ("ingress", "egress"):
                rules = json.loads(
                    self.run(
                        "tc",
                        "-j",
                        "filter",
                        "show",
                        "dev",
                        name,
                        direction,
                        "pref",
                        "49150",
                    )
                )
                rules = [r for r in rules if "options" in r]
                if rules and not all(
                    r.get("kind") == "matchall"
                    and any(
                        a.get("kind") == "gact"
                        and a.get("control_action", {}).get("type") == "drop"
                        for a in r.get("options", {}).get("actions", [])
                    )
                    for r in rules
                ):
                    raise RuntimeError(
                        "TC priority 49150 on mirror belongs to another owner"
                    )
                if not rules:
                    self.run(
                        "tc",
                        "filter",
                        "add",
                        "dev",
                        name,
                        direction,
                        "pref",
                        "49150",
                        "handle",
                        "1",
                        "matchall",
                        "action",
                        "drop",
                    )

    def release_mirrors(self):
        for name in set((self.previous or self.model)["ports"]) - set(
            self.model["ports"]
        ):
            for direction in ("ingress", "egress"):
                rules = json.loads(
                    self.run(
                        "tc",
                        "-j",
                        "filter",
                        "show",
                        "dev",
                        name,
                        direction,
                        "pref",
                        "49150",
                    )
                )
                owned = [
                    r
                    for r in rules
                    if r.get("kind") == "matchall"
                    and r.get("options", {}).get("handle") == 1
                    and any(
                        a.get("kind") == "gact"
                        and a.get("control_action", {}).get("type") == "drop"
                        for a in r.get("options", {}).get("actions", [])
                    )
                ]
                if owned:
                    self.run(
                        "tc",
                        "filter",
                        "del",
                        "dev",
                        name,
                        direction,
                        "pref",
                        "49150",
                        "handle",
                        "1",
                        "matchall",
                    )

    def sync_neighbors(self, bvis):
        failed_bindings = keepalive.failed(self)
        kernel = json.loads(self.run("ip", "-j", "neigh", "show"))
        for row in kernel:
            if (
                row.get("dev") in bvis
                and not external(row)
                and "PERMANENT" in row.get("state", [])
                and valid_neighbor(row)
            ):
                self.published_neighbors.setdefault(
                    (row["dev"], row["dst"]), row["lladdr"]
                )
        desired_remote = {}
        published = {}
        for name, index in bvis.items():
            for af in (0, 1):
                rows = [
                    plain(r.neighbor)
                    for r in self.api.call("ip_neighbor_dump", sw_if_index=index, af=af)
                ]
                native = {str(r["ip_address"]): r for r in rows}
                for r in kernel:
                    if r.get("dev") == name and valid_neighbor(r) and external(r):
                        desired_remote[(index, r["dst"])] = r["lladdr"]
                for ip, row in native.items():
                    key = (index, ip)
                    if row["flags"] & 1:
                        wanted = desired_remote.get(key)
                        if wanted != row["mac_address"]:
                            self.api.call(
                                "ip_neighbor_add_del", is_add=False, neighbor=row
                            )
                        continue
                    # Only advertise neighbors whose MAC was learned on a local access port.
                    mac = row["mac_address"]
                    if (index, ip, mac) in failed_bindings:
                        continue
                    if (name, mac) not in self.local or any(
                        r.get("dev") == name and r.get("dst") == ip and external(r)
                        for r in kernel
                    ):
                        continue
                    published[(name, ip)] = mac
                    if not any(
                        r.get("dev") == name
                        and r.get("dst") == ip
                        and r.get("lladdr") == mac
                        and "PERMANENT" in r.get("state", [])
                        for r in kernel
                    ):
                        # Linux cannot probe through the isolated mirror. VPP
                        # owns reachability; its local FDB/neighbor withdrawal
                        # removes this publication instead of kernel NUD timers.
                        self.run(
                            "ip",
                            "neigh",
                            "replace",
                            ip,
                            "lladdr",
                            mac,
                            "dev",
                            name,
                            "nud",
                            "permanent",
                        )
                for (i, ip), mac in desired_remote.items():
                    if i != index or (ipaddress.ip_address(ip).version == 6) != bool(
                        af
                    ):
                        continue
                    old = native.get(ip)
                    if not old or old["mac_address"] != mac or not old["flags"] & 1:
                        self.api.call(
                            "ip_neighbor_add_del",
                            is_add=True,
                            neighbor={
                                "sw_if_index": i,
                                "ip_address": ip,
                                "mac_address": mac,
                                "flags": 3,
                            },
                        )
        for (name, ip), mac in self.published_neighbors.items():
            if (
                name in bvis
                and (name, ip) not in published
                and not any(
                    r.get("dev") == name
                    and r.get("dst") == ip
                    and (external(r) or r.get("lladdr") not in (None, mac))
                    for r in kernel
                )
            ):
                # FRR treats RTM_DELNEIGH with NUD_PERMANENT as an RFC5549
                # next-hop deletion and skips EVPN withdrawal. First publish
                # an explicit reachability failure for this owned local IP,
                # even if a prior link change already removed the kernel item.
                # Never overwrite a new MAC or a remote winner for that IP.
                self.run(
                    "ip",
                    "neigh",
                    "replace",
                    ip,
                    "lladdr",
                    mac,
                    "dev",
                    name,
                    "nud",
                    "failed",
                )
                self.run("ip", "neigh", "del", ip, "dev", name)
        self.published_neighbors = published
        # Native BD ARP/ND termination is fed only by FRR-selected remote bindings.
        for name, index in bvis.items():
            enabled = any(
                p["bridge"] == name and p.get("neighSuppress")
                for p in self.model["ports"].values()
            )
            desired = (
                {ip: mac for (i, ip), mac in desired_remote.items() if i == index}
                if enabled
                else {}
            )
            current = {
                str(r.entry.ip): str(r.entry.mac)
                for r in self.api.call("bd_ip_mac_dump", bd_id=bd_id(name))
            }
            for ip, mac in current.items():
                if desired.get(ip) != mac:
                    self.api.call(
                        "bd_ip_mac_add_del",
                        is_add=False,
                        entry={"bd_id": bd_id(name), "ip": ip, "mac": mac},
                    )
            for ip, mac in desired.items():
                if current.get(ip) != mac:
                    self.api.call(
                        "bd_ip_mac_add_del",
                        is_add=True,
                        entry={"bd_id": bd_id(name), "ip": ip, "mac": mac},
                    )
            self.api.call("bridge_flags", bd_id=bd_id(name), is_set=enabled, flags=0x10)

    def kernel_routes(self, table, ipv6, bvis):
        rows = json.loads(
            self.run(
                "ip", "-6" if ipv6 else "-4", "-j", "route", "show", "table", str(table)
            )
        )
        desired = {}
        for row in rows:
            if (
                row.get("protocol") in ("kernel", "local")
                or row.get("type", "unicast") != "unicast"
            ):
                continue
            prefix = row.get("dst", "default")
            prefix = str(
                ipaddress.ip_network(
                    (
                        ("::/0" if ipv6 else "0.0.0.0/0")
                        if prefix == "default"
                        else prefix
                    ),
                    strict=False,
                )
            )
            paths = []

            def lookup(ident):
                objects = json.loads(
                    self.run("ip", "-j", "nexthop", "show", "id", str(ident))
                )
                if len(objects) != 1:
                    raise RuntimeError("selected nexthop is missing")
                return objects[0]

            for hop in expand_hops(row, lookup):
                dev = hop.get("dev")
                if dev not in bvis or self.table_for(dev) != table:
                    raise RuntimeError(
                        f"selected tenant route {prefix} has unsupported output {dev}"
                    )
                gateway = hop.get(
                    "gateway",
                    hop.get("via", {}).get("host", "::" if ipv6 else "0.0.0.0"),
                )
                nh6 = ipaddress.ip_address(gateway).version == 6
                paths.append(
                    {
                        "sw_if_index": bvis[dev],
                        "table_id": table,
                        "weight": hop.get("weight", 1),
                        "preference": 0,
                        "type": 0,
                        "flags": 0,
                        "proto": 1 if nh6 else 0,
                        "nh": {"address": {"ip6" if nh6 else "ip4": gateway}},
                    }
                )
            if paths:
                desired[prefix] = {"table_id": table, "prefix": prefix, "paths": paths}
        return desired

    @staticmethod
    def path_keys(paths):
        return sorted(
            (
                p["sw_if_index"],
                p.get("weight", 1),
                str(
                    p.get("nh", {})
                    .get("address", {})
                    .get("ip6" if int(p.get("proto", 0)) == 1 else "ip4", "")
                ),
            )
            for p in paths
        )

    def sync_routes(self, bvis):
        active = {int(t["tableId"]) for t in self.model["tenants"].values()}
        for (table, ipv6), row in self.tables().items():
            if not row["name"].startswith(PREFIX + "vrf/"):
                continue
            desired = self.kernel_routes(table, ipv6, bvis) if table in active else {}
            current = {r["prefix"]: r for r in self.route_rows(table, ipv6)}
            for prefix, route in current.items():
                if prefix not in desired:
                    self.write_route(route, False)
            for prefix, route in desired.items():
                old = current.get(prefix)
                if not old or self.path_keys(old["paths"]) != self.path_keys(
                    route["paths"]
                ):
                    self.write_route(route, True)
            if table not in active:
                self.api.call(
                    "ip_table_add_del",
                    is_add=False,
                    table={"table_id": table, "is_ip6": ipv6, "name": row["name"]},
                )

    def reconcile(self):
        self.ensure_source()
        self.sync_loopback()
        tables = self.tables()
        for name, tenant in self.model["tenants"].items():
            for ipv6 in (False, True):
                key = (int(tenant["tableId"]), ipv6)
                if key in tables and tables[key]["name"] != PREFIX + "vrf/" + name:
                    raise RuntimeError("tenant FIB ownership conflict")
                if key not in tables:
                    self.api.call(
                        "ip_table_add_del",
                        is_add=True,
                        table={
                            "table_id": key[0],
                            "is_ip6": ipv6,
                            "name": PREFIX + "vrf/" + name,
                        },
                    )
        self.clean_bvis()
        self.release_mirrors()
        super().reconcile()
        bvis = self.ensure_bvis()
        keepalive.reconcile(self, bvis)
        self.isolate_mirrors()
        self.sync_neighbors(bvis)
        self.sync_routes(bvis)
        interfaces, _, tunnels, _ = self.api.snapshot()
        for index in tunnels:
            tag = interfaces[index].get("tag", "")
            if not tag.startswith(PREFIX + "vx/"):
                continue
            obj = self.model["vxlans"][tag.split("/")[2]]
            self.api.call(
                "sw_interface_set_flags",
                sw_if_index=index,
                flags=1 if obj.get("up", True) else 0,
            )
            if "mtu" in obj:
                self.api.call(
                    "sw_interface_set_mtu", sw_if_index=index, mtu=[obj["mtu"], 0, 0, 0]
                )
            if (
                "mac" in obj
                and interfaces[index]["l2_address"].lower() != obj["mac"].lower()
            ):
                self.api.call(
                    "sw_interface_set_mac_address",
                    sw_if_index=index,
                    mac_address=obj["mac"],
                )
        for name, obj in self.model["bridges"].items():
            if obj.get("up", True) is False:
                self.api.call(
                    "bridge_flags", bd_id=bd_id(name), flags=0x1E, is_set=False
                )
        self.last_success = time.time()

    def verify(self, op):
        if op["code"] == "NEIGHBOR_KEEPALIVE":
            return keepalive.verify(self)
        code = op["code"]
        if updated(self.model, op) != self.model:
            raise RuntimeError("VPP desired state does not contain operation")
        interfaces, domains, _, _ = self.api.snapshot()
        bvis = self.bvis(interfaces)
        if op.get("action") == "DELETE":
            if code == "BD" and (
                op["bridge"]["name"] in bvis or bd_id(op["bridge"]["name"]) in domains
            ):
                raise RuntimeError("deleted VPP BD or BVI remains")
            if code == "TENANT" and any(
                t.get("name") == PREFIX + "vrf/" + op["tenant"]["name"]
                for t in self.tables().values()
            ):
                raise RuntimeError("deleted VPP tenant FIB remains")
        address_indices = dict(bvis)
        if self.loopback() is not None:
            address_indices["lo"] = self.loopback()
        if code == "ADDRESS" and (
            op["address"]["interface"] in address_indices
            or op["address"]["interface"] == "lo"
        ):
            a = op["address"]
            prefix = ipaddress.ip_interface(a["prefix"])
            present = a["interface"] in address_indices and any(
                str(r.prefix) == str(prefix)
                for r in self.api.call(
                    "ip_address_dump",
                    sw_if_index=address_indices[a["interface"]],
                    is_ipv6=prefix.version == 6,
                )
            )
            if present != (op["action"] != "DELETE"):
                raise RuntimeError("VPP BVI address differs")
        elif code == "STATIC_ROUTE" and op["route"].get("table", 0) not in (0, 254):
            r = op["route"]
            prefix = route_network(r)
            self.sync_routes(bvis)
            present = any(
                x["prefix"] == str(prefix)
                and any(
                    p["sw_if_index"] == bvis.get(r["interface"])
                    and (
                        not r.get("gateway")
                        or r["gateway"] in p.get("nh", {}).get("address", {}).values()
                    )
                    for p in x["paths"]
                )
                for x in self.route_rows(r["table"], prefix.version == 6)
            )
            if present != (op["action"] != "DELETE"):
                raise RuntimeError("VPP tenant route differs")
        else:
            super().verify(op)
        for name, obj in self.model["bridges"].items():
            if name not in bvis:
                raise RuntimeError("VPP BVI missing")
            for ipv6 in (False, True):
                if self.api.call(
                    "sw_interface_get_table", sw_if_index=bvis[name], is_ipv6=ipv6
                ).vrf_id != self.table_for(name):
                    raise RuntimeError("VPP BVI tenant binding differs")
            native = interfaces[bvis[name]]
            tenant = self.model["bindings"].get(name, {}).get("tenant")
            up = obj.get("up", False) and (
                not tenant or self.model["tenants"][tenant].get("up", True)
            )
            if (
                bool(native["flags"] & 1) != up
                or (
                    obj.get("mac")
                    and native["l2_address"].lower() != obj["mac"].lower()
                )
                or (obj.get("mtu") and native["mtu"][0] != obj["mtu"])
            ):
                raise RuntimeError("VPP BVI link attributes differ")
        for name, obj in self.model["tenants"].items():
            if any(
                self.tables().get((int(obj["tableId"]), ipv6), {}).get("name")
                != PREFIX + "vrf/" + name
                for ipv6 in (False, True)
            ):
                raise RuntimeError("VPP tenant FIB missing")
        return {"verified": True}

    def rollback_expectation(self, op):
        if op["code"] == "NEIGHBOR_KEEPALIVE":
            return {
                "code": op["code"],
                "action": "SET",
                "neighborKeepalive": {"enabled": keepalive.enabled(self.model)},
            }
        if op["code"] == "STATIC_ROUTE" and op["route"].get("table", 0) not in (0, 254):
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
                    str(obj["table"]),
                )
            )
            present = any(
                r.get("dev") == obj["interface"]
                and r.get("gateway", "") == obj.get("gateway", "")
                for r in rows
            )
            return {
                "code": "STATIC_ROUTE",
                "action": "CREATE" if present else "DELETE",
                "route": obj,
            }
        return super().rollback_expectation(op)

    def query(self, q):
        if q.get("kind") == "NEIGHBOR_KEEPALIVE_STATUS":
            return keepalive.query(self, q)
        kind = q.get("kind")
        interfaces, _, tunnels, pairs = self.api.snapshot()
        bvis = self.bvis(interfaces)
        names = {i: row["interface_name"] for i, row in interfaces.items()}
        names.update({i: n for n, i in {**pairs, **bvis}.items()})
        for index in tunnels:
            tag = interfaces[index].get("tag", "")
            if tag.startswith(PREFIX + "vx/"):
                names[index] = tag.split("/")[2]
        if self.loopback() is not None:
            names[self.loopback()] = "lo"
        if q.get("vrf") and q["vrf"] not in self.model["tenants"]:
            raise ValueError("unknown VPP tenant VRF")
        if q.get("bridge") and q["bridge"] not in self.model["bridges"]:
            raise ValueError("unknown managed VPP bridge")
        if q.get("interface") and q["interface"] not in set(names.values()) | {
            row["interface_name"] for row in interfaces.values()
        }:
            raise ValueError("unknown VPP interface")
        tables = self.tables()
        if kind == "TENANTS":
            return [
                {
                    "name": n,
                    "tableId": t["tableId"],
                    "ipv4": (int(t["tableId"]), False) in tables,
                    "ipv6": (int(t["tableId"]), True) in tables,
                    "l3vni": self.model["l3vnis"].get(n, {}).get("vni"),
                }
                for n, t in self.model["tenants"].items()
                if not q.get("vrf") or n == q["vrf"]
            ]
        if kind not in ("ADDRESSES", "NEIGHBORS", "ROUTES", "INTERFACES"):
            return super().query(q)
        rows = []
        for index, name in names.items():
            bridge = self.model["ports"].get(name, {}).get("bridge", name)
            tenant = self.model["bindings"].get(bridge, {}).get("tenant", "")
            if (
                q.get("vrf")
                and q["vrf"] != tenant
                or q.get("interface")
                and q["interface"] not in (name, interfaces[index]["interface_name"])
                or q.get("bridge")
                and q["bridge"] != bridge
            ):
                continue
            if kind == "INTERFACES":
                row = interfaces[index]
                rows.append(
                    {
                        "name": name,
                        "vrf": tenant,
                        "vppName": row["interface_name"],
                        "swIfIndex": index,
                        "up": bool(row["flags"] & 1),
                        "linkUp": bool(row["flags"] & 2),
                        "kind": (
                            "vxlan"
                            if index in tunnels
                            else "bvi" if index in bvis.values() else "interface"
                        ),
                        "mtu": row["mtu"][0],
                        "mac": row["l2_address"],
                    }
                )
            for ipv6 in (False, True):
                if q.get("family") not in (
                    None,
                    "AF_UNSPECIFIED",
                    "IPV6" if ipv6 else "IPV4",
                ):
                    continue
                if kind == "ADDRESSES":
                    rows += [
                        {"interface": name, "vrf": tenant, "prefix": str(r.prefix)}
                        for r in self.api.call(
                            "ip_address_dump", sw_if_index=index, is_ipv6=ipv6
                        )
                    ]
                if kind == "NEIGHBORS":
                    rows += [
                        {"interface": name, "vrf": tenant, **plain(r.neighbor)}
                        for r in self.api.call(
                            "ip_neighbor_dump", sw_if_index=index, af=int(ipv6)
                        )
                    ]
        if kind == "ROUTES":
            for name, t in {"": {"tableId": 0}, **self.model["tenants"]}.items():
                if q.get("vrf") and q["vrf"] != name:
                    continue
                for ipv6 in (False, True):
                    if q.get("family") in (
                        None,
                        "AF_UNSPECIFIED",
                        "IPV6" if ipv6 else "IPV4",
                    ):
                        rows += [
                            {
                                "vrf": name,
                                "tableId": int(t["tableId"]),
                                "family": "IPV6" if ipv6 else "IPV4",
                                **plain(r.route),
                            }
                            for r in self.api.call(
                                "ip_route_dump",
                                table={"table_id": int(t["tableId"]), "is_ip6": ipv6},
                            )
                        ]
            if q.get("interface"):
                indices = {
                    i
                    for i, n in names.items()
                    if q["interface"] in (n, interfaces[i]["interface_name"])
                }
                rows = [
                    r
                    for r in rows
                    if any(p["sw_if_index"] in indices for p in r.get("paths", []))
                ]
        if q.get("prefix"):
            rows = [r for r in rows if r.get("prefix") == q["prefix"]]
        if q.get("neighbor"):
            neighbor = str(ipaddress.ip_address(q["neighbor"]))
            rows = [r for r in rows if r.get("ip_address") == neighbor]
        return rows

    def restore_mirrors(self):
        if self.paused:
            return
        links = {
            r["ifname"]: r
            for r in json.loads(self.run("ip", "-j", "-d", "link", "show"))
        }
        for name, t in self.model["tenants"].items():
            if name not in links:
                self.run(
                    "ip", "link", "add", name, "type", "vrf", "table", str(t["tableId"])
                )
            elif links[name].get("linkinfo", {}).get("info_kind") != "vrf" or int(
                links[name]["linkinfo"]["info_data"]["table"]
            ) != int(t["tableId"]):
                raise RuntimeError("tenant mirror ownership conflict")
            self.run("ip", "link", "set", name, "up" if t.get("up", True) else "down")
        super().restore_mirrors()
        for name, b in self.model["bindings"].items():
            self.run("ip", "link", "set", name, "master", b["tenant"])
        for a in self.model["addresses"].values():
            self.run("ip", "addr", "replace", a["prefix"], "dev", a["interface"])
        self.isolate_mirrors()

    def request(self, method, body):
        value = super().request(method, body)
        if method == "status":
            value["scope"] = "evpn-irb"
            value["tenants"] = len(self.model["tenants"])
        return value
