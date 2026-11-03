"""Samples a whole application's process tree over time: memory, handles, threads and CPU per role.

    python scripts/verify/proc_sampler.py <pid file> <minutes> <out.csv> [--json summary.json]

It follows the process id written to <pid file> (by the soak test, once the application is up) and
every descendant of it: Electron's main process, its renderers, GPU and utility processes, and the
engine it supervises. `scripts/soak.py` measures the engine alone; this measures what a person
actually runs.

The verdict says how long the run was and compares the start of the settled period with the end. A
short run does not show that a long one would pass, and the first lines of output say so.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
import time
from pathlib import Path

import psutil

SAMPLE_S = 15
WARMUP_S = 120  # start-up (model load, caches, first render) is not growth
WAIT_FOR_PID_S = 180
PLAN_MINUTES = 480
GROWTH_LIMIT = 0.10  # 10% between the start of the settled period and its end
ROLES = ("shell", "renderer", "gpu", "utility", "engine", "other")


def role_of(name: str, command: list[str]) -> str:
    """Which part of the application a process is, from its name and arguments."""
    lowered = name.lower()
    joined = " ".join(command).lower()
    if lowered.startswith("vigil-engine") or "vigil serve" in joined or " serve " in joined:
        return "engine"
    if "--type=renderer" in joined:
        return "renderer"
    if "--type=gpu-process" in joined:
        return "gpu"
    if "--type=utility" in joined or "--type=crashpad" in joined:
        return "utility"
    if lowered.startswith(("vigil-88", "electron")):
        return "shell"
    return "other"


def tree_of(pid: int) -> list[psutil.Process]:
    root = psutil.Process(pid)
    return [root, *root.children(recursive=True)]


def sample(pid: int, began: float) -> dict[str, float]:
    row: dict[str, float] = {"t_s": round(time.time() - began)}
    totals = dict.fromkeys(ROLES, 0.0)
    handles = dict.fromkeys(ROLES, 0.0)
    threads = dict.fromkeys(ROLES, 0.0)
    cpu = dict.fromkeys(ROLES, 0.0)
    count = 0
    for proc in tree_of(pid):
        try:
            with proc.oneshot():
                role = role_of(proc.name(), proc.cmdline())
                totals[role] += proc.memory_info().rss / 1_048_576
                handles[role] += proc.num_handles()
                threads[role] += proc.num_threads()
                cpu[role] += proc.cpu_percent(None)
            count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue  # a process that exited between listing and reading is not an error
    row["processes"] = float(count)
    for role in ROLES:
        row[f"rss_{role}_mb"] = totals[role]
        row[f"handles_{role}"] = handles[role]
        row[f"threads_{role}"] = threads[role]
        row[f"cpu_{role}"] = cpu[role]
    row["rss_total_mb"] = sum(totals.values())
    row["handles_total"] = sum(handles.values())
    row["threads_total"] = sum(threads.values())
    return row


def verdict(rows: list[dict[str, float]], minutes: float) -> tuple[list[str], bool]:
    """Human-readable lines, and whether memory and handles stayed bounded on this run."""
    out = [
        f"Ran {minutes:g} min of the {PLAN_MINUTES} the plan asks for. "
        f"Samples every {SAMPLE_S} s, whole process tree.",
        "A short run does not show that a long one would pass.",
    ]
    settled = [r for r in rows if r["t_s"] >= WARMUP_S]
    if len(settled) < 8:
        out.append(f"Too short to judge: {minutes:g} min leaves too few samples after the warm-up.")
        return out, False
    edge = max(4, len(settled) // 5)
    first, last = settled[:edge], settled[-edge:]
    bounded = True
    for label, key in (
        ("Total memory", "rss_total_mb"),
        ("Total handles", "handles_total"),
        ("Total threads", "threads_total"),
        *(
            (f"{role} memory", f"rss_{role}_mb")
            for role in ("shell", "renderer", "gpu", "engine")
            if any(r[f"rss_{role}_mb"] > 0 for r in rows)
        ),
    ):
        a = statistics.fmean(r[key] for r in first)
        b = statistics.fmean(r[key] for r in last)
        change = (b - a) / a if a else 0.0
        peak = max(r[key] for r in rows)
        unit = "MB" if key.startswith("rss") else ""
        out.append(
            f"{label}: {a:.0f}{unit} -> {b:.0f}{unit} ({change * 100:+.1f}%), peak {peak:.0f}"
        )
        if change > GROWTH_LIMIT and key != "threads_total":
            bounded = False
    fewest = min(r["processes"] for r in rows)
    most = max(r["processes"] for r in rows)
    out.append(f"Processes: {fewest:.0f} to {most:.0f}.")
    out.append(
        "Verdict on this run: memory and handles stayed bounded."
        if bounded
        else "Verdict on this run: NOT shown to be bounded; read the figures above."
    )
    return out, bounded


def main(argv: list[str]) -> int:
    pid_file, minutes, out_path = Path(argv[0]), float(argv[1]), Path(argv[2])
    summary_path = Path(argv[argv.index("--json") + 1]) if "--json" in argv else None
    deadline = time.time() + WAIT_FOR_PID_S
    while not pid_file.is_file() and time.time() < deadline:
        time.sleep(1)
    if not pid_file.is_file():
        print("The application never wrote its process id; nothing was measured.")
        return 2
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    began = time.time()
    rows: list[dict[str, float]] = []
    for proc in tree_of(pid):
        proc.cpu_percent(None)  # prime the CPU counters
    while time.time() - began < minutes * 60 + 30 and psutil.pid_exists(pid):
        time.sleep(SAMPLE_S)
        try:
            rows.append(sample(pid, began))
        except psutil.NoSuchProcess:
            break
    ended_early = psutil.pid_exists(pid) is False and time.time() - began < minutes * 60 - SAMPLE_S
    if rows:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    lines, bounded = verdict(rows, round((rows[-1]["t_s"] if rows else 0) / 60, 1))
    if ended_early:
        lines.append(
            "The application exited before the run was over; the figures end where it did."
        )
        bounded = False
    for line in lines:
        print(line)
    if summary_path is not None:
        summary_path.write_text(
            json.dumps({"lines": lines, "bounded": bounded, "samples": len(rows)}, indent=2),
            encoding="utf-8",
        )
    return 0 if bounded else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
