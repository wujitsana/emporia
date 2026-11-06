"""Labs (W0): the sandbox a patch's code runs in — no network, no secrets, its own folder only.

The checker (`lab_check.py`) runs two things a patch can influence — the lab's tests and its
secret scanner over the patched tree — and runs them only through `run()` here. Nothing else in
Emporia runs a patch.

What it uses is what the machine allows an ordinary user, best first (`probe()` says which):

* **bwrap** (bubblewrap): new user, mount, network, PID and IPC namespaces; only the system's
  read-only folders, the interpreter and the work folder are mounted; no network at all.
* **landlock**: Linux's unprivileged sandbox (5.13+; ABI 4, 6.7+, for TCP), plus a seccomp
  filter, applied by a small launcher (`python -m emporia.lab_sandbox SPEC`) before it starts
  the command:
  - files: read-only the system's folders and the interpreter's; read-write the work folder
    only — not the person's home, not Hermes's folders, not the keys, not `/proc`;
  - network: TCP connect and bind refused (Landlock), and `socket()` refused outright by the
    seccomp filter (UDP, Unix sockets, netlink), `io_uring` too; `socketpair()` still works;
  - limits: CPU time, memory, file size, no core files; a new session, killed as a group at
    the wall-clock timeout;
  - an environment of its own: PATH, HOME and TMPDIR in the work folder, nothing inherited;
  - what it does **not** do: it shares the user and the kernel. It can still signal this user's
    other processes. Its result says `isolation: partial` and names these gaps.
  - with `unshare -rn` available too, it also runs in a network namespace of its own.
* **none**: a throwaway folder and the environment above, network turned away only by proxy
  settings. Not isolated, and said so: `isolation: none`. Used only when the person allows it
  (`--allow-unisolated` / EMPORIA_LAB_ALLOW_UNISOLATED=1); otherwise the checker refuses.

A container runtime (docker, podman) is noticed by `probe()` but not used in W0.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import resource
import shutil
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Read-only for the sandboxed command: the system's own folders. /etc only in part.
SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/opt/conda")
ETC_RO = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/localtime", "/etc/ssl",
          "/etc/alternatives", "/etc/passwd", "/etc/group", "/etc/nsswitch.conf", "/etc/mime.types",
          "/etc/python3", "/etc/gitconfig")
DEV_RW = ("/dev/null", "/dev/zero", "/dev/full")
DEV_RO = ("/dev/urandom", "/dev/random")

# Landlock (linux/landlock.h)
_LL_CREATE_RULESET, _LL_ADD_RULE, _LL_RESTRICT_SELF = 444, 445, 446
_LL_VERSION = 1
_RULE_PATH_BENEATH, _RULE_NET_PORT = 1, 2
FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_REMOVE_DIR, FS_REMOVE_FILE, FS_MAKE_CHAR, FS_MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
FS_MAKE_REG, FS_MAKE_SOCK, FS_MAKE_FIFO, FS_MAKE_BLOCK = 1 << 8, 1 << 9, 1 << 10, 1 << 11
FS_MAKE_SYM, FS_REFER, FS_TRUNCATE, FS_IOCTL_DEV = 1 << 12, 1 << 13, 1 << 14, 1 << 15
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
_FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV

# seccomp
_PR_SET_NO_NEW_PRIVS, _PR_SET_SECCOMP, _SECCOMP_MODE_FILTER = 38, 22, 2
_RET_ALLOW, _RET_ERRNO = 0x7FFF0000, 0x00050000
_EACCES, _ENOSYS, _EPERM = 13, 38, 1
_ARCH = {"x86_64": (0xC000003E, {"socket": 41, "io_uring_setup": 425}),
         "aarch64": (0xC00000B7, {"socket": 198, "io_uring_setup": 425})}

NETWORK_ENV = {k: "http://127.0.0.1:9" for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                                                   "ALL_PROXY", "all_proxy")} | {
    "NO_PROXY": "", "no_proxy": "", "PIP_NO_INDEX": "1", "UV_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0"}


class SandboxUnavailable(RuntimeError):
    pass


def search_path() -> str:
    """A PATH of the system's folders and the one git is in — never the person's whole PATH."""
    found = shutil.which("git")
    return ":".join(dict.fromkeys([*([os.path.dirname(found)] if found else []), "/usr/local/bin", "/usr/bin",
                                   "/bin"]))


# ── what this machine allows ──────────────────────────────────────────────────

def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def landlock_abi() -> int:
    """Landlock's ABI version here, 0 when it is not available."""
    if sys.platform != "linux":
        return 0
    try:
        r = _libc().syscall(_LL_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(_LL_VERSION))
    except (OSError, AttributeError):
        return 0
    return int(r) if r > 0 else 0


def _works(argv: list[str], timeout: float = 10) -> bool:
    exe = shutil.which(argv[0])
    if not exe:
        return False
    try:
        return subprocess.run([exe, *argv[1:]], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                              env={"PATH": search_path()}).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _bwrap_base(rw: list[str], ro: list[str]) -> list[str]:
    argv = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for p in ro:
        argv += ["--ro-bind-try", p, p]
    for p in rw:
        argv += ["--bind", p, p]
    return argv


def probe() -> dict[str, Any]:
    """What this machine allows an ordinary user, and the backend `run()` would use."""
    out: dict[str, Any] = {"platform": f"{sys.platform}/{platform.machine()}"}
    bw = shutil.which("bwrap")
    out["bwrap"] = bool(bw) and _works(_bwrap_base([], list(_ro_paths())) + ["true"])
    out["unshare_net"] = bool(shutil.which("unshare")) and _works(["unshare", "-rn", "true"])
    out["container_runtime"] = next((c for c in ("docker", "podman")
                                     if shutil.which(c) and _works([c, "info"], timeout=5)), None)
    out["landlock_abi"] = landlock_abi()
    out["seccomp"] = out["landlock_abi"] > 0 and platform.machine() in _ARCH
    out["backend"] = ("bwrap" if out["bwrap"] else
                      "landlock" if out["landlock_abi"] >= 1 and out["seccomp"] else "none")
    return out


# ── the paths a sandboxed command may read ────────────────────────────────────

def _prefixes(python: str) -> list[str]:
    """The interpreter's own folders: its venv (sys.prefix) and the Python it was made from."""
    real = Path(os.path.realpath(python))
    out = {str(real.parent.parent)}
    try:
        p = subprocess.run([python, "-I", "-c", "import sys; print(sys.prefix); print(sys.base_prefix)"],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                           env={"PATH": "/usr/bin:/bin"})
        out.update(line.strip() for line in p.stdout.splitlines() if line.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    out.add(str(Path(python).absolute().parent.parent))
    return sorted(out)


def _ro_paths(extra: list[str] | None = None) -> list[str]:
    return [p for p in (*SYSTEM_RO, *ETC_RO, *DEV_RO, *(extra or [])) if os.path.exists(p)]


# ── the launcher: applied inside the child, then exec ────────────────────────

def _seccomp_program(machine: str) -> bytes:
    arch, nr = _ARCH[machine]

    def ins(code: int, jt: int, jf: int, k: int) -> bytes:
        return struct.pack("HBBI", code, jt, jf, k)
    ld, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    prog = [ins(ld, 0, 0, 4), ins(jeq, 1, 0, arch), ins(ret, 0, 0, _RET_ERRNO | _EPERM),
            ins(ld, 0, 0, 0)]
    if machine == "x86_64":                       # the x32 ABI: refused
        prog += [ins(jge, 0, 1, 0x40000000), ins(ret, 0, 0, _RET_ERRNO | _EPERM)]
    prog += [ins(jeq, 0, 1, nr["socket"]), ins(ret, 0, 0, _RET_ERRNO | _EACCES),
             ins(jeq, 0, 1, nr["io_uring_setup"]), ins(ret, 0, 0, _RET_ERRNO | _ENOSYS),
             ins(ret, 0, 0, _RET_ALLOW)]
    return b"".join(prog)


class _Fprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _apply_seccomp(libc) -> None:
    machine = platform.machine()
    if machine not in _ARCH:
        raise SandboxUnavailable(f"no seccomp filter for {machine}")
    prog = _seccomp_program(machine)
    buf = ctypes.create_string_buffer(prog, len(prog))
    fp = _Fprog(len(prog) // 8, ctypes.addressof(buf))
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fp), 0, 0) != 0:
        raise SandboxUnavailable(f"seccomp: {os.strerror(ctypes.get_errno())}")


def _fs_rights(abi: int) -> int:
    rights = (1 << 13) - 1                         # ABI 1: EXECUTE … MAKE_SYM
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


def _apply_landlock(libc, abi: int, ro: list[str], rw: list[str], dev_rw: list[str]) -> None:
    handled = _fs_rights(abi)
    net = NET_BIND_TCP | NET_CONNECT_TCP if abi >= 4 else 0
    attr = struct.pack("QQ", handled, net) if abi >= 4 else struct.pack("Q", handled)
    abuf = ctypes.create_string_buffer(attr, len(attr))
    fd = libc.syscall(_LL_CREATE_RULESET, abuf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    if fd < 0:
        raise SandboxUnavailable(f"landlock: {os.strerror(ctypes.get_errno())}")
    read = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
    write = handled                                # all of it, inside the work folder

    def allow(path: str, rights: int) -> None:
        try:
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            return
        try:
            if not os.path.isdir(path):
                rights &= _FILE_RIGHTS & handled
            rule = struct.pack("=Qi", rights, pfd)
            rbuf = ctypes.create_string_buffer(rule, len(rule))
            if libc.syscall(_LL_ADD_RULE, ctypes.c_int(fd), ctypes.c_int(_RULE_PATH_BENEATH), rbuf,
                            ctypes.c_uint32(0)) != 0:
                raise SandboxUnavailable(f"landlock rule for {path}: {os.strerror(ctypes.get_errno())}")
        finally:
            os.close(pfd)
    for p in ro:
        allow(p, read)
    for p in dev_rw:
        allow(p, FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE)
    for p in rw:
        allow(p, write)
    # no network rules: with `net` handled, every TCP bind and connect is refused
    if libc.syscall(_LL_RESTRICT_SELF, ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
        raise SandboxUnavailable(f"landlock: {os.strerror(ctypes.get_errno())}")
    os.close(fd)


def _limits(spec: Mapping[str, Any]) -> None:
    cpu = int(spec.get("cpu_s") or 0)
    for res, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_CPU, cpu),
                       (resource.RLIMIT_FSIZE, int(spec.get("fsize_bytes") or 0)),
                       (resource.RLIMIT_AS, int(spec.get("memory_bytes") or 0))):
        if value:
            try:
                resource.setrlimit(res, (value, value))
            except (ValueError, OSError):
                pass


def launch(spec_path: str) -> None:
    """The launcher: read the spec, lock this process down, then become the command."""
    spec = json.loads(Path(spec_path).read_text())
    libc = _libc()
    _limits(spec)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SandboxUnavailable("no_new_privs refused")
    abi = landlock_abi()
    if abi < 1:
        raise SandboxUnavailable("landlock is not available here")
    _apply_landlock(libc, abi, spec["ro"], spec["rw"], spec.get("dev_rw", list(DEV_RW)))
    _apply_seccomp(libc)
    os.chdir(spec["cwd"])
    argv = spec["argv"]
    os.execvpe(argv[0], argv, spec["env"])


# ── run a command in the sandbox ──────────────────────────────────────────────

def sandbox_env(work: Path, python: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The command's whole environment: nothing of the person's, nothing secret."""
    home, tmp = work / "home", work / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    pybin = str(Path(python).absolute().parent)
    env = {"PATH": f"{pybin}:/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp),
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
           "PYTHONNOUSERSITE": "1", "EMPORIA_LAB_SANDBOX": "1", "EMPORIA_NO_DOTENV": "1",
           "XDG_CACHE_HOME": str(tmp), "XDG_CONFIG_HOME": str(home)}
    env.update(NETWORK_ENV)
    env.update(extra or {})
    return env


def describe(backend: str, info: Mapping[str, Any]) -> dict[str, Any]:
    """What a result says about where its commands ran — the part the checker signs."""
    if backend == "bwrap":
        return {"kind": "bwrap", "isolation": "isolated", "network": "none (own network namespace)",
                "filesystem": "system read-only, the work folder only", "gaps": ["shares the kernel"]}
    if backend == "landlock":
        gaps = ["same user: it can signal this user's other processes", "shares the kernel"]
        net = ("none: socket() refused (seccomp), TCP connect and bind refused (Landlock)"
               if info.get("landlock_abi", 0) >= 4 else "socket() refused (seccomp)")
        if info.get("unshare_net"):
            net += ", own network namespace"
        return {"kind": f"landlock-abi{info.get('landlock_abi')}+seccomp" + ("+netns" if info.get("unshare_net")
                                                                              else ""),
                "isolation": "partial", "network": net,
                "filesystem": "system and interpreter read-only; writes only its work folder; no home, keys or /proc",
                "gaps": gaps}
    return {"kind": "none", "isolation": "none", "network": "proxy settings only (not enforced)",
            "filesystem": "not restricted: it can read everything this user can",
            "gaps": ["NOT ISOLATED: same user, same files, same network"]}


def run(argv: list[str], *, cwd: Path, work: Path, python: str, env: Mapping[str, str],
        timeout_s: int, info: Mapping[str, Any] | None = None, allow_unisolated: bool = False,
        output: Path | None = None) -> dict[str, Any]:
    """Run `argv` in the best sandbox here: {exit_code, timed_out, seconds, output_tail, sandbox}.
    `work` is the one folder it may write (cwd is inside it). Raises SandboxUnavailable when no
    sandbox works and the person has not allowed an unisolated run."""
    info = dict(info or probe())
    backend = info.get("backend", "none")
    if backend == "none" and not allow_unisolated:
        raise SandboxUnavailable(
            "no sandbox works here (no bwrap, no Landlock): refused rather than run a patch unisolated — "
            "allow it with --allow-unisolated (EMPORIA_LAB_ALLOW_UNISOLATED=1) only on a machine that holds "
            "no keys or secrets")
    work = work.resolve()
    ro = _ro_paths(_prefixes(python))
    out_path = output or (work / "output.log")
    if backend == "bwrap":
        full = _bwrap_base([str(work)], ro)
        full[0] = shutil.which("bwrap") or "bwrap"
        for k, v in env.items():
            full += ["--setenv", k, v]
        full += ["--chdir", str(cwd), "--", *argv]
        run_env: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    elif backend == "landlock":
        spec = work / "sandbox-spec.json"
        spec.write_text(json.dumps({"argv": argv, "cwd": str(cwd), "env": dict(env), "ro": ro, "rw": [str(work)],
                                    "dev_rw": [p for p in DEV_RW if os.path.exists(p)], "cpu_s": timeout_s + 30,
                                    "fsize_bytes": 2 * 1024 ** 3, "memory_bytes": 8 * 1024 ** 3}))
        full = [sys.executable, "-I", "-m", "emporia.lab_sandbox", str(spec)]
        if info.get("unshare_net"):
            full = [shutil.which("unshare") or "unshare", "-rn", "--", *full]
        run_env = {"PATH": str(env["PATH"]) if "PATH" in env else search_path()}
    else:
        full = list(argv)
        run_env = dict(env)
    t0 = time.monotonic()
    timed_out = False
    with out_path.open("wb") as fh:
        proc = subprocess.Popen(full, cwd=str(cwd), env=run_env, stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)       # its own session: the command and its children
            except ProcessLookupError:
                pass
            code = proc.wait()
    data = out_path.read_bytes()[-64_000:]
    return {"exit_code": None if timed_out else code, "timed_out": timed_out,
            "seconds": round(time.monotonic() - t0, 1), "output_tail": data.decode("utf-8", "replace"),
            "sandbox": describe(backend, info)}


if __name__ == "__main__":       # the launcher, as `python -I -m emporia.lab_sandbox SPEC`
    try:
        launch(sys.argv[1])
    except (SandboxUnavailable, OSError, KeyError, ValueError) as exc:
        sys.stderr.write(f"emporia lab sandbox: {exc}\n")
        sys.exit(125)
