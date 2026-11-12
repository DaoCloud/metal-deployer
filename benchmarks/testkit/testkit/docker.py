"""Docker operations on remote hosts, run over ssh.

The executor only talks to an object with this interface, so tests can replace it
with an in-memory fake.
"""

import json
import shlex
import subprocess

from testkit.ssh import Remote, Result

q = shlex.quote


class DockerOps:

    def __init__(self, ssh_cfg, command_timeout=120):
        self.remote = Remote(ssh_cfg)
        self.timeout = command_timeout

    def _run(self, host, cmd, timeout=None, input=None):
        return self.remote.run(host, cmd, timeout=timeout or self.timeout, input=input)

    def ping(self, host):
        res = self._run(host, "docker version --format '{{.Server.Version}}'")
        return Result(res.rc, res.out.strip(), res.err) if res.ok else res

    def mkdir(self, host, path):
        return self._run(host, "mkdir -p {}".format(q(path)))

    def put(self, host, path, data, mode="600"):
        return self.remote.put(host, path, data, mode)

    def image_info(self, host, image):
        """(present, digest). digest is the first RepoDigest, or the image ID for local-only images."""
        res = self._run(host, "docker image inspect --format '{{.Id}}|{{json .RepoDigests}}' " + q(image))
        if not res.ok:
            return False, ""
        image_id, _, digests = res.out.strip().partition("|")
        try:
            repo = json.loads(digests or "[]")
        except ValueError:
            repo = []
        return True, repo[0] if repo else image_id

    def pull(self, host, image, timeout=3600):
        return self._run(host, "docker pull " + q(image), timeout=timeout)

    def push(self, host, image, timeout=3600):
        """Copy a local image to the host: docker save | ssh docker load."""
        try:
            save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
        except OSError as e:
            return Result(127, "", "local docker not available: {}".format(e))
        load = subprocess.Popen(self.remote.argv(host, "docker load"), stdin=save.stdout,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        save.stdout.close()
        try:
            out, err = load.communicate(timeout=timeout)
            save.wait(timeout=60)
        except subprocess.TimeoutExpired:
            load.kill()
            save.kill()
            return Result(124, "", "push timeout after {}s".format(timeout))
        if save.returncode != 0:
            return Result(save.returncode, "", "docker save failed: " + save.stderr.read().decode(errors="replace"))
        return Result(load.returncode, out.decode(errors="replace"), err.decode(errors="replace"))

    def containers(self, host, prefix):
        """Names of running containers whose name starts with prefix."""
        res = self._run(host, "docker ps --format '{{.Names}}'")
        if not res.ok:
            return None
        return [n for n in res.out.split() if n.startswith(prefix)]

    def run(self, host, args):
        """docker run -d <args>; returns Result with the container ID in out."""
        return self._run(host, "docker run -d " + " ".join(q(a) for a in args))

    def states(self, host, names):
        """{name: (status, exit_code)}; containers that do not exist are ("missing", None)."""
        out = {n: ("missing", None) for n in names}
        if not names:
            return out
        res = self._run(host, "docker inspect --format '{{.Name}} {{.State.Status}} {{.State.ExitCode}}' "
                        + " ".join(q(n) for n in names) + " 2>/dev/null")
        if res.rc == 124 or res.rc == 255:
            raise ConnectionError(res.message())
        for line in res.out.splitlines():
            parts = line.split()
            if len(parts) == 3:
                out[parts[0].lstrip("/")] = (parts[1], int(parts[2]))
        return out

    def exec_stream(self, host, name, command, env, log):
        """Start "docker exec" of a shell command in a running container; output goes to log (file)."""
        args = ["docker", "exec"]
        for key, value in env.items():
            args += ["-e", "{}={}".format(key, value)]
        args += [name, "sh", "-c", command]
        return self.remote.popen(host, " ".join(q(a) for a in args), stdout=log)

    def host_run(self, host, command, timeout=300):
        return self._run(host, command, timeout=timeout)

    def host_script(self, host, name, args, timeout=300):
        """Run node_scripts/<name> (python3) on the host; returns its JSON output. Raises NodeError."""
        from testkit.nodecmd import run_script
        return run_script(self.remote, host, name, args, timeout=timeout)

    def exec_run(self, host, name, command, timeout):
        return self._run(host, "docker exec {} sh -c {}".format(q(name), q(command)), timeout=timeout)

    def save_logs(self, host, name, path):
        return self._run(host, "docker logs {} > {} 2>&1".format(q(name), q(path)))

    def stop(self, host, names, grace=10):
        if not names:
            return Result(0)
        return self._run(host, "docker stop -t {} {} >/dev/null 2>&1; true".format(
            grace, " ".join(q(n) for n in names)), timeout=grace + 60)

    def remove(self, host, names):
        if not names:
            return Result(0)
        return self._run(host, "docker rm -f {} >/dev/null 2>&1; true".format(" ".join(q(n) for n in names)))

    def remove_by_label(self, host, label):
        return self._run(host, "ids=$(docker ps -aq --filter label={}); [ -z \"$ids\" ] || docker rm -f $ids "
                         ">/dev/null".format(q(label)))

    def fetch(self, host, remote_dir, local_dir):
        return self.remote.fetch(host, remote_dir, local_dir)

    def follow_logs(self, host, name):
        """Replace the current process output with docker logs -f (for testkit logs --follow)."""
        return subprocess.call(self.remote.argv(host, "docker logs -f " + q(name)))
