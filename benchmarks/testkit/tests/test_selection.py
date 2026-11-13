import unittest

from testkit.config import load_both
from testkit.items import BY_ID
from testkit.selection import SelectionError, build_groups, resolve
from tests.helpers import Workspace


class SelectionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace()
        cls.ws.edit("tests.yaml", "hosts: {}", "hosts:\n  gb300-r1-t01: {tags: [batch1]}\n"
                                               "  gb300-r1-t02: {skip: [moe], tags: [batch1]}")
        cls.cluster, _, problems = load_both(cls.ws.network, cls.ws.tests)
        assert problems.ok, problems.errors

    @classmethod
    def tearDownClass(cls):
        cls.ws.close()

    def names(self, hosts):
        return [h.name for h in hosts]

    def test_resolve(self):
        self.assertEqual(len(resolve(self.cluster, "all")), 36)
        self.assertEqual(len(resolve(self.cluster, None)), 36)
        self.assertEqual(len(resolve(self.cluster, "R2")), 18)
        self.assertEqual(self.names(resolve(self.cluster, "tag:batch1")), ["gb300-r1-t01", "gb300-r1-t02"])
        mixed = resolve(self.cluster, ["gb300-r2-t03", "gb300-r1-t05", "gb300-r2-t03"])
        self.assertEqual(self.names(mixed), ["gb300-r1-t05", "gb300-r2-t03"])
        self.assertEqual(len(resolve(self.cluster, "R1,gb300-r2-t01")), 19)

    def test_resolve_errors(self):
        for bad in ("R9", "tag:none", "gb300-x", ["R1", ["R2"]], 5):
            with self.assertRaises(SelectionError):
                resolve(self.cluster, bad)

    def groups(self, item, cfg, **kw):
        groups, notes = build_groups(self.cluster, BY_ID[item], cfg, **kw)
        return groups, notes

    def test_scope_node(self):
        groups, _ = self.groups("dcgm", {"scope": "node", "hosts": "R1"})
        self.assertEqual(len(groups), 18)
        self.assertEqual(groups[0].id, "gb300-r1-t01")
        self.assertEqual([g.index for g in groups[:3]], [0, 1, 2])

    def test_scope_rack_with_skip(self):
        groups, notes = self.groups("moe.rack", {"scope": "rack", "hosts": "all"})
        self.assertEqual([g.id for g in groups], ["R1", "R2"])
        self.assertEqual(len(groups[0].hosts), 17)
        self.assertNotIn("gb300-r1-t02", self.names(groups[0].hosts))
        self.assertTrue(any("gb300-r1-t02 skips moe.rack" in n for n in notes))

    def test_scope_group(self):
        groups, _ = self.groups("superbench.all", {"scope": "group", "hosts": "all"})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].id, "R1+R2")
        self.assertEqual(groups[0].master.name, "gb300-r1-t01")
        partial, _ = self.groups("superbench.pair", {"scope": "group", "hosts": ["gb300-r1-t01", "gb300-r1-t03"]})
        self.assertEqual(partial[0].id, "g1")

    def test_explicit_groups(self):
        cfg = {"scope": "group", "groups": [["R1"], ["R2"], ["gb300-r1-t02"]]}
        groups, notes = self.groups("moe.cross_rack", cfg)
        self.assertEqual([g.id for g in groups], ["g1", "R2"])
        self.assertEqual(len(groups[0].hosts), 17)
        self.assertTrue(any("group g3 has no hosts left" in n for n in notes))

    def test_only_and_exclude(self):
        groups, _ = self.groups("moe.single", {"scope": "node", "hosts": "all"}, only="R2",
                                exclude="gb300-r2-t05,gb300-r2-t06")
        self.assertEqual(len(groups), 16)
        groups, _ = self.groups("moe.single", {"scope": "node", "hosts": "R1"}, only="R2")
        self.assertEqual(groups, [])

    def test_bad_scope(self):
        with self.assertRaises(SelectionError):
            self.groups("dcgm", {"scope": "pod"})


if __name__ == "__main__":
    unittest.main()
