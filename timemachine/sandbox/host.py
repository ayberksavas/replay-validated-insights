"""Time-machine side of the sandbox.

SandboxHost starts the system under test as a separate, jailed Python process and feeds it the
timeline step by step. The host keeps everything the sandbox must not see: the full store, the clone,
the oracle and the API key. The sandbox gets:
- a copy of ONLY the predictor-side modules (no oracle, no evaluator, no TimeView, no ingest code),
- the events with visibility time <= T, appended to its own store as T advances,
- optionally a git repo with the code as of T (history up to T, nothing after),
- answers to diff()/file_at() for commits already released (the host refuses later ones),
- LLM calls through the host's gateway (cached, metered, budgeted); the sandbox has no network.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

from ..gitutil import run_git, require_repo
from ..predictor import Flag
from ..snapshot import IncrementalSnapshot
from ..timeline import Timeline

# The only modules that exist inside the sandbox.
SANDBOX_MODULES = ("__init__.py", "store.py", "predictor.py", "heuristics.py", "insight.py",
                   "kb/__init__.py", "kb/base.py", "kb/bm25.py", "kb/code.py", "kb/events.py", "kb/links.py",
                   "sandbox/__init__.py", "sandbox/jail.py", "sandbox/worker.py")
PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DEFAULT_IMAGE = "python:3.12-slim"
CONTAINER_WORKDIR = "/work"


class SandboxError(RuntimeError):
    pass


def _boot(pkg_dir):
    return f"import sys; sys.path.insert(0, {pkg_dir!r}); from timemachine.sandbox.worker import main; main()"


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_image(image=DEFAULT_IMAGE):
    if not docker_available():
        raise SandboxError("Docker is not running: start Docker Desktop (or the docker service) and retry")
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        subprocess.run(["docker", "pull", "-q", image], check=True, capture_output=True)


def docker_run_args(image, root, name, code=False):
    """Kernel-level isolation around the worker. Mounted from the host: only the predictor package
    (read-only) and, when enabled, the code-as-of-T folder (read-only). Everything else is cut off."""
    w = CONTAINER_WORKDIR
    args = ["docker", "run", "--rm", "-i", "--name", name,
            "--network", "none",                               # no network at all (the LLM goes through the host)
            "--read-only", "--tmpfs", f"{w}:rw,mode=1777,size=1g",   # only the work dir is writable, and it is RAM
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
            "--pids-limit", "128", "--memory", "2g",
            "-v", f"{os.path.join(root, 'pkg')}:{w}/pkg:ro", "-w", w]
    if code:
        args += ["-v", f"{os.path.join(root, 'code')}:{w}/code:ro"]
    return args + [image]


CONTAINER_CHECK = r"""
import json, os, socket
out = {}
def t(label, fn):
    try:
        fn(); out[label] = "ALLOWED"
    except Exception as e:
        out[label] = f"blocked ({type(e).__name__})"
t("DNS lookup of github.com", lambda: socket.getaddrinfo("github.com", 443))
t("TCP connect to 140.82.112.3:443 (GitHub)", lambda: socket.create_connection(("140.82.112.3", 443), timeout=3))
t("write outside the work dir (/etc)", lambda: open("/etc/tm_probe", "w"))
t("become root", lambda: os.setuid(0))
mounts = [l.split()[1] for l in open("/proc/self/mounts") if l.split()[1].startswith("/work/")]
out["host folders visible"] = ", ".join(mounts) + " (read-only)"
print(json.dumps(out))
"""


def container_check(image=DEFAULT_IMAGE, root=None) -> dict:
    """Run a probe in a container with the sandbox's docker flags but WITHOUT our Python jail, to show
    what the container blocks on its own."""
    root = root or tempfile.mkdtemp(prefix="tm_container_check_")
    os.makedirs(os.path.join(root, "pkg"), exist_ok=True)
    name = f"tm-container-check-{os.getpid()}"
    p = subprocess.run(docker_run_args(image, root, name) + ["python", "-I", "-c", CONTAINER_CHECK],
                       capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise SandboxError(f"container check failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout.strip().splitlines()[-1])


def install_package(root: str):
    for rel in SANDBOX_MODULES:
        dst = os.path.join(root, "pkg", "timemachine", *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(PKG_DIR, *rel.split("/")), dst)


def _inside(path, root):
    path, root = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return os.path.commonpath([path, root]) == root


class SandboxHost:
    def __init__(self, db_path, repo, predictor="kb_heuristic", options=None, root=None, llm=None,
                 code_snapshot=False, timeline: Timeline | None = None, runtime="process", image=DEFAULT_IMAGE):
        """runtime: "process" (local python -I + audit-hook jail) or "docker" (the same worker inside a
        container: no network, read-only root fs, no capabilities, unprivileged user, jail still on)."""
        require_repo(repo)
        self._repo, self._db_path = os.path.abspath(repo), os.path.abspath(db_path)
        self.root = os.path.abspath(root or tempfile.mkdtemp(prefix=f"tm_sandbox_{predictor}_"))
        for secret in (self._repo, self._db_path):
            if _inside(secret, self.root):
                raise ValueError(f"sandbox dir {self.root} would contain {secret}; pick a dir outside the data")
        os.makedirs(self.root, exist_ok=True)
        if os.listdir(self.root):
            raise FileExistsError(f"sandbox dir {self.root} is not empty (a sandbox only moves forward in time)")
        install_package(self.root)
        self.timeline = timeline.fork() if timeline is not None else Timeline(db_path)
        self.llm = llm
        self.stats = Counter()
        self.code = IncrementalSnapshot(self._repo, os.path.join(self.root, "code")) if code_snapshot else None
        self._log = open(os.path.join(self.root, "worker.log"), "wb")
        self.runtime, self.container = runtime, None
        if runtime == "docker":
            # the worker's store lives in the container's own tmpfs; the host folder only provides the
            # package (read-only) and, optionally, the code as of T (read-only)
            self.container = f"tm-sandbox-{predictor.replace('_', '-')}-{os.getpid()}-{id(self) % 100000}"
            pkg_dir = f"{CONTAINER_WORKDIR}/pkg"
            cmd = docker_run_args(image, self.root, self.container, code=code_snapshot) + ["python", "-I", "-B", "-c", _boot(pkg_dir)]
            env, cwd = None, None          # the docker CLI needs the user's env; nothing is forwarded into the container
        elif runtime == "process":
            cmd = [sys.executable, "-I", "-B", "-c", _boot(os.path.join(self.root, "pkg"))]
            env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR")}   # no tokens, no keys
            cwd = self.root
        else:
            raise ValueError(f"unknown sandbox runtime {runtime!r} (process | docker)")
        try:
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log)
        except FileNotFoundError as e:
            raise SandboxError(f"cannot start the {runtime} sandbox: {e}") from e
        info = self._call({"op": "init", "predictor": predictor, "options": options or {}})
        self.pid, self.predictor = info["pid"], info["predictor"]

    # ---- channel ----
    def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _call(self, msg):
        try:
            self._send(msg)
        except OSError as e:
            raise SandboxError(f"sandbox worker is gone ({e}); log: {self._log_tail()}") from e
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise SandboxError(f"sandbox worker exited; log: {self._log_tail()}")
            r = json.loads(line)
            if "rpc" in r:
                self._send(self._serve(r))
                continue
            if "error" in r:
                raise SandboxError(r["error"])
            return r["result"]

    def _log_tail(self):
        self._log.flush()
        try:
            with open(os.path.join(self.root, "worker.log"), "rb") as f:
                return f.read()[-1500:].decode("utf-8", "replace")
        except OSError:
            return "?"

    def _serve(self, r):
        """Requests from the sandbox. Every git access is checked against the cursor."""
        op = r["rpc"]
        try:
            if op in ("diff", "file_at"):
                sha = r["sha"]
                if sha not in self.timeline.released_commits:
                    self.stats["blocked_requests"] += 1
                    return {"error": f"LeakageError: commit {sha[:12]} is not visible at T={self.timeline.as_of}"}
                self.stats["git_" + op] += 1
                if op == "diff":
                    return {"result": run_git(self._repo, "show", "--format=", "-U3", "--no-color", sha)}
                return {"result": run_git(self._repo, "show", f"{sha}:{r['path']}")}
            if op == "llm":
                self.stats["llm_requests"] += 1
                if self.llm is None:
                    return {"result": {"text": None, "skipped": "no-llm"}}
                return {"result": self.llm.call(r["request"])}
        except Exception as e:  # noqa: BLE001 - report to the sandbox, keep the run going
            return {"error": f"{type(e).__name__}: {e}"}
        return {"error": f"unknown rpc {op!r}"}

    # ---- time machine API ----
    def advance(self, t: int, snapshot: bool = False) -> dict:
        events = self.timeline.advance(t)
        out = self._call({"op": "advance", "as_of": t, "events": events})
        self.stats["events_sent"] += len(events)
        self.stats["steps"] += 1
        if snapshot and self.code is not None:
            out["code_sha"] = self.code.update(t)
        return out

    def risky(self, shas):
        return [Flag(*f) for f in self._call({"op": "risky", "candidates": list(shas)})]

    def tests(self, shas):
        return [Flag(*f) for f in self._call({"op": "tests", "candidates": list(shas)})]

    def similar(self, issue: dict, k: int):
        q = {"number": issue["number"], "title": issue["title"], "body": issue.get("body") or ""}
        return [Flag(*f) for f in self._call({"op": "similar", "issue": q, "k": k})]

    def predict_change(self, task: dict, k: int) -> dict:
        """task = {"kind": "issue"|"pr", "number": N}: only a reference; the sandbox reads the words itself."""
        return self._call({"op": "change", "task": {"kind": task["kind"], "number": task["number"]}, "k": k})

    def worker_stats(self) -> dict:
        return self._call({"op": "stats"})

    def probe(self) -> dict:
        """Ask the sandbox to try to read the future in several ways; returns what happened."""
        nxt = self.timeline.peek_after()
        return self._call({"op": "probe", "db_path": self._db_path, "data_dir": os.path.dirname(self._db_path),
                           "repo": self._repo, "future_sha": nxt["row"][0] if nxt else None})

    def close(self):
        if self.proc.poll() is None:
            try:
                self._call({"op": "shutdown"})
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
                if self.container:
                    subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()
            except OSError:
                pass
        self._log.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
