"""VPP native neighbor keepalive policy and readback (Apache-2.0).

The native plugin discovers dynamic local neighbors and runs probes even when
this process is disconnected. This module only owns policy and access scopes.
"""

import ipaddress

from adapter import plain

STATES = ("reachable", "probing", "failed", "send-error")


def enabled(model):
    return model.get("neighborKeepalive", {}).get("enabled", False)


def validate(model):
    if enabled(model) and any(
        b.get("ageingTime", 300) == 60 for b in model["bridges"].values()
    ):
        raise ValueError("neighbor keepalive requires MAC ageing 0 or >=120 seconds")


def reconcile(engine, bvis):
    api = engine.api
    # Old VPP builds remain usable with the feature disabled, but cannot claim
    # that enabling this policy succeeded.
    if not hasattr(api.client.api, "neighbor_keepalive_status"):
        if enabled(engine.model):
            raise RuntimeError("VPP lacks neighbor_keepalive plugin; rebuild/enable it")
        return
    validate(engine.model)
    desired_enabled = enabled(engine.model)
    status = api.call("neighbor_keepalive_status")
    if not desired_enabled and status.enabled:
        api.call("neighbor_keepalive_set", enabled=False)
    _, _, _, pairs = api.snapshot()
    desired = (
        {
            (bvis[p["bridge"]], pairs[name])
            for name, p in engine.model["ports"].items()
            if p["bridge"] in bvis
            and name in pairs
            and name not in engine.model["vxlans"]
        }
        if desired_enabled
        else set()
    )
    actual = {(r.bvi, r.port) for r in api.call("neighbor_keepalive_scope_dump")}
    for bvi, port in actual - desired:
        api.call("neighbor_keepalive_scope_add_del", is_add=False, bvi=bvi, port=port)
    for bvi, port in desired - actual:
        api.call("neighbor_keepalive_scope_add_del", is_add=True, bvi=bvi, port=port)
    if desired_enabled and not status.enabled:
        api.call("neighbor_keepalive_set", enabled=True)


def verify(engine):
    status = engine.api.call("neighbor_keepalive_status")
    if bool(status.enabled) != enabled(engine.model):
        raise RuntimeError("VPP neighbor keepalive policy readback mismatch")
    return {"verified": True}


def failed(engine):
    if not enabled(engine.model):
        return set()
    return {
        (r.bvi, str(r.address), str(r.mac))
        for r in engine.api.call("neighbor_keepalive_dump")
        if r.state == 2
    }


def query(engine, q):
    status = plain(engine.api.call("neighbor_keepalive_status"))
    bvis = engine.bvis()
    _, _, _, pairs = engine.api.snapshot()
    names = {i: n for n, i in bvis.items()}
    ports = {i: n for n, i in pairs.items()}
    result = []
    for raw in engine.api.call("neighbor_keepalive_dump"):
        r = plain(raw)
        bridge = names.get(r["bvi"], str(r["bvi"]))
        interface = ports.get(r["port"], str(r["port"]))
        tenant = engine.model["bindings"].get(bridge, {}).get("tenant", "")
        family = "IPV6" if ipaddress.ip_address(r["address"]).version == 6 else "IPV4"
        row = {
            "enabled": bool(status["enabled"]),
            "bridge": bridge,
            "interface": interface,
            "vrf": tenant,
            "family": family,
            "ip": r["address"],
            "mac": r["mac"],
            "state": STATES[r["state"]],
            "attempts": r["attempts"],
            "ageSeconds": r["age"],
            "nextProbeSeconds": r["next_probe"],
            "intervalSeconds": r["interval"],
        }
        if any(
            q.get(k) and q[k] != row[field]
            for k, field in (
                ("bridge", "bridge"),
                ("interface", "interface"),
                ("vrf", "vrf"),
                ("neighbor", "ip"),
            )
        ):
            continue
        if q.get("family") in ("IPV4", "IPV6") and q["family"] != family:
            continue
        result.append(row)
    # Policy readback is always available, including an empty/disabled node.
    if not any(q.get(k) for k in ("bridge", "interface", "vrf", "neighbor", "family")):
        result.insert(0, {"recordType": "policy", **status})
    return result
