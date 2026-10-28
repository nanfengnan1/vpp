"""Policy lifecycle and publication safety; real packets are covered by the lab."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from adapter import empty, normalized, updated
from irb import IRBEngine
import keepalive


class KeepaliveTests(unittest.TestCase):
    def test_owned_withdrawal_survives_kernel_flush_but_preserves_new_owner(self):
        mac, ip = "02:00:00:10:00:02", "192.168.20.12"
        for row in (
            None,
            {"lladdr": "02:00:00:10:00:99"},
            {"lladdr": mac, "extern_learn": True},
        ):
            with self.subTest(row=row), TemporaryDirectory() as tmp:
                kernel = [] if row is None else [{"dev": "br20", "dst": ip, **row}]
                api = Mock()
                api.call.return_value = []
                run = Mock(return_value=json.dumps(kernel))
                engine = IRBEngine(api, Path(tmp) / "state.json", run=run)
                engine.published_neighbors = {("br20", ip): mac}
                engine.sync_neighbors({"br20": 10})
                writes = [c.args for c in run.call_args_list if "-j" not in c.args]
                if row is None:
                    self.assertEqual(len(writes), 2)
                    self.assertEqual(writes[0][-2:], ("nud", "failed"))
                    self.assertEqual(
                        writes[1], ("ip", "neigh", "del", ip, "dev", "br20")
                    )
                else:
                    self.assertEqual(writes, [])
                self.assertEqual(engine.published_neighbors, {})

    def test_unchanged_mirror_mac_does_not_flush_linux_neighbors(self):
        with TemporaryDirectory() as tmp:
            mac = "02:00:00:00:14:01"
            api = Mock()
            api.snapshot.return_value = (
                {10: {"tag": "sdn-agent/bvi/br20", "l2_address": mac}},
                {},
                {},
                {},
            )
            api.call.side_effect = lambda method, **kw: (
                SimpleNamespace(vrf_id=0) if method == "sw_interface_get_table" else []
            )
            run = Mock(
                return_value=json.dumps(
                    [{"ifname": "br20", "address": mac, "mtu": 1500}]
                )
            )
            engine = IRBEngine(api, Path(tmp) / "state.json", run=run)
            engine.model["bridges"] = {"br20": {"mac": mac}}
            engine.ip6_enabled = Mock(return_value=True)
            engine.ensure_bvis()
            self.assertEqual([c for c in run.call_args_list if "set" in c.args], [])
            run.return_value = json.dumps(
                [{"ifname": "br20", "address": "02:00:00:00:99:99", "mtu": 1500}]
            )
            engine.ensure_bvis()
            run.assert_called_with("ip", "link", "set", "dev", "br20", "address", mac)

    def test_default_migration_and_explicit_disable(self):
        self.assertFalse(keepalive.enabled(normalized({})))
        on = updated(
            empty(),
            {
                "code": "NEIGHBOR_KEEPALIVE",
                "action": "SET",
                "neighborKeepalive": {"enabled": True},
            },
        )
        off = updated(
            on,
            {
                "code": "NEIGHBOR_KEEPALIVE",
                "action": "SET",
                "neighborKeepalive": {"enabled": False},
            },
        )
        self.assertTrue(keepalive.enabled(on))
        self.assertFalse(keepalive.enabled(off))
        for value in (None, 0, "false"):
            with self.assertRaises(ValueError):
                updated(
                    on,
                    {
                        "code": "NEIGHBOR_KEEPALIVE",
                        "action": "SET",
                        "neighborKeepalive": {"enabled": value},
                    },
                )

    def test_persistent_policy_and_rollback_readback(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            api = Mock()
            api.call.return_value = SimpleNamespace(enabled=True)
            engine = IRBEngine(api, path)
            engine.model["neighborKeepalive"] = {"enabled": True}
            engine.save()
            restored = IRBEngine(api, path)
            self.assertTrue(keepalive.enabled(restored.model))
            self.assertEqual(json.loads(path.read_text())["version"], 3)
            restored.verify(
                restored.rollback_expectation({"code": "NEIGHBOR_KEEPALIVE"})
            )
            api.call.return_value = SimpleNamespace(enabled=False)
            with self.assertRaises(RuntimeError):
                restored.verify({"code": "NEIGHBOR_KEEPALIVE"})

    def test_minimum_age_accounts_for_fdb_granularity(self):
        model = empty()
        model["neighborKeepalive"] = {"enabled": True}
        for age in (0, 120, 300):
            model["bridges"] = {"br10": {"ageingTime": age}}
            keepalive.validate(model)
        model["bridges"]["br10"]["ageingTime"] = 60
        with self.assertRaises(ValueError):
            keepalive.validate(model)

    def test_scope_never_enrolls_vxlan_or_unmanaged_ports(self):
        engine = Mock()
        engine.model = empty()
        engine.model["neighborKeepalive"] = {"enabled": True}
        engine.model["ports"] = {
            "access0": {"bridge": "br10"},
            "vx10": {"bridge": "br10"},
        }
        engine.model["vxlans"] = {"vx10": {}}
        engine.api.snapshot.return_value = (
            {},
            {},
            {},
            {"access0": 7, "vx10": 8, "unmanaged": 9},
        )
        engine.api.call.side_effect = lambda method, **kw: (
            SimpleNamespace(enabled=True)
            if method == "neighbor_keepalive_status"
            else []
        )
        keepalive.reconcile(engine, {"br10": 100})
        changes = [
            c
            for c in engine.api.call.call_args_list
            if c.args[0] == "neighbor_keepalive_scope_add_del"
        ]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kwargs, {"is_add": True, "bvi": 100, "port": 7})

    def test_failed_bindings_are_scoped_per_ip_not_per_mac(self):
        engine = Mock()
        engine.model = {"neighborKeepalive": {"enabled": True}}
        engine.api.call.return_value = [
            SimpleNamespace(
                bvi=10, address="192.0.2.1", mac="02:00:00:00:00:01", state=2
            ),
            SimpleNamespace(
                bvi=10, address="2001:db8::1", mac="02:00:00:00:00:01", state=0
            ),
        ]
        self.assertEqual(
            keepalive.failed(engine), {(10, "192.0.2.1", "02:00:00:00:00:01")}
        )
        engine.model["neighborKeepalive"]["enabled"] = False
        self.assertEqual(keepalive.failed(engine), set())

    def test_failed_ip_is_not_republished_and_frr_gets_failed_before_delete(self):
        with TemporaryDirectory() as tmp:
            mac = "02:00:00:10:00:02"
            ips = ("192.168.20.12", "2001:db8:20::12")
            kernel = [
                {"dev": "br20", "dst": ip, "lladdr": mac, "state": ["PERMANENT"]}
                for ip in ips
            ]
            api = Mock()

            def call(method, **args):
                if method == "neighbor_keepalive_dump":
                    return [SimpleNamespace(bvi=10, address=ips[0], mac=mac, state=2)]
                if method == "ip_neighbor_dump":
                    return [
                        SimpleNamespace(
                            neighbor={
                                "sw_if_index": 10,
                                "ip_address": ips[args["af"]],
                                "mac_address": mac,
                                "flags": 2,
                            }
                        )
                    ]
                return []

            api.call.side_effect = call
            run = Mock(return_value=json.dumps(kernel))
            engine = IRBEngine(api, Path(tmp) / "state.json", run=run)
            engine.model["neighborKeepalive"] = {"enabled": True}
            engine.local = {("br20", mac): "access0"}
            engine.sync_neighbors({"br20": 10})
            self.assertEqual(engine.published_neighbors, {("br20", ips[1]): mac})
            writes = [c.args for c in run.call_args_list if "-j" not in c.args]
            self.assertEqual(
                writes,
                [
                    (
                        "ip",
                        "neigh",
                        "replace",
                        ips[0],
                        "lladdr",
                        mac,
                        "dev",
                        "br20",
                        "nud",
                        "failed",
                    ),
                    ("ip", "neigh", "del", ips[0], "dev", "br20"),
                ],
            )
