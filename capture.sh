#!/usr/bin/env bash
# capture.sh — one-command GPU+CPU profiling wrapper for Sirius/DuckDB
#
# Usage (fully explicit):
#   bash capture.sh \
#       --sirius-dir /path/to/sirius \
#       --sf 100 --sql /path/to/q1.sql --label q1
#
# Usage (path defaults: --duckdb relative to --sirius-dir, or to cwd):
#   cd ~/sirius
#   bash /path/to/gpu-perf-tools/capture.sh \
#       --sf 100 --sql test/tpch_performance/tpch_queries/gpu/q1.sql --label q1
#
# All paths are resolved relative to wherever you cd'd before invocation, EXCEPT
# the duckdb binary and nsys binary which can be set explicitly with --duckdb /
# --nsys or via $DUCKDB / $NSYS env vars.

set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_DIR="$TOOLS_DIR"

# ── defaults ──────────────────────────────────────────────────────────────────
SIRIUS_DIR="${SIRIUS_DIR:-}"     # optional; if set, default --duckdb resolves under it
DUCKDB="${DUCKDB:-}"             # auto-derived below if empty
DB_FILE="${DB_FILE:-}"           # optional persistent db
NSYS="${NSYS:-nsys}"             # PATH lookup by default
GAP_MS=25
SF=""
SQL=""
CPU_SQL=""
LABEL=""
ITERATIONS=2
POLL_MS=5

usage() {
  echo "Usage: $0 --sf <N> --sql <gpu_sql_file> --label <name> [options]"
  echo ""
  echo "Options:"
  echo "  --sf <N>            Scale factor (for metadata)"
  echo "  --sql <file>        GPU SQL file (with gpu_execution wrappers)"
  echo "  --label <name>      Short name for this profile (e.g. q1, all22)"
  echo "  --cpu-sql <file>    CPU SQL file (plain SQL, no wrappers) for cpu_timeline.py"
  echo "  --gap <ms>          nsys_timeline.py query boundary gap (default: $GAP_MS)"
  echo "  --iterations <N>    Number of iterations for cpu_timeline.py (default: $ITERATIONS)"
  echo "  --sirius-dir <dir>  Path to the sirius checkout (overrides cwd auto-detect)"
  echo "  --duckdb <path>     DuckDB binary (default: <sirius-dir>/build/release/duckdb)"
  echo "  --nsys <path>       nsys binary (default: 'nsys' from PATH; or set \$NSYS)"
  echo "  --out-dir <dir>     Override the output directory under profiles/"
  exit 1
}

OUT_DIR_OVERRIDE=""

# ── argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)           SF="$2";                shift 2;;
    --sql)          SQL="$2";               shift 2;;
    --cpu-sql)      CPU_SQL="$2";           shift 2;;
    --label)        LABEL="$2";             shift 2;;
    --gap)          GAP_MS="$2";            shift 2;;
    --iterations)   ITERATIONS="$2";        shift 2;;
    --sirius-dir)   SIRIUS_DIR="$2";        shift 2;;
    --duckdb)       DUCKDB="$2";            shift 2;;
    --nsys)         NSYS="$2";              shift 2;;
    --out-dir)      OUT_DIR_OVERRIDE="$2";  shift 2;;
    -h|--help)      usage;;
    *)              echo "Unknown option: $1"; usage;;
  esac
done

# Resolve --sirius-dir from cwd if not set: walk up looking for src/sirius_extension.cpp.
if [[ -z "$SIRIUS_DIR" ]]; then
  d="$PWD"
  while [[ "$d" != "/" ]]; do
    if [[ -f "$d/src/sirius_extension.cpp" ]]; then
      SIRIUS_DIR="$d"
      break
    fi
    d="$(dirname "$d")"
  done
fi

# Resolve DUCKDB default from SIRIUS_DIR.
if [[ -z "$DUCKDB" ]]; then
  if [[ -n "$SIRIUS_DIR" && -x "$SIRIUS_DIR/build/release/duckdb" ]]; then
    DUCKDB="$SIRIUS_DIR/build/release/duckdb"
  else
    DUCKDB="build/release/duckdb"
  fi
fi

# Resolve nsys: if not on PATH and $NSYS isn't an executable, error out.
if ! command -v "$NSYS" >/dev/null 2>&1 && [[ ! -x "$NSYS" ]]; then
  echo "ERROR: nsys not found (looked for '$NSYS'). Set --nsys /path/to/nsys or export NSYS=..." >&2
  exit 1
fi

[[ -z "$SF" || -z "$SQL" || -z "$LABEL" ]] && { echo "ERROR: --sf, --sql, and --label are required"; usage; }
[[ ! -f "$SQL" ]] && { echo "ERROR: SQL file not found: $SQL"; exit 1; }
[[ ! -x "$DUCKDB" ]] && { echo "ERROR: DuckDB binary not found or not executable: $DUCKDB"; exit 1; }

# ── collect metadata ──────────────────────────────────────────────────────────
# Resolve commit/branch from $SIRIUS_DIR if set, otherwise from cwd. Strip any
# slashes in the branch name so the directory name stays single-level.
git_in() { git -C "${SIRIUS_DIR:-.}" "$@" 2>/dev/null; }
COMMIT=$(git_in rev-parse --short HEAD || echo "unknown")
BRANCH=$(git_in branch --show-current || echo "unknown")
BRANCH_SLUG=${BRANCH//\//-}
# Hostname can be a long FQDN; keep just the short name for readability.
MACHINE=$(hostname -s 2>/dev/null || hostname)
GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | tr ' ' '-' || echo "unknown-gpu")
DATE=$(date +%Y-%m-%d)
COMMIT_MSG=$(git_in log -1 --pretty=%s || echo "")

DIRNAME="${DATE}_${MACHINE}_${BRANCH_SLUG}_${COMMIT}_sf${SF}"
OUTDIR="${OUT_DIR_OVERRIDE:-$TOOLS_DIR/profiles/$DIRNAME}"
mkdir -p "$OUTDIR"

echo "=== capture.sh ==="
echo "  label:   $LABEL"
echo "  sf:      $SF"
echo "  commit:  $BRANCH @ $COMMIT"
echo "  machine: $MACHINE ($GPU)"
echo "  output:  $OUTDIR"
echo ""

# ── temp files ────────────────────────────────────────────────────────────────
SQLITE_FILE="$OUTDIR/${LABEL}_sf${SF}.sqlite"
NSYS_REP="$OUTDIR/${LABEL}_sf${SF}.nsys-rep"
CPU_JSON="$OUTDIR/${LABEL}_sf${SF}_cpu.json"
HTML_GPU="$OUTDIR/${LABEL}_sf${SF}_timeline.html"
HTML_COMBINED="$OUTDIR/${LABEL}_sf${SF}_combined_timeline.html"

# ── GPU profiling via nsys ────────────────────────────────────────────────────
echo "--- [1/3] Running nsys profile ---"
DB_ARGS=""
[[ -n "$DB_FILE" ]] && DB_ARGS="$DB_FILE"

"$NSYS" profile \
    --trace=cuda,nvtx \
    --output="$NSYS_REP" \
    --force-overwrite=true \
    "$DUCKDB" $DB_ARGS -unsigned -noheader < "$SQL"

echo "--- Exporting nsys → sqlite ---"
"$NSYS" export --type=sqlite --output="$SQLITE_FILE" --force-overwrite=true "$NSYS_REP"

echo "--- [2/3] Generating GPU HTML timeline ---"
python3 "$SCRIPT_DIR/nsys_timeline.py" \
    --gap "$GAP_MS" \
    --title "${LABEL} SF=${SF}  GPU  (${BRANCH} @ ${COMMIT})" \
    "$SQLITE_FILE" "$HTML_GPU"

# ── CPU profiling ─────────────────────────────────────────────────────────────
if [[ -n "$CPU_SQL" ]]; then
  [[ ! -f "$CPU_SQL" ]] && { echo "ERROR: CPU SQL file not found: $CPU_SQL"; exit 1; }
  echo "--- [3/3] Running cpu_timeline.py ---"
  python3 "$SCRIPT_DIR/cpu_timeline.py" \
      --iterations "$ITERATIONS" \
      --poll-ms "$POLL_MS" \
      "$DUCKDB" "${DB_FILE:-:memory:}" "$CPU_SQL" "$CPU_JSON"

  echo "--- Generating combined GPU+CPU HTML timeline ---"
  python3 "$SCRIPT_DIR/nsys_timeline.py" \
      --gap "$GAP_MS" \
      --cpu-profile "$CPU_JSON" \
      --title "${LABEL} SF=${SF}  GPU+CPU  (${BRANCH} @ ${COMMIT})" \
      "$SQLITE_FILE" "$HTML_COMBINED"
else
  echo "--- [3/3] Skipped CPU profiling (no --cpu-sql given) ---"
fi

# ── write metadata.json ───────────────────────────────────────────────────────
cat > "$OUTDIR/metadata.json" <<EOF
{
  "date": "$DATE",
  "machine": "$MACHINE",
  "gpu": "$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo unknown)",
  "branch": "$BRANCH",
  "commit": "$COMMIT",
  "commit_message": "$COMMIT_MSG",
  "scale_factor": $SF,
  "label": "$LABEL",
  "sql_file": "$SQL",
  "cpu_sql_file": "${CPU_SQL:-null}",
  "gap_ms": $GAP_MS,
  "files": {
    "nsys_rep": "$(basename "$NSYS_REP")",
    "sqlite": "$(basename "$SQLITE_FILE")",
    "html_gpu": "$(basename "$HTML_GPU")"$(
      [[ -n "$CPU_SQL" ]] && echo ',
    "cpu_json": "'$(basename "$CPU_JSON")'",
    "html_combined": "'$(basename "$HTML_COMBINED")'"'
    )
  }
}
EOF

echo ""
echo "=== Done ==="
echo "  Profile dir: $OUTDIR"
echo "  GPU HTML:    $HTML_GPU"
[[ -n "$CPU_SQL" ]] && echo "  Combined:    $HTML_COMBINED"
echo ""
echo "  NOTE: $NSYS_REP is $(du -sh "$NSYS_REP" 2>/dev/null | cut -f1 || echo '?')"
echo "  If >50MB, attach to a GitHub Release:"
echo "    gh release create v${DATE} --title '${BRANCH} SF=${SF}' '$NSYS_REP'"
echo ""
echo "  To update the profiles index:"
echo "    python3 $SCRIPT_DIR/build_index.py $TOOLS_DIR/profiles"
