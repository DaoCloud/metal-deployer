import io
import os
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from testkit.runners import inventory
from testkit.ssh import Remote, safe_extract, settings
from testkit.util import ConfigError


def archive(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tar.addfile(info)
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r:")


class SafeExtractTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "run1")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_regular_files(self):
        res = safe_extract(archive([("./raw", "dir", None), ("./raw/a.txt", "file", b"x"),
                                    ("./summary.json", "file", b"{}")]), self.root)
        self.assertTrue(res.ok)
        with open(os.path.join(self.root, "raw", "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"x")

    def test_symlink_refused(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside)
        res = safe_extract(archive([("lnk", "symlink", outside), ("lnk/owned", "file", b"x")]), self.root)
        self.assertFalse(res.ok)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(os.listdir(self.root), [])

    def test_paths_outside_refused(self):
        for name in ("../run10/x", "../../etc/x", "/tmp/abs"):
            res = safe_extract(archive([(name, "file", b"x")]), self.root)
            self.assertFalse(res.ok, name)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "run10")))


class SshPortTest(unittest.TestCase):

    def test_default_port(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings()["port"], 22)

    def test_port_from_env_reaches_ssh_and_inventory(self):
        host = SimpleNamespace(name="h1", ssh="10.0.0.1", mgmt_ip="10.1.0.1")
        with mock.patch.dict(os.environ, {"TESTKIT_SSH_PORT": "2022"}, clear=True):
            cfg = settings()
        argv = Remote(cfg).argv(host, "true")
        self.assertEqual(argv[argv.index("-p") + 1], "2022")
        self.assertIn("ansible_port=2022", inventory([SimpleNamespace(host=host)], cfg, "/k"))

    def test_invalid_port(self):
        for value in ("abc", "0", "70000", "-1"):
            with mock.patch.dict(os.environ, {"TESTKIT_SSH_PORT": value}, clear=True):
                with self.assertRaises(ConfigError, msg=value):
                    settings()


if __name__ == "__main__":
    unittest.main()


class PutTest(unittest.TestCase):
    """Remote.put on a path that several hosts write at once (shared node_results_dir)."""

    def test_concurrent_writers_never_expose_a_partial_file(self):
        import subprocess
        import threading
        from testkit.ssh import Result

        class Local(Remote):
            def __init__(self):
                pass

            def run(self, host, command, timeout=None, input=None):
                p = subprocess.run(["sh", "-c", command], input=input, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=timeout)
                return Result(p.returncode, p.stdout.decode(), p.stderr.decode())

        data = "".join("10.10.0.{}\n".format(i) for i in range(1, 200))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "share dir", "nodes.list")
            self.assertTrue(Local().put(None, path, data, "644").ok)
            seen, done = set(), threading.Event()

            def read():
                while not done.is_set():
                    with open(path) as f:
                        seen.add(f.read())
            reader = threading.Thread(target=read)
            reader.start()
            writers = [threading.Thread(target=lambda: [Local().put(None, path, data, "644") for _ in range(10)])
                       for _ in range(3)]
            for w in writers:
                w.start()
            for w in writers:
                w.join()
            done.set()
            reader.join()
            self.assertEqual(seen, {data})
            self.assertEqual(os.listdir(os.path.dirname(path)), ["nodes.list"])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
