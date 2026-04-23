#!/usr/bin/env python3
"""
cpu_timeline.py  –  CPU thread timeline profiler for DuckDB (no privileges needed)

Polls /proc/<pid>/task/*/stat at 5 ms intervals to track per-thread running/waiting
state, and captures DuckDB's built-in JSON profiling for operator-level annotations.
Outputs a JSON file that nsys_timeline.py --cpu-profile can consume for side-by-side
comparison with a GPU nsys timeline.

Usage:
    python3 cpu_timeline.py <duckdb_bin> <db_file> <sql_file> [output.json]
    python3 cpu_timeline.py --iterations 2 --poll-ms 5 <bin> <db> <sql> [out.json]

Environment:
    LD_LIBRARY_PATH   passed through to duckdb
    SIRIUS_CONFIG_FILE  NOT set (CPU-only run, no GPU)

The sql_file should contain plain SQL without gpu_execution() wrappers.
"""

import sys, os, json, time, subprocess, threading, glob, re, argparse, tempfile
from collections import defaultdict
from pathlib import Path

# ── thread state polling ───────────────────────────────────────────────────────

INTERESTING_STATES = {"R", "D"}   # Running, Disk-wait = "busy"

def _read_tasks(pid):
    """Return {tid: (name, state)} for all tasks of pid. Fast path: no exceptions."""
    result = {}
    try:
        for stat_path in glob.glob(f"/proc/{pid}/task/*/stat"):
            try:
                raw = Path(stat_path).read_text()
                # Format: pid (name) state ...
                m = re.match(r"\d+ \((.+?)\) (\S)", raw)
                if m:
                    tid = int(stat_path.split("/")[-2])
                    result[tid] = (m.group(1), m.group(2))
            except OSError:
                pass
    except OSError:
        pass
    return result


def poll_proc(pid, interval_ms, stop_event, out):
    """Background thread: append (t_ms, {tid: (name, state)}) to out."""
    t0 = time.monotonic()
    interval_s = interval_ms / 1000.0
    while not stop_event.is_set():
        t_ms = (time.monotonic() - t0) * 1000
        tasks = _read_tasks(pid)
        if tasks:
            out.append((round(t_ms, 1), tasks))
        time.sleep(interval_s)


# ── DuckDB operator profiling ──────────────────────────────────────────────────

PROFILE_PRAGMA = """\
PRAGMA enable_profiling='json';
PRAGMA profiling_output='{path}';
"""

def flatten_operators(node, depth=0):
    """Walk DuckDB profiling JSON tree → list of {name, ms, depth}."""
    ops = []
    name = node.get("operator_type", node.get("name", "?"))
    # DuckDB uses "operator_timing" (wall seconds per op) in newer versions
    t_s  = (node.get("operator_timing") or node.get("timing") or 0)
    t_ms = t_s * 1000
    ops.append({"name": name, "ms": round(t_ms, 1), "depth": depth})
    for child in node.get("children", []):
        ops.extend(flatten_operators(child, depth + 1))
    return ops


# ── main runner ───────────────────────────────────────────────────────────────

def run(duckdb_bin, db_file, sql_file, n_iters, poll_ms, with_profiling=False):
    sql_text = Path(sql_file).read_text()

    iters = []

    for it in range(n_iters):
        # Temp file for DuckDB JSON profiling output
        prof_fd, prof_path = tempfile.mkstemp(suffix=".json", prefix="duckdb_prof_")
        os.close(prof_fd)

        # Build SQL: prepend profiling pragmas only when --with-profiling requested
        if with_profiling:
            full_sql = PROFILE_PRAGMA.format(path=prof_path) + "\n" + sql_text
        else:
            full_sql = sql_text
            os.unlink(prof_path)  # won't be written, clean up

        env = os.environ.copy()
        # Disable Sirius GPU extension so we get clean CPU-only timing.
        # SIRIUS_DISABLE=1 suppresses GPU initialization without changing query results.
        env["SIRIUS_DISABLE"] = "1"
        env.pop("SIRIUS_CONFIG_FILE", None)

        poll_data = []
        stop_ev   = threading.Event()

        t_wall_start = time.monotonic()

        proc = subprocess.Popen(
            [duckdb_bin, db_file, "-unsigned", "-noheader"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Start poller right after process begins
        poller = threading.Thread(target=poll_proc,
                                  args=(proc.pid, poll_ms, stop_ev, poll_data),
                                  daemon=True)
        poller.start()

        stdout, stderr = proc.communicate(input=full_sql.encode())
        t_wall_end   = time.monotonic()
        wall_ms      = (t_wall_end - t_wall_start) * 1000

        stop_ev.set()
        poller.join(timeout=1)

        # Parse DuckDB JSON profile only if profiling was enabled
        operators = []
        if with_profiling:
            try:
                raw = Path(prof_path).read_text().strip()
                if raw:
                    profiles = [json.loads(chunk) for chunk in
                                re.split(r'(?<=\})\s*(?=\{)', raw) if chunk.strip()]
                    if profiles:
                        operators = flatten_operators(profiles[-1])
            except Exception:
                pass
            finally:
                try: os.unlink(prof_path)
                except: pass

        # Build per-thread timeline: {tid: [(t_ms, state), ...]}
        thread_timeline = defaultdict(list)
        thread_names    = {}
        for t_ms, tasks in poll_data:
            for tid, (name, state) in tasks.items():
                thread_timeline[tid].append((t_ms, state))
                thread_names[tid] = name

        # Collapse to run segments: [(start_ms, end_ms, state)]
        thread_segments = {}
        for tid, events in thread_timeline.items():
            segs = []
            if not events:
                continue
            seg_start, seg_state = events[0]
            for t_ms, state in events[1:]:
                if state != seg_state:
                    segs.append({"s": seg_start, "e": t_ms, "st": seg_state})
                    seg_start, seg_state = t_ms, state
            segs.append({"s": seg_start, "e": wall_ms, "st": seg_state})
            thread_segments[tid] = segs

        iters.append({
            "iter":        it,
            "wall_ms":     round(wall_ms, 1),
            "threads":     {str(tid): {
                                "name": thread_names[tid],
                                "segs": thread_segments[tid]
                            } for tid in sorted(thread_timeline)},
            "operators":   operators,
            "stdout":      stdout.decode(errors="replace")[:2000],
        })

        print(f"  iter {it}: {wall_ms:.0f} ms  "
              f"({len(thread_timeline)} threads, {len(operators)} operators)",
              flush=True)

    return iters


# ── output ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("duckdb_bin")
    ap.add_argument("db_file")
    ap.add_argument("sql_file")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--iterations", "-n", type=int, default=2)
    ap.add_argument("--poll-ms", type=float, default=5.0)
    ap.add_argument("--with-profiling", action="store_true",
                    help="Enable DuckDB JSON profiling for operator annotations "
                         "(adds ~20%% overhead — wall times will be inflated)")
    args = ap.parse_args()

    out_path = args.output or (Path(args.sql_file).stem + "_cpu_timeline.json")

    print(f"CPU thread profiler")
    print(f"  binary:   {args.duckdb_bin}")
    print(f"  db:       {args.db_file}")
    print(f"  sql:      {args.sql_file}")
    print(f"  iters:    {args.iterations}  poll: {args.poll_ms} ms", flush=True)

    iters = run(
        duckdb_bin    = args.duckdb_bin,
        db_file       = args.db_file,
        sql_file      = args.sql_file,
        n_iters       = args.iterations,
        poll_ms       = args.poll_ms,
        with_profiling= args.with_profiling,
    )

    payload = {
        "tool":    "cpu_timeline.py",
        "db":      args.db_file,
        "sql":     args.sql_file,
        "poll_ms": args.poll_ms,
        "iters":   iters,
    }

    Path(out_path).write_text(json.dumps(payload, separators=(",", ":")),
                              encoding="utf-8")
    size_kb = Path(out_path).stat().st_size // 1024
    print(f"Written: {out_path}  ({size_kb} KB)")


if __name__ == "__main__":
    main()
