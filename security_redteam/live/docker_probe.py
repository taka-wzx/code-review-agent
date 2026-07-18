"""Tiny non-destructive probes executed only inside the Phase 4 container."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Callable


def _status_value(name: str) -> str | None:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        if key == name:
            return value.strip()
    return None


def _cgroup_value(name: str) -> str:
    return (Path("/sys/fs/cgroup") / name).read_text(encoding="ascii").strip()


def _uid() -> tuple[bool, dict[str, object]]:
    uid = int(getattr(os, "getuid")())
    gid = int(getattr(os, "getgid")())
    return uid == 65532 and gid == 65532, {"uid": uid, "gid": gid}


def _root_read_only() -> tuple[bool, dict[str, object]]:
    target = Path("/w6-forbidden-root-write")
    try:
        target.write_text("forbidden", encoding="utf-8")
    except OSError as exc:
        return exc.errno == 30 and not target.exists(), {"errno": exc.errno}
    target.unlink(missing_ok=True)
    return False, {"errno": None}


def _network_none() -> tuple[bool, dict[str, object]]:
    interfaces = sorted(item.name for item in Path("/sys/class/net").iterdir())
    return interfaces == ["lo"], {"interfaces": interfaces}


def _capabilities() -> tuple[bool, dict[str, object]]:
    effective = _status_value("CapEff")
    return effective == "0000000000000000", {"cap_eff": effective}


def _no_new_privileges() -> tuple[bool, dict[str, object]]:
    value = _status_value("NoNewPrivs")
    return value == "1", {"no_new_privs": value}


def _pids() -> tuple[bool, dict[str, object]]:
    value = _cgroup_value("pids.max")
    passed = value.isdigit() and int(value) <= 128
    return passed, {"pids_max": value}


def _memory() -> tuple[bool, dict[str, object]]:
    value = _cgroup_value("memory.max")
    passed = value.isdigit() and int(value) <= 2 * 1024 * 1024 * 1024
    return passed, {"memory_max": value}


def _cpu() -> tuple[bool, dict[str, object]]:
    value = _cgroup_value("cpu.max")
    quota, period = value.split()
    passed = quota.isdigit() and period.isdigit() and int(quota) <= 2 * int(period)
    return passed, {"cpu_max": value}


def _tmpfs() -> tuple[bool, dict[str, object]]:
    matching = [
        line
        for line in Path("/proc/self/mountinfo")
        .read_text(encoding="utf-8")
        .splitlines()
        if " /tmp " in line
    ]
    joined = "\n".join(matching)
    passed = bool(matching) and "noexec" in joined and "nosuid" in joined
    return passed, {"mount_count": len(matching), "noexec": "noexec" in joined}


def _symlink_isolation() -> tuple[bool, dict[str, object]]:
    alias = Path("host-alias")
    alias.unlink(missing_ok=True)
    alias.symlink_to("/host-week6-canary")
    try:
        exists = alias.exists()
        readable = False
        try:
            alias.read_bytes()
            readable = True
        except OSError:
            pass
        return not exists and not readable, {"target_exists": exists, "readable": readable}
    finally:
        alias.unlink(missing_ok=True)


def _argv_inert() -> tuple[bool, dict[str, object]]:
    expected = "; touch /workspace/W6_INJECTED"
    supplied = sys.argv[2] if len(sys.argv) > 2 else None
    marker = Path("W6_INJECTED")
    return supplied == expected and not marker.exists(), {
        "argument_matched": supplied == expected,
        "marker_exists": marker.exists(),
    }


def _slow() -> tuple[bool, dict[str, object]]:
    time.sleep(120)
    return False, {"unexpected_completion": True}


PROBES: dict[str, Callable[[], tuple[bool, dict[str, object]]]] = {
    "W6-DK-01": _uid,
    "W6-DK-02": _root_read_only,
    "W6-DK-03": _network_none,
    "W6-DK-04": _capabilities,
    "W6-DK-05": _no_new_privileges,
    "W6-DK-06": _pids,
    "W6-DK-07": _memory,
    "W6-DK-08": _cpu,
    "W6-DK-09": _tmpfs,
    "W6-DK-10": _symlink_isolation,
    "W6-DK-11": _argv_inert,
    "W6-DK-12": _slow,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in PROBES:
        return 64
    case_id = sys.argv[1]
    try:
        passed, evidence = PROBES[case_id]()
        record = {"case_id": case_id, "passed": passed, "evidence": evidence}
    except Exception as exc:  # pragma: no cover - container-side evidence
        record = {"case_id": case_id, "passed": False, "error_type": type(exc).__name__}
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
