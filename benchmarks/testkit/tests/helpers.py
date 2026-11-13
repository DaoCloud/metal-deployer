import os
import tempfile

from testkit import scaffold


class Workspace:
    """Temporary directory with generated network.yaml / tests.yaml that tests can edit."""

    def __init__(self, racks=2, trays=18):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.network = os.path.join(self.dir, "network.yaml")
        self.tests = os.path.join(self.dir, "tests.yaml")
        self.write("network.yaml", scaffold.network_text(racks, trays))
        self.write("tests.yaml", scaffold.tests_text())

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def edit(self, name, old, new, count=1):
        text = self.read(name)
        if old not in text:
            raise AssertionError("{!r} not found in {}".format(old, name))
        self.write(name, text.replace(old, new, count))

    def close(self):
        self.tmp.cleanup()
