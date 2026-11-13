import csv
import os
import unittest

from testkit.config import load_cluster
from testkit.plan_import import read_rows, replace_hosts, run_import, to_hosts
from testkit.util import Problems, load_yaml
from tests.helpers import Workspace

HEAD = ["rack", "tray", "node_no", "sn", "hostname", "lom_ip", "lom_gateway", "bf3_mgt_ip", "bf3_mgt_gateway",
        "bf3_mgt_pci", "bf3_mgt_mac", "bf3_storage_ip", "bf3_storage_gateway", "bf3_storage_routes",
        "bf3_storage_pci", "ib_rail1_pci", "ib_rail2_pci", "ib_rail3_pci", "ib_rail4_pci", "ib_rail1_guid", "ib_mtu",
        "remark", "colour"]


def row(rack, tray, pci=True):
    n = (rack - 1) * 18 + tray
    return [rack, tray, n, "SN{}".format(n), "gb300-r{}-t{:02d}".format(rack, tray), "10.252.4{}.{}".format(rack, 10 + tray),
            "10.252.4{}.1".format(rack), "10.60.0.{}/24".format(10 + n), "10.60.0.1",
            "0009:01:00.0" if pci else "", "AA-BB-CC-00-00-{:02d}".format(n), "10.61.0.{}/24".format(10 + n), "10.61.0.1",
            "10.61.1.0/24", "0009:01:00.1" if pci else "", "0000:03:00.0", "0001:03:00.0", "0002:03:00.0",
            "0003:03:00.0", "0xb83f", "4092", "", "red"]


class ImportTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(1, 1)
        self.plan = os.path.join(self.ws.dir, "plan.csv")
        self.write([row(1, 1), row(1, 2), row(2, 1, pci=False)])
        self.out = []

    def tearDown(self):
        self.ws.close()

    def write(self, rows):
        with open(self.plan, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(HEAD)
            w.writerows(rows)

    def test_to_hosts(self):
        hosts, unknown = to_hosts(read_rows(self.plan))
        h = hosts[0]
        self.assertEqual(h["name"], "gb300-r1-t01")
        self.assertEqual((h["rack"], h["tray"], h["serial"], h["ssh"]), ("R1", 1, "SN1", "10.252.41.11"))
        self.assertEqual(h["storage_routes"], ["10.61.1.0/24"])
        self.assertEqual(h["ib_pci"], ["0000:03:00.0", "0001:03:00.0", "0002:03:00.0", "0003:03:00.0"])
        self.assertEqual(h["ib_guid"], ["0xb83f", "", "", ""])
        self.assertEqual(h["ib_mtu"], 4092)
        self.assertNotIn("ib", h)
        self.assertEqual(unknown, ["colour"])

    def test_hardware_neutral_columns(self):
        with open(self.plan, "w", newline="") as f:
            f.write("hostname,rack,tray,lom_ip,mgmt_ip,mgmt_pci,storage_ip,rail1_pci,rail2_pci,rail1_ip,rail2_ip\n"
                    "b300-01,1,1,10.0.0.11,10.1.0.11/24,0005:02:00.0,10.2.0.11/24,0000:03:00.0,0001:03:00.0,"
                    "10.100.0.11/24,10.101.0.11/24\n")
        hosts, unknown = to_hosts(read_rows(self.plan))
        self.assertEqual(unknown, [])
        h = hosts[0]
        self.assertEqual((h["mgmt"], h["mgmt_pci"], h["storage"]), ("10.1.0.11/24", "0005:02:00.0", "10.2.0.11/24"))
        self.assertEqual(h["ib_pci"], ["0000:03:00.0", "0001:03:00.0"])
        self.assertEqual(h["ib"], ["10.100.0.11/24", "10.101.0.11/24"])

    def test_into_existing_cluster_keeps_the_rest(self):
        self.ws.edit("cluster.yaml", "tags: [golden]", "tags: [golden, batch1]\n    env: {A: '1'}")
        self.ws.edit("cluster.yaml", "  - name: gb300-r1-t01", "  - name: gb300-r1-t01\n    gpus: 4")
        before = self.ws.read("cluster.yaml")
        self.assertEqual(run_import(self.plan, self.ws.cluster, out=self.out.append), 0, self.out)
        text = self.ws.read("cluster.yaml")
        self.assertEqual(text[:text.index("hosts:")], before[:before.index("hosts:")])
        self.assertTrue(os.path.isfile(self.ws.cluster + ".bak"))
        data = load_yaml(self.ws.cluster)
        self.assertEqual(data["hosts"][0]["tags"], ["golden", "batch1"])
        self.assertEqual(data["hosts"][0]["env"], {"A": "1"})
        problems = Problems()
        cluster = load_cluster(self.ws.cluster, problems)
        self.assertEqual(problems.errors, [])
        h = cluster.by_name["gb300-r1-t02"]
        self.assertEqual(h.plan["mgmt_mac"], "aa:bb:cc:00:00:02")
        self.assertEqual(h.plan["storage_gateway"], "10.61.0.1")
        self.assertTrue(any("lack mgmt_pci" in w and "gb300-r2-t01" in w for w in problems.warnings))
        self.assertTrue(any("mgmt_pci     empty for 1 hosts" in l for l in self.out))

    def test_new_cluster_and_dry_run(self):
        path = os.path.join(self.ws.dir, "new.yaml")
        self.assertEqual(run_import(self.plan, path, dry_run=True, out=self.out.append), 0)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(run_import(self.plan, path, out=self.out.append), 0)
        data = load_yaml(path)
        self.assertEqual(data["network"]["mgmt"], {"default_route": True})
        self.assertEqual(data["platform"], "gb300")
        self.assertNotIn("profile", data["network"])
        self.assertNotIn("dns", data["network"]["mgmt"])
        self.assertEqual(data["hosts"][0]["tags"], ["golden"])

    def test_bad_plan_is_not_written(self):
        bad = row(1, 1)
        bad[HEAD.index("bf3_mgt_pci")] = "pci-zero"
        self.write([bad])
        before = self.ws.read("cluster.yaml")
        self.assertEqual(run_import(self.plan, self.ws.cluster, out=self.out.append), 1)
        self.assertEqual(self.ws.read("cluster.yaml"), before)
        self.assertTrue(any("not a PCI address" in l for l in self.out))

    def test_xlsx(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "hosts"
        ws.append(HEAD)
        ws.append(row(1, 1))
        wb.create_sheet("说明").append(["x"])
        path = os.path.join(self.ws.dir, "plan.xlsx")
        wb.save(path)
        hosts, _ = to_hosts(read_rows(path))
        self.assertEqual(hosts[0]["tray"], 1)
        self.assertEqual(hosts[0]["mgmt_pci"], "0009:01:00.0")

    def test_replace_hosts_keeps_later_sections(self):
        text = "ssh: {}\nhosts:\n  - name: a\nother: 1\n"
        self.assertEqual(replace_hosts(text, "hosts:\n  - name: b\n"), "ssh: {}\nhosts:\n  - name: b\n\nother: 1\n")


if __name__ == "__main__":
    unittest.main()
