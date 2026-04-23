# gpu-perf-tools

Profiling tools and interactive HTML timelines for Sirius GPU engine analysis.

## Tools

| Script | Purpose |
|--------|---------|
| `nsys_timeline.py` | Generates a self-contained interactive HTML timeline from an nsys SQLite export |
| `cpu_timeline.py` | Polls `/proc/<pid>/task/*/stat` for CPU thread states; feeds into `nsys_timeline.py` for side-by-side comparison |
| `capture.sh` | One-command wrapper: runs `nsys profile` + sqlite export + CPU profiling + HTML generation, writes into an organized profile directory |
| `build_index.py` | Scans `profiles/` subdirectories and generates `profiles/index.html` (GitHub Pages listing) |

---

## Quick start: capture a new profile

```bash
# From the sirius repo root:
cd /home/dnguyen56/sirius

bash /home/dnguyen56/gpu-perf-tools/capture.sh \
    --sf 100 \
    --sql test/tpch_performance/tpch_queries/gpu/q1.sql \
    --label q1 \
    --cpu-sql test/tpch_performance/tpch_queries/orig/q1.sql

# For all 22 queries:
bash /home/dnguyen56/gpu-perf-tools/capture.sh \
    --sf 100 \
    --sql /tmp/all22_gpu.sql \
    --label all22 \
    --gap 50
```

Output goes to `profiles/YYYY-MM-DD_<machine>_<branch>_<commit>_sf<N>/` containing:
- `<label>_sf<N>.nsys-rep` — Nsight Systems capture (open in Nsight GUI for full thread view)
- `<label>_sf<N>.sqlite` — nsys SQLite export
- `<label>_sf<N>_timeline.html` — GPU-only interactive HTML timeline
- `<label>_sf<N>_combined_timeline.html` — GPU + CPU thread overlay (if `--cpu-sql` given)
- `<label>_sf<N>_cpu.json` — raw CPU thread polling data
- `metadata.json` — git commit, branch, date, machine, GPU, config used

After capturing, update the index:

```bash
python3 /home/dnguyen56/gpu-perf-tools/build_index.py profiles/
```

---

## nsys_timeline.py — standalone usage

```bash
# Export nsys report to SQLite first:
nsys export --type=sqlite --output=profile.sqlite profile.nsys-rep

# Generate HTML:
python3 nsys_timeline.py profile.sqlite [output.html]

# Options:
#   --gap <ms>            Min GPU idle gap to detect query boundaries (default: 25)
#   --no-coldwarm         Disable cold/warm labelling
#   --cpu-profile <json>  Overlay cpu_timeline.py JSON as CPU thread rows
#   --title <str>         Override page title
```

### What the HTML shows

- **Sidebar**: per-query list — click any query to enter split cold|warm view
- **Full timeline view**: all invocations, zoom (mouse wheel) + pan (drag)
- **Split view**: cold on left, warm on right, both starting at t=0, same time scale
- **Summary bar**: H2D stall % / interleaved % / compute % / idle %
- **Color coding**: H2D (blue), decode (orange), join (red), agg (purple), scan/shuffle (teal), other (grey)
- **CPU thread rows**: green = running, dark = sleeping, amber = disk wait
- **Tooltips**: hover any event for kernel name, duration, stream

---

## cpu_timeline.py — standalone usage

```bash
python3 cpu_timeline.py <duckdb_bin> <db_file> <sql_file> [output.json]

# Options:
#   --iterations <N>    Number of runs (default: 2)
#   --poll-ms <ms>      Sampling interval (default: 5)
#   --with-profiling    Enable DuckDB JSON profiling for operator annotations
#                       (adds ~20% overhead — wall times will be inflated)
```

Runs DuckDB with `SIRIUS_DISABLE=1` so no GPU initialization interferes with CPU timing.
The sql_file should be plain SQL without `gpu_execution()` wrappers.

---

## Profile organization

Profiles live under `profiles/<dir>/` where `<dir>` is:

```
YYYY-MM-DD_<machine>_<branch>_<commit>_sf<N>
```

Each directory has a `metadata.json` recording the exact capture context so results
are always traceable and comparable across sessions.

### Large .nsys-rep files

`.nsys-rep` files are typically 200MB–2GB and are **not committed to git**.
Attach them to a GitHub Release instead:

```bash
gh release create v<date> --title "<branch> SF=<N> profiles" \
  profiles/<dir>/<label>.nsys-rep
```

Small HTML outputs (< ~5MB) are committed directly:

```bash
git add profiles/<dir>/<label>_timeline.html profiles/<dir>/metadata.json
git commit -m "add <label> SF=<N> timeline"
git push
```

### Opening .nsys-rep in Nsight Systems GUI

The HTML timelines cover GPU streams and CPU thread states.
For the full Nsight Systems experience (OS runtime, pthread, system calls, CPU sampling):
- Re-capture with `nsys profile --trace=osrt,pthread,cuda,nvtx ...`
- Open `<label>.nsys-rep` in **Nsight Systems GUI** (the SQLite export omits OSRT data)

---

## profiles/

Generated HTML timelines — open in any browser, no install needed.

See [profiles/index.html](profiles/index.html) for the full listing (GitHub Pages).
