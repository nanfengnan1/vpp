"""Outer-table changes must preserve identity and restore both control planes."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
from adapter import Engine, empty, updated


class EncapVRFTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.api = Mock()
        self.api.snapshot.return_value = ({}, {}, {}, {"uplink": 9})
        self.links = [
            {"ifname": "lo", "ifindex": 1},
            {"ifname": "OUTER", "ifindex": 3,
             "linkinfo": {"info_kind": "vrf", "info_data": {"table": 100}}},
            {"ifname": "uplink", "ifindex": 4, "master": "OUTER"},
            {"ifname": "vx1", "ifindex": 5, "master": "br10",
             "linkinfo": {"info_kind": "vxlan", "info_data": {"id": 10100}}},
        ]
        self.writes = []
        def run(*args):
            if "-j" in args:
                return json.dumps(self.links)
            self.writes.append(args)
            self.links[-1]["linkinfo"]["info_data"]["link"] = args[-1]
            return ""
        self.engine = Engine(self.api, self.path, run=run)
        self.engine.model["vxlans"]["vx1"] = {
            "name": "vx1", "local": "192.0.2.1", "vni": 10100, "port": 4799}
        self.op = {"code": "VXLAN", "action": "SET",
                   "vxlan": {"name": "vx1", "encapVrfId": 100}}
        def call(method, **kw):
            if method == "ip_table_dump":
                return [NS(table=NS(table_id=t, is_ip6=f))
                        for t in (0, 100) for f in (False, True)]
            return NS(vrf_id=100)
        self.api.call.side_effect = call

    def test_partial_set_retains_identity_and_requires_existing_table(self):
        self.engine.validate(self.op)
        new = updated(self.engine.model, self.op)["vxlans"]["vx1"]
        self.assertEqual((new["local"], new["vni"], new["port"]),
                         ("192.0.2.1", 10100, 4799))
        for fields in ({"encapVrfId": 200}, {"encapVrfId": 254},
                       {"encapVrfId": True}, {"vni": 12}, {}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.engine.validate({**self.op, "vxlan": {"name": "vx1", **fields}})
        for family in ("2001:db8::1", "192.0.2.1"):
            self.engine.model["vxlans"]["vx1"]["local"] = family
            self.engine.validate(self.op)

    def test_explicit_underlay_must_match_both_linux_and_vpp_tables(self):
        op = copy.deepcopy(self.op)
        op["vxlan"]["underlay"] = "uplink"
        self.engine.validate(op)
        self.api.call.side_effect = lambda method, **kw: (
            [NS(table=NS(table_id=100, is_ip6=False))] if method == "ip_table_dump"
            else NS(vrf_id=0))
        with self.assertRaisesRegex(ValueError, "VPP underlay"):
            self.engine.validate(op)
        self.links[2].pop("master")
        with self.assertRaisesRegex(ValueError, "Linux underlay"):
            self.engine.validate(op)

    def test_legacy_undo_after_partial_failure_restores_default_and_reloads(self):
        undo = self.engine.request("prepare", self.op)
        def broken():
            self.engine.sync_underlay_mirrors()
            raise RuntimeError("native API connection lost")
        self.engine.reconcile = broken
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            self.engine.request("apply", self.op)
        self.assertEqual(self.links[-1]["linkinfo"]["info_data"]["link"], "OUTER")
        recovered = Engine(self.api, self.path, run=self.engine.run)
        self.assertTrue(recovered.paused)
        recovered.reconcile = recovered.sync_underlay_mirrors
        recovered.request("rollback", undo)
        self.assertEqual(self.links[-1]["linkinfo"]["info_data"]["link"], "lo")
        self.assertEqual(self.links[-1]["master"], "br10")
        restarted = Engine(self.api, self.path, run=self.engine.run)
        self.assertFalse(restarted.paused)
        self.assertEqual(restarted.model["vxlans"]["vx1"]["encapVrfId"], 0)
        self.assertEqual(json.loads(self.path.read_text())["version"], 4)

    def test_reload_nonzero_table_and_verify_drift(self):
        self.engine.model = updated(self.engine.model, self.op)
        self.engine.reconcile = self.engine.sync_underlay_mirrors
        self.engine.save()
        restored = Engine(self.api, self.path, run=self.engine.run)
        with self.assertRaisesRegex(RuntimeError, "differs"):
            restored.sync_underlay_mirrors(verify=True)
        restored.sync_underlay_mirrors()
        restored.sync_underlay_mirrors(verify=True)
        self.assertEqual(self.links[-1]["ifindex"], 5)
        self.assertEqual(len(self.writes), 1)

    def test_legacy_versions_remain_loadable(self):
        for version in (1, 2, 3):
            self.path.write_text(json.dumps({"version": version, "desired": empty()}))
            self.assertEqual(Engine(self.api, self.path).model, empty())


if __name__ == "__main__":
    unittest.main()
