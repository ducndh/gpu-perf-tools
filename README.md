# gpu-perf-tools

Profiling tools and interactive HTML timelines for Sirius GPU engine analysis.

## Tools

| Script | Purpose |
|--------|---------|
| `nsys_timeline.py` | Generates a self-contained interactive HTML timeline from an nsys SQLite export |
| `cpu_timeline.py` | Polls `/proc/<pid>/task/*/stat` for CPU thread states; feeds into `nsys_timeline.py` for side-by-side comparison |
| `capture.sh` | One-command wrapper: runs `nsys profile` + sqlite export + CPU profiling + HTML generation, writes into an organized profile directory |
| `build_index.py` | Scans `profiles/` subdirectories and generates `profiles/index.html` (GitHub Pages listing) |
| `build_experiment_index.py` | Renders a per-experiment matrix `index.html` (config × query) from `metadata.json` + `nsys_summary.csv` |

---

## Quick start: capture a new profile

```bash
# cd into a sirius checkout (capture.sh walks up to find src/sirius_extension.cpp)
cd /path/to/your/sirius

# Single query:
bash /path/to/gpu-perf-tools/capture.sh \
    --sf 100 \
    --sql test/tpch_performance/tpch_queries/gpu/q1.sql \
    --label q1 \
    --cpu-sql test/tpch_performance/tpch_queries/orig/q1.sql

# All 22 queries:
bash /path/to/gpu-perf-tools/capture.sh \
    --sf 100 \
    --sql /tmp/all22_gpu.sql \
    --label all22 \
    --gap 50
```

Path resolution:
- `--sirius-dir` is auto-detected by walking up from `cwd` for `src/sirius_extension.cpp`. Override with `--sirius-dir /path/to/sirius`.
- `--duckdb` defaults to `<sirius-dir>/build/release/duckdb`; override with `--duckdb /path` or `$DUCKDB`.
- `--nsys` defaults to `nsys` on `$PATH`; override with `--nsys /path` or `$NSYS`.
- `--out-dir` overrides the default `profiles/<dir>/` location.

Output goes to `profiles/YYYY-MM-DD_<machine>_<branch>_<commit>_sf<N>/` containing:
- `<label>_sf<N>.nsys-rep` — Nsight Systems capture (open in Nsight GUI for full thread view)
- `<label>_sf<N>.sqlite` — nsys SQLite export
- `<label>_sf<N>_timeline.html` — GPU-only interactive HTML timeline
- `<label>_sf<N>_combined_timeline.html` — GPU + CPU thread overlay (if `--cpu-sql` given)
- `<label>_sf<N>_cpu.json` — raw CPU thread polling data
- `metadata.json` — git commit, branch, date, machine, GPU, config used

After capturing, update the index:

```bash
python3 /path/to/gpu-perf-tools/build_index.py profiles/
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

### .nsys-rep file size policy

When the capture is wrapped in `cudaProfilerStart`/`cudaProfilerStop` (Sirius's
`profiler_start`/`profiler_stop` SQL functions, used for single-warm-iter
captures), .nsys-rep files are typically 100KB–2MB per query — small enough to
commit directly. The `preloaded-scan-region` experiment dir holds 66 such files
in 40MB total.

For full-process (cold + warm + setup) captures without a profiler-range wrapper,
.nsys-rep is 200MB–2GB. Attach those to a GitHub Release instead:

```bash
gh release create v<date> --title "<branch> SF=<N> profiles" \
  profiles/<dir>/<label>.nsys-rep
```

Commit small artifacts directly:

```bash
git add profiles/<dir>/
git commit -m "add <branch> SF=<N> profile dir"
git push
```

### Multi-config experiments

For sweeps across multiple configs (e.g. native vs enc_region vs dec_region ×
22 queries × 1 SF = 66 captures), use the experiment dir layout:

```
profiles/<exp_dir>/
  metadata.json         # kind: "experiment", lists configs + queries
  nsys_summary.csv      # config,query,kernel_total_ms,sync_total_ms,h2d_bytes,...
  <config>/q<N>/q<N>.nsys-rep
```

Then render the per-experiment matrix:

```bash
python3 build_experiment_index.py profiles/<exp_dir>/
python3 build_index.py profiles/         # update top-level index too
```

`build_index.py` recognizes experiment dirs (via `kind: "experiment"` or the
presence of `nsys_summary.csv`) and emits a single "Open experiment matrix"
link instead of dumping all 66 file links.

### Opening .nsys-rep in Nsight Systems GUI

The HTML timelines cover GPU streams and CPU thread states.
For the full Nsight Systems experience (OS runtime, pthread, system calls, CPU sampling):
- Re-capture with `nsys profile --trace=osrt,pthread,cuda,nvtx ...`
- Open `<label>.nsys-rep` in **Nsight Systems GUI** (the SQLite export omits OSRT data)

---

## profiles/

Generated HTML timelines — open in any browser, no install needed.

See [profiles/index.html](profiles/index.html) for the full listing (GitHub Pages).
