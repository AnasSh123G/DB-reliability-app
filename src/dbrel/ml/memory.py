"""What a training process really uses, and handing freed memory back.

Swap on the training machine is zram -- compressed RAM -- so memory pushed to
swap still occupies RAM while the process's resident size (VmRSS) falls. On
2026-09-24 a VmRSS-only log line read 5-9 GB while the bake-off held 35 GB and
was about to be OOM-killed. Every figure here therefore counts swap too.

The cgroup figures are only reported when the process runs in its own
`dbrel ml launch` unit: anywhere else the group is shared (the editor's scope,
a terminal's), and its peak would describe other programs.

Standard library only: the TabM scripts import this from another virtualenv.
"""

from __future__ import annotations

from pathlib import Path

GIB = 2**30
UNIT_PREFIX = "dbrel-ml-"
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _status_bytes(field: str, status: str) -> int:
    for line in status.splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1]) * 1024  # the kernel reports kB
    return 0


def own_cgroup(proc_cgroup: str | None = None) -> str | None:
    """This process's cgroup v2 path, e.g. /user.slice/.../dbrel-ml-x.service."""
    if proc_cgroup is None:
        try:
            proc_cgroup = Path("/proc/self/cgroup").read_text()
        except OSError:
            return None
    for line in proc_cgroup.splitlines():
        if line.startswith("0::"):
            return line[3:].strip()
    return None


def launched_unit(cgroup: str | None) -> str | None:
    """The `dbrel ml launch` unit name if `cgroup` is one, else None."""
    if not cgroup:
        return None
    last = cgroup.rstrip("/").rsplit("/", 1)[-1]
    if last.startswith(UNIT_PREFIX) and last.endswith(".service"):
        return last
    return None


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return None if text == "max" else int(text)


def memory_report(proc_cgroup: str | None = None) -> dict:
    """Bytes: resident, swapped, their total, and the launch unit's current/peak/swap."""
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        status = ""
    rss = _status_bytes("VmRSS", status)
    swap = _status_bytes("VmSwap", status)
    report = {"rss": rss, "swap": swap, "total": rss + swap,
              "unit": None, "group_current": None, "group_peak": None, "group_swap": None}
    cgroup = own_cgroup(proc_cgroup)
    unit = launched_unit(cgroup)
    if unit is not None:
        directory = CGROUP_ROOT / cgroup.lstrip("/")
        report.update(
            unit=unit,
            group_current=_read_int(directory / "memory.current"),
            group_peak=_read_int(directory / "memory.peak"),
            group_swap=_read_int(directory / "memory.swap.current"),
        )
    return report


def format_memory(report: dict | None = None) -> str:
    r = report if report is not None else memory_report()
    text = f"rss {r['rss'] / GIB:.1f} + swap {r['swap'] / GIB:.1f} = {r['total'] / GIB:.1f} GiB"
    if r["group_peak"] is not None:
        text += f", group peak {r['group_peak'] / GIB:.1f} GiB"
    return text


def release_memory() -> None:
    """Return memory Python and glibc are holding on to after large objects were freed.

    The model libraries allocate large native buffers per fit (CatBoost's pools,
    LightGBM's binned datasets). gc returns them to glibc, which keeps the
    pages; malloc_trim hands them back to the OS. This frees memory that is no
    longer referenced -- it is not evidence that nothing leaks; `memory_report`
    across trials is.
    """
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def inside_launched_unit() -> bool:
    return launched_unit(own_cgroup()) is not None
