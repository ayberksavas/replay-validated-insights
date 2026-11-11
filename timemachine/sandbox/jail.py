"""In-process jail for the sandbox worker, built on CPython audit hooks (PEP 578).

Once installed it cannot be removed. It blocks, for all Python code in the worker:
- reading files outside the sandbox dir (the Python install stays readable, for imports),
- writing anywhere outside the sandbox dir, and opening SQLite files outside it,
- sockets / DNS / HTTP (the only way out is the host channel on stdin/stdout),
- starting processes (so no `git log --all` on some other clone), ctypes and the registry.

This is defense in depth. The main guarantee is the data feed: the sandbox store simply holds no
row after T, the oracle code is not copied in, and diff()/LLM calls are checked by the host.
Native code could bypass audit hooks, so a shell-capable agent (e.g. Claude Code) should run in a
container with no network and only the sandbox mounted; the worker protocol stays the same.
"""
import os
import sys

BLOCKED_PREFIXES = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
                    "os.forkpty", "os.startfile", "os.kill", "ctypes.", "winreg.", "_winapi.", "urllib.",
                    "http.client.", "ftplib.", "smtplib.", "poplib.", "imaplib.", "nntplib.", "telnetlib.",
                    "webbrowser.", "pty.")
READ_EVENTS = ("os.listdir", "os.scandir", "glob.glob", "pathlib.Path.glob", "pathlib.Path.rglob")
WRITE_EVENTS = ("os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.replace", "os.truncate", "os.chmod",
                "os.chown", "os.utime", "os.link", "os.symlink", "os.chdir", "os.chflags", "os.mkfifo", "os.mknod",
                "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move",
                "shutil.rmtree", "shutil.make_archive", "shutil.unpack_archive", "os.add_dll_directory")
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

_installed = None


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def install_jail(root: str) -> str:
    """Confine this process to `root`. Idempotent per process (a second call must use the same root)."""
    global _installed
    root = _norm(root)
    if _installed is not None:
        if _installed != root:
            raise RuntimeError("jail already installed for another root")
        return root
    read_roots = tuple(sorted({root} | {_norm(p) for p in (sys.base_prefix, sys.prefix, sys.exec_prefix,
                                                           os.path.dirname(sys.executable))}))
    write_roots = (root,)

    def inside(p, roots):
        if p is None or isinstance(p, int):
            return True                          # already-open file descriptors
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        try:
            p = os.fspath(p)
        except TypeError:
            return True
        if p in ("", ":memory:"):
            return True
        n = _norm(p)
        return any(n == r or n.startswith(r.rstrip(os.sep) + os.sep) for r in roots)

    def deny(event, args):
        raise PermissionError(f"sandbox: {event} blocked {args!r:.200}")

    def hook(event, args):
        if event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                      (isinstance(flags, int) and flags & WRITE_FLAGS)
            if not inside(path, write_roots if writing else read_roots):
                deny(event, args)
        elif event == "sqlite3.connect":
            if not inside(args[0] if args else None, write_roots):
                deny(event, args)
        elif event in READ_EVENTS:
            if not inside(args[0] if args else None, read_roots):
                deny(event, args)
        elif event in WRITE_EVENTS:
            for a in args:
                if isinstance(a, (str, bytes, os.PathLike)) and not inside(a, write_roots):
                    deny(event, args)
        elif event.startswith(BLOCKED_PREFIXES):
            deny(event, args)

    sys.addaudithook(hook)
    _installed = root
    return root
