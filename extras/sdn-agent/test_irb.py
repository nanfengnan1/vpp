"""Tenant isolation, selection and journal migration regression tests."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from adapter import empty, normalized, updated
from irb import IRBEngine, external, expand_hops


class IRBTests(unittest.TestCase):
    def test_unknown_query_scope_is_an_error_not_empty_success(self):
        for scope in (
            {"vrf": "missing"},
            {"bridge": "missing"},
            {"interface": "missing"},
        ):
            with self.subTest(scope=scope), self.assertRaisesRegex(
                ValueError, "unknown"
            ):
                self.engine.query({"kind": "INTERFACES", **scope})

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = Mock()
        self.api.snapshot.return_value = ({}, {}, {}, {})
        self.api.call.return_value = []
        self.engine = IRBEngine(
            self.api, Path(self.tmp.name) / "state.json", run=Mock(return_value="[]")
        )

    def test_v1_undo_is_upgraded_without_losing_l2_objects(self):
        old = {"bridges": {"b1": {"name": "b1"}}, "vxlans": {}, "ports": {}}
        model = normalized(old)
        self.assertEqual(model["bridges"], old["bridges"])
        self.assertEqual(model["tenants"], {})
        self.engine.reconcile = Mock()
        self.engine.request("rollback", old)
        self.assertEqual(self.engine.model, model)

    def test_ipv6_link_readback_does_not_increment_enable_references(self):
        self.api.call.return_value = Mock(ip="fe80::1")
        self.assertTrue(self.engine.ip6_enabled(10))
        self.api.call.assert_called_once_with(
            "sw_interface_ip6_get_link_local_address", sw_if_index=10
        )
        self.api.call.side_effect = RuntimeError(
            "sw_interface_ip6_get_link_local_address: VPP retval -62"
        )
        self.assertFalse(self.engine.ip6_enabled(10))
        self.api.call.side_effect = RuntimeError("API disconnected")
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            self.engine.ip6_enabled(10)

    def test_address_delete_does_not_delete_other_gateway_addresses(self):
        model = empty()
        for prefix in ("10.0.0.1/24", "2001:db8::1/64"):
            model = updated(
                model,
                {
                    "code": "ADDRESS",
                    "action": "CREATE",
                    "address": {"interface": "br10", "prefix": prefix},
                },
            )
        model = updated(
            model,
            {
                "code": "ADDRESS",
                "action": "DELETE",
                "address": {"interface": "br10", "prefix": "10.0.0.1/24"},
            },
        )
        self.assertEqual(list(model["addresses"]), ["br10|2001:db8::1/64"])

    def test_table_collision_and_cross_tenant_route_are_rejected(self):
        self.engine.model["tenants"] = {
            "blue": {"tableId": 1001},
            "red": {"tableId": 1002},
        }
        self.engine.model["bridges"] = {"br10": {}}
        self.engine.model["bindings"] = {"br10": {"tenant": "blue"}}
        with self.assertRaisesRegex(ValueError, "already owned"):
            self.engine.validate(
                {
                    "code": "TENANT",
                    "action": "CREATE",
                    "tenant": {"name": "green", "tableId": 1001},
                }
            )
        with self.assertRaisesRegex(ValueError, "bound to that tenant"):
            self.engine.validate(
                {
                    "code": "STATIC_ROUTE",
                    "action": "CREATE",
                    "route": {
                        "table": 1002,
                        "interface": "br10",
                        "prefix": "10.1.0.0/16",
                    },
                }
            )

    def test_l3vni_requires_its_own_tenant_binding(self):
        self.engine.model["vxlans"] = {"vx50000": {"vni": 50000}}
        self.engine.model["ports"] = {"vx50000": {"bridge": "br50000"}}
        self.engine.model["bindings"] = {"br50000": {"tenant": "blue"}}
        with self.assertRaisesRegex(ValueError, "bound to this tenant"):
            self.engine.validate(
                {
                    "code": "TENANT_L3VNI",
                    "action": "BIND",
                    "l3vni": {"tenant": "red", "vni": 50000},
                }
            )

    def test_selected_kernel_multipath_retains_each_remote_and_weight(self):
        self.engine.model["tenants"] = {"blue": {"tableId": 1001}}
        self.engine.model["bindings"] = {"b1": {"tenant": "blue"}}
        self.engine.run.return_value = json.dumps(
            [
                {
                    "dst": "10.1.0.0/24",
                    "protocol": "bgp",
                    "nexthops": [
                        {"dev": "b1", "gateway": "192.0.2.1", "weight": 1},
                        {"dev": "b1", "gateway": "192.0.2.2", "weight": 2},
                    ],
                }
            ]
        )
        rows = self.engine.kernel_routes(1001, False, {"b1": 12})
        self.assertEqual([p["weight"] for p in rows["10.1.0.0/24"]["paths"]], [1, 2])
        self.assertEqual(
            rows["10.1.0.0/24"]["paths"][1]["nh"]["address"]["ip4"], "192.0.2.2"
        )

    def test_null_iproute_external_flag_is_recognized(self):
        self.assertTrue(external({"extern_learn": None}))
        self.assertTrue(external({"flags": ["extern_learn"]}))
        self.assertFalse(external({"state": ["REACHABLE"]}))

    def test_nexthop_group_expansion_and_cycle_rejection(self):
        objects = {
            1: {"id": 1, "group": [{"id": 2, "weight": 3}]},
            2: {"id": 2, "dev": "br1", "gateway": "192.0.2.2"},
        }
        self.assertEqual(expand_hops({"nhid": 1}, objects.__getitem__)[0]["weight"], 3)
        objects[2] = {"id": 2, "group": [{"id": 1}]}
        with self.assertRaisesRegex(RuntimeError, "cyclic"):
            expand_hops({"nhid": 1}, objects.__getitem__)

    def test_ipv6_prefix_keeps_ipv4_evpn_next_hop(self):
        self.engine.model["tenants"] = {"blue": {"tableId": 1001}}
        self.engine.model["bindings"] = {"b1": {"tenant": "blue"}}
        self.engine.run.return_value = json.dumps(
            [
                {
                    "dst": "2001:db8::/64",
                    "protocol": "bgp",
                    "dev": "b1",
                    "via": {"family": "inet", "host": "192.0.2.1"},
                }
            ]
        )
        path = self.engine.kernel_routes(1001, True, {"b1": 12})["2001:db8::/64"][
            "paths"
        ][0]
        self.assertEqual(path["proto"], 0)
        self.assertEqual(path["nh"]["address"], {"ip4": "192.0.2.1"})

    def test_mirror_tc_preserves_carrier_and_accepts_iproute_header(self):
        self.engine.model["ports"] = {"vx10": {"bridge": "br10"}}
        self.engine.run.side_effect = [json.dumps([{"kind": "clsact"}])] + [
            json.dumps(
                [
                    {"kind": "matchall"},
                    {
                        "kind": "matchall",
                        "options": {
                            "actions": [
                                {"kind": "gact", "control_action": {"type": "drop"}}
                            ]
                        },
                    },
                ]
            )
        ] * 2
        self.engine.isolate_mirrors()
        self.assertEqual(self.engine.run.call_count, 3)

    def test_failed_tc_collision_rollback_does_not_remove_foreign_filter(self):
        self.engine.previous = {**empty(), "ports": {"access0": {"bridge": "br1"}}}
        self.engine.run.return_value = json.dumps(
            [
                {
                    "kind": "matchall",
                    "options": {
                        "handle": 1,
                        "actions": [
                            {"kind": "gact", "control_action": {"type": "pass"}}
                        ],
                    },
                }
            ]
        )
        self.engine.release_mirrors()
        self.assertEqual(self.engine.run.call_count, 2)
        self.assertTrue(
            all("del" not in call.args for call in self.engine.run.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
