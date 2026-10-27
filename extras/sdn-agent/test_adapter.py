"""Logic tests. These tests do not start VPP or change Linux networking."""

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import adapter


class AdapterTests(unittest.TestCase):
    def test_ipv6_source_only_create_uses_unspecified_ipv6_destination(self):
        self.engine.model["vxlans"]["vx6"] = {
            "name": "vx6",
            "local": "2001:db8::1",
            "vni": 106,
        }
        self.engine.run = Mock(return_value="[]")

        def call(method, **kwargs):
            if method == "vxlan_add_del_tunnel_v3":
                self.assertEqual(kwargs["dst_address"], "::")
                return SimpleNamespace(sw_if_index=99)
            if method == "vxlan_mp_remote_v2_dump":
                return []
            return SimpleNamespace(retval=0)

        self.api.call.side_effect = call
        self.engine.reconcile()
        self.assertTrue(
            any(
                c.args[0] == "vxlan_add_del_tunnel_v3"
                for c in self.api.call.call_args_list
            )
        )

    def test_ipv6_membership_rejects_wrong_family_and_canonicalizes_addresses(self):
        self.engine.model["vxlans"]["vx6"] = {
            "name": "vx6",
            "local": "2001:db8::1",
            "vni": 106,
        }
        self.engine.model["ports"]["vx6"] = {"bridge": "br6"}
        self.engine.run = Mock(
            return_value=json.dumps(
                [
                    {"ifname": "vx6", "mac": adapter.ZERO, "dst": dst}
                    for dst in (
                        "2001:db8:0:0::2",
                        "2001:db8::1",
                        "192.0.2.2",
                        "ff02::1",
                    )
                ]
            )
        )
        self.assertEqual(
            self.engine.remote(), {("vx6", "2001:db8::2"): {"bum": True, "macs": set()}}
        )

    def test_vxlan_query_combines_native_identity_family_and_remote_filters(self):
        self.engine.model["bindings"]["br6"] = {"tenant": "BLUE"}
        self.engine.model["ports"]["vx6"] = {"bridge": "br6"}
        self.api.snapshot.return_value = (
            {7: {"tag": "sdn-agent/vx/vx6", "interface_name": "vxlan_tunnel9"}},
            {},
            {
                7: {
                    "src_address": "2001:db8::1",
                    "dst_address": "::",
                    "dst_port": 4789,
                    "vni": 106,
                }
            },
            {},
        )
        self.api.call.return_value = [
            {"remote": "2001:db8::2", "mac": adapter.ZERO, "bum": True},
            {"remote": "2001:db8::2", "mac": "02:00:00:00:00:02", "bum": False},
        ]
        q = {
            "kind": "VXLAN_TUNNELS",
            "family": "IPV6",
            "interface": "vx6",
            "vrf": "BLUE",
            "bridge": "br6",
            "neighbor": "2001:db8:0:0::2",
        }
        (row,) = self.engine.query(q)
        self.assertEqual(
            (row["vppName"], row["remoteCount"], row["bumCount"]),
            ("vxlan_tunnel9", 1, 1),
        )
        self.assertTrue(row["multipoint"])
        self.assertEqual(self.engine.query({**q, "family": "IPV4"}), [])
        self.assertEqual(self.engine.query({**q, "neighbor": "2001:db8::3"}), [])
        self.assertEqual(self.engine.query({**q, "vrf": "RED"}), [])
        self.assertEqual(len(self.engine.query({**q, "interface": "vxlan_tunnel9"})), 1)

    def test_source_only_interface_can_exist_before_bridge_binding(self):
        self.engine.model["vxlans"]["vx100"] = {
            "name": "vx100",
            "local": "192.0.2.1",
            "vni": 10100,
        }
        self.engine.run = Mock(return_value="[]")

        def call(method, **kwargs):
            if method == "l2_interface_feat_flags_set":
                self.fail("L2 flags cannot be changed before bridge binding")
            if method == "vxlan_add_del_tunnel_v3":
                self.assertEqual(kwargs["dst_address"], "0.0.0.0")
                return SimpleNamespace(sw_if_index=99)
            if method == "vxlan_mp_remote_v2_dump":
                return []
            return SimpleNamespace(retval=0)

        self.api.call.side_effect = call
        self.engine.reconcile()
        self.assertEqual(self.engine.cycles, 1)
        self.assertTrue(
            any(
                c.args[0] == "vxlan_add_del_tunnel_v3"
                for c in self.api.call.call_args_list
            )
        )

    def test_multipoint_mac_move_installs_before_old_withdrawal(self):
        mac = "02:00:00:00:00:01"
        self.api.call.return_value = [
            SimpleNamespace(mac=mac, remote="192.0.2.2", bum=False)
        ]
        selected = self.engine.sync_remotes(
            7,
            "vx100",
            {
                ("vx100", "192.0.2.3"): {"bum": False, "macs": {mac}},
            },
        )
        self.assertEqual(selected, {mac})
        updates = self.api.call.call_args_list[1:]
        self.assertEqual([c.kwargs["is_add"] for c in updates], [True, False])
        self.assertEqual(
            [c.kwargs["remote"] for c in updates], ["192.0.2.3", "192.0.2.2"]
        )

    def test_multipoint_type3_withdrawal_preserves_type2(self):
        mac = "02:00:00:00:00:01"
        self.api.call.return_value = [
            SimpleNamespace(mac=mac, remote="192.0.2.2", bum=False),
            SimpleNamespace(mac=adapter.ZERO, remote="192.0.2.2", bum=True),
        ]
        self.engine.sync_remotes(
            7,
            "vx100",
            {
                ("vx100", "192.0.2.2"): {"bum": False, "macs": {mac}},
            },
        )
        update = self.api.call.call_args_list[1:]
        self.assertEqual(len(update), 1)
        self.assertTrue(update[0].kwargs["bum"])
        self.assertFalse(update[0].kwargs["is_add"])

    def test_multipoint_reconcile_uses_native_readback_after_restart(self):
        self.api.call.return_value = [
            SimpleNamespace(mac=adapter.ZERO, remote="192.0.2.2", bum=True)
        ]
        self.engine.sync_remotes(
            7,
            "vx100",
            {
                ("vx100", "192.0.2.2"): {"bum": True, "macs": set()},
                ("unmanaged", "192.0.2.9"): {"bum": True, "macs": set()},
            },
        )
        self.assertEqual(self.api.call.call_count, 1)

    def test_multipoint_rejects_ambiguous_selected_mac(self):
        mac = "02:00:00:00:00:01"
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            self.engine.sync_remotes(
                7,
                "vx100",
                {
                    ("vx100", "192.0.2.2"): {"bum": False, "macs": {mac}},
                    ("vx100", "192.0.2.3"): {"bum": False, "macs": {mac}},
                },
            )
        self.api.call.assert_not_called()

    def test_managed_bd_ids_fit_vpp_24_bit_namespace(self):
        for name in ("br100", "rollbackbd", "bridge-long-name", "br0"):
            self.assertGreater(adapter.bd_id(name), 0)
            self.assertLessEqual(adapter.bd_id(name), 0xFFFFFF)

    def test_empty_lcp_dump_terminates_when_cursor_stays_zero(self):
        vpp = object.__new__(adapter.VPP)
        vpp.call = Mock(side_effect=[[], [], [], (SimpleNamespace(cursor=0), [])])
        self.assertEqual(vpp.snapshot(), ({}, {}, {}, {}))
        self.assertEqual(vpp.call.call_count, 4)

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.api = Mock()
        self.api.snapshot.return_value = ({}, {}, {}, {})
        self.engine = adapter.Engine(self.api, self.path)

    def configured(self):
        self.engine.model = {
            "bridges": {"br100": {"name": "br100"}},
            "vxlans": {"vx100": {"name": "vx100", "vni": 10100, "local": "192.0.2.1"}},
            "ports": {"vx100": {"bridge": "br100", "port": "vx100"}},
        }

    def test_remote_membership_and_mac_references_are_independent(self):
        self.configured()
        rows = [
            {"ifname": "vx100", "dst": "192.0.2.2", "mac": adapter.ZERO},
            {"ifname": "vx100", "dst": "192.0.2.2", "mac": "02:00:00:00:00:02"},
            {
                "ifname": "vx100",
                "master": "br100",
                "mac": "02:00:00:00:00:02",
                "flags": ["extern_learn"],
            },
            {"dev": "unmanaged", "dst": "192.0.2.3", "mac": adapter.ZERO},
        ]
        self.engine.run = lambda *args: json.dumps(rows)
        remote = self.engine.remote()
        self.assertEqual(len(remote), 1)
        self.assertTrue(remote[("vx100", "192.0.2.2")]["bum"])
        rows.pop(0)
        remote = self.engine.remote()
        self.assertFalse(remote[("vx100", "192.0.2.2")]["bum"])
        self.assertEqual(remote[("vx100", "192.0.2.2")]["macs"], {"02:00:00:00:00:02"})
        rows[1]["ifname"] = "access0"  # MAC moved local; stale VXLAN self row remains.
        self.assertEqual(self.engine.remote(), {})

    def test_unmanaged_bd_collision_rejected_before_mutation(self):
        self.api.snapshot.return_value = (
            {},
            {adapter.bd_id("br100"): {"bd_tag": "someone-else"}},
            {},
            {},
        )
        with self.assertRaisesRegex(ValueError, "collision"):
            self.engine.validate(
                {"code": "BD", "action": "CREATE", "bridge": {"name": "br100"}}
            )
        self.api.call.assert_not_called()

    def test_partial_apply_persists_recovery_barrier(self):
        self.engine.reconcile = Mock(side_effect=RuntimeError("API lost after write"))
        target = adapter.updated(
            adapter.empty(),
            {"code": "BD", "action": "CREATE", "bridge": {"name": "br100"}},
        )
        with self.assertRaisesRegex(RuntimeError, "API lost"):
            self.engine.set_model(target)
        recovered = adapter.Engine(self.api, self.path)
        self.assertTrue(recovered.paused)
        self.assertEqual(recovered.previous, adapter.empty())
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            recovered.request(
                "prepare",
                {"code": "BD", "action": "CREATE", "bridge": {"name": "br200"}},
            )

    def test_rollback_restores_saved_model_and_clears_barrier(self):
        self.engine.model = {
            "bridges": {"br100": {"name": "br100"}},
            "ports": {},
            "vxlans": {},
        }
        self.engine.reconcile = Mock()
        self.engine.request("rollback", adapter.empty())
        recovered = adapter.Engine(self.api, self.path)
        self.assertFalse(recovered.paused)
        self.assertEqual(recovered.model, adapter.empty())

    def test_config_delete_does_not_keep_historical_creation(self):
        model = adapter.updated(
            adapter.empty(),
            {"code": "BD", "action": "CREATE", "bridge": {"name": "br100"}},
        )
        result = adapter.updated(
            model, {"code": "BD", "action": "DELETE", "bridge": {"name": "br100"}}
        )
        self.assertEqual(result, adapter.empty())
        self.assertIn("br100", model["bridges"])

    def test_remote_mac_never_reannounced_as_local(self):
        self.configured()
        self.engine.model["ports"]["access0"] = {"bridge": "br100", "port": "access0"}

        def call(method, **args):
            return [
                SimpleNamespace(
                    sw_if_index=200,
                    mac="02:00:00:00:00:22",
                    bvi_mac=False,
                    filter_mac=False,
                )
            ]

        self.api.call.side_effect = call
        self.engine.run = Mock(return_value="[]")
        self.engine.sync_local({"access0": 10})
        self.assertEqual(self.engine.local, {})
        self.assertEqual(self.engine.run.call_count, 1)

    def test_missing_pair_and_svi_are_not_silent_success(self):
        self.configured()
        for op in [
            {
                "code": "BD_PORT",
                "action": "BIND",
                "bridgePort": {"bridge": "br100", "port": "missing"},
            },
            {
                "code": "ADDRESS",
                "action": "CREATE",
                "address": {"interface": "br100", "prefix": "10.0.0.1/24"},
            },
        ]:
            with self.assertRaises(ValueError):
                self.engine.validate(op)

    def test_existing_owned_bd_is_reconciled_without_recreating_it(self):
        self.engine.model["bridges"]["br100"] = {"name": "br100"}
        self.api.snapshot.return_value = (
            {},
            {
                adapter.bd_id("br100"): {
                    "bd_tag": "sdn-agent/bd/br100",
                    "mac_age": 5,
                    "sw_if_details": [],
                }
            },
            {},
            {},
        )
        self.api.call.return_value = []
        self.engine.run = Mock(return_value="[]")
        self.engine.reconcile()
        self.assertNotIn(
            "bridge_domain_add_del_v2",
            [c.args[0] for c in self.api.call.call_args_list],
        )

    def test_restart_removes_stale_local_publication_without_deleting_remote(self):
        self.configured()
        self.engine.model["ports"]["access0"] = {"bridge": "br100", "port": "access0"}
        self.api.call.return_value = []
        self.engine.run = Mock(
            return_value=json.dumps(
                [
                    {
                        "ifname": "access0",
                        "mac": "02:00:00:00:00:01",
                        "master": "br100",
                        "flags": ["extern_learn"],
                        "state": "",
                    },
                    {
                        "ifname": "vx100",
                        "mac": "02:00:00:00:00:02",
                        "master": "br100",
                        "flags": ["extern_learn"],
                        "state": "",
                    },
                ]
            )
        )
        self.engine.sync_local({"access0": 10})
        mutations = [c.args for c in self.engine.run.call_args_list if "del" in c.args]
        self.assertEqual(len(mutations), 1)
        self.assertIn("02:00:00:00:00:01", mutations[0])

    def test_unchanged_local_mac_yields_to_selected_remote_move(self):
        self.configured()
        self.engine.model["ports"]["access0"] = {"bridge": "br100", "port": "access0"}
        mac = "02:00:00:00:00:01"
        self.engine.local = {("br100", mac): "access0"}
        self.api.call.return_value = [
            SimpleNamespace(sw_if_index=10, mac=mac, bvi_mac=False, filter_mac=False)
        ]
        self.engine.run = Mock(
            return_value=json.dumps(
                [
                    {
                        "ifname": "vx100",
                        "master": "br100",
                        "mac": mac,
                        "flags": ["extern_learn"],
                    }
                ]
            )
        )
        self.engine.sync_local({"access0": 10})
        self.engine.run.assert_called_once_with("bridge", "-j", "fdb", "show")


if __name__ == "__main__":
    unittest.main()
