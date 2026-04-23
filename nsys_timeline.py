#!/usr/bin/env python3
"""
nsys_timeline.py  –  Interactive per-stream GPU timeline HTML from nsys SQLite export

Usage:
    python3 nsys_timeline.py <profile.sqlite> [output.html]
    python3 nsys_timeline.py --gap <ms> <profile.sqlite> [output.html]

Options:
    --gap <ms>    Min idle gap (ms) used to detect query boundaries. Default: 25.
    --no-coldwarm Disable cold/warm labelling (useful for single-query profiles).
    --title <str> Override the page title.

Output:
    Self-contained HTML with:
    - Per-GPU-stream Gantt rows (H2D / D2D / decode / join / agg / other)
    - Auto-detected query boundaries  →  "Q1 cold" / "Q1 warm" labels
    - Zoom (mouse wheel) + Pan (click-drag)
    - Hover tooltip showing event name, duration, stream
    - Summary bar: exposed H2D %, compute %, interleaved %, idle %

GPU memory:
    Not shown – requires re-profiling with:
        nsys profile --cuda-memory-usage=true ...
"""

import sqlite3, json, sys, os, argparse
from pathlib import Path
from collections import defaultdict

# ── kernel classification ──────────────────────────────────────────────────────
CATEGORIES = {
    "h2d":      {"label": "H2D transfer",    "color": "#3498db"},
    "d2d":      {"label": "D2D (GPU-GPU)",   "color": "#85c1e9"},
    "d2h":      {"label": "D2H transfer",    "color": "#5dade2"},
    "decode":   {"label": "Decode / unpack", "color": "#e67e22"},
    "join":     {"label": "Join / hash-tbl", "color": "#e74c3c"},
    "agg":      {"label": "Agg / reduce",    "color": "#9b59b6"},
    "shuffle":  {"label": "Partition / scan","color": "#1abc9c"},
    "other":    {"label": "Other kernel",    "color": "#95a5a6"},
    # CPU thread states (used when --cpu-profile is supplied)
    "cpu_run":  {"label": "CPU thread running", "color": "#2ecc71"},
    "cpu_wait": {"label": "CPU thread sleeping", "color": "#2c3e50"},
    "cpu_disk": {"label": "CPU disk wait",       "color": "#f39c12"},
}

def classify_kernel(name: str) -> str:
    n = name.lower()
    if any(x in n for x in ("bitpack", "decode", "gather_dict", "gather_fsst",
                             "kernel_decode", "kernel_gather")):
        return "decode"
    if name in ("mixed_join", "mixed_join_count", "insert_if_n",
                "retrieve", "contains_if_n", "hash_join_kernel",
                "build_join_hash_table"):
        return "join"
    if any(x in n for x in ("reduce", "agg", "count", "shmem_agg")):
        return "agg"
    if any(x in n for x in ("partition", "scan_kernel", "select_sweep",
                             "copy_block", "row_partition", "batch_memcpy",
                             "fused_concat", "static_kernel", "transform")):
        return "shuffle"
    return "other"


# ── data extraction ────────────────────────────────────────────────────────────
def extract(db: sqlite3.Connection, gap_ms: float, cold_warm: bool):
    # time origin
    t0 = db.execute("""
        SELECT MIN(t) FROM (
            SELECT MIN(start) t FROM CUPTI_ACTIVITY_KIND_MEMCPY
            UNION ALL SELECT MIN(start) FROM CUPTI_ACTIVITY_KIND_KERNEL)
    """).fetchone()[0]

    # build name lookup
    names = dict(db.execute("SELECT id, value FROM StringIds").fetchall())

    streams_set = set()
    raw_events = []  # (stream, start_ms, end_ms, cat, label)

    # memcpy
    kind_map = {1: "h2d", 2: "d2h", 8: "d2d"}
    for start, end, stream, kind, nbytes in db.execute(
        "SELECT start, end, streamId, copyKind, bytes FROM CUPTI_ACTIVITY_KIND_MEMCPY"
    ).fetchall():
        cat = kind_map.get(kind, "other")
        mb = nbytes / 1e6
        label = f"{cat.upper()} {mb:.2f} MB" if mb >= 0.1 else f"{cat.upper()} {nbytes} B"
        raw_events.append((stream, (start - t0) / 1e6, (end - t0) / 1e6, cat, label))
        streams_set.add(stream)

    # kernels
    for start, end, stream, nm_id, full_id in db.execute(
        "SELECT start, end, streamId, shortName, demangledName FROM CUPTI_ACTIVITY_KIND_KERNEL"
    ).fetchall():
        short = names.get(nm_id, "?")
        full  = names.get(full_id, short)
        cat   = classify_kernel(short)
        dur   = (end - start) / 1e6
        label = f"{short} ({dur:.2f} ms)"
        raw_events.append((stream, (start - t0) / 1e6, (end - t0) / 1e6, cat, label))
        streams_set.add(stream)

    raw_events.sort(key=lambda e: e[1])

    # ── stream labeling ──
    # figure out which streams are mainly H2D vs mostly kernels
    stream_h2d_ms   = defaultdict(float)
    stream_kern_ms  = defaultdict(float)
    for s, st, en, cat, _ in raw_events:
        dur = en - st
        if cat in ("h2d", "d2h", "d2d"):
            stream_h2d_ms[s] += dur
        else:
            stream_kern_ms[s] += dur

    stream_labels = {}
    for s in sorted(streams_set):
        h = stream_h2d_ms[s]
        k = stream_kern_ms[s]
        if h > k * 3:
            stream_labels[s] = f"S{s} (scan/H2D)"
        elif k > h * 3:
            stream_labels[s] = f"S{s} (compute)"
        else:
            stream_labels[s] = f"S{s} (mixed)"

    # ── query boundary detection ──
    all_intervals = [(st, en) for _, st, en, _, _ in raw_events]
    all_intervals.sort()

    boundaries = []  # start_ms of each detected segment
    if all_intervals:
        boundaries.append(all_intervals[0][0])
        prev_end = all_intervals[0][1]
        for st, en in all_intervals[1:]:
            if st - prev_end >= gap_ms:
                boundaries.append(st)
            prev_end = max(prev_end, en)

    total_ms = max(en for _, _, en, _, _ in raw_events) if raw_events else 0

    # build segment end list (end of each segment = start of next, or total_ms)
    seg_ends = boundaries[1:] + [total_ms + 1]

    # label segments
    seg_labels = []
    q = 1
    for i, (seg_start, seg_end) in enumerate(zip(boundaries, seg_ends)):
        if cold_warm:
            phase = "cold" if i % 2 == 0 else "warm"
            if i % 2 == 0 and i > 0:
                q += 1
            lbl = f"Q{q} {phase}"
        else:
            lbl = f"Seg {i+1}"
        seg_labels.append({"start": seg_start, "end": seg_end, "label": lbl})

    # ── per-stream event merging ──
    # Merge adjacent events of the same cat within merge_gap_ms; drop sub-threshold events.
    # Use short JSON keys (c/l) to keep file size down.
    merge_gap_ms = 2.0   # merge same-type events within 2 ms of each other
    min_dur_ms   = 0.05  # drop events shorter than this (invisible at default zoom)
    stream_events = defaultdict(list)
    for s, st, en, cat, lbl in raw_events:
        if en - st < min_dur_ms:
            continue
        evs = stream_events[s]
        if evs and evs[-1]["c"] == cat and st - evs[-1]["e"] < merge_gap_ms:
            evs[-1]["e"] = max(evs[-1]["e"], round(en, 1))
            evs[-1]["n"] = evs[-1].get("n", 1) + 1
        else:
            evs.append({"s": round(st, 1), "e": round(en, 1), "c": cat, "l": lbl})

    # ── aggregate stats ──
    bucket_ms = 1
    n_buckets = int(total_ms / bucket_ms) + 2
    h2d_b  = bytearray(n_buckets)
    kern_b = bytearray(n_buckets)

    for s, st, en, cat, _ in raw_events:
        b0 = max(0, int(st / bucket_ms))
        b1 = min(n_buckets, int(en / bucket_ms) + 1)
        if cat == "h2d":
            for i in range(b0, b1): h2d_b[i] = 1
        else:
            for i in range(b0, b1): kern_b[i] = 1

    both     = sum(1 for i in range(n_buckets) if h2d_b[i] and kern_b[i])
    h2d_only = sum(1 for i in range(n_buckets) if h2d_b[i] and not kern_b[i])
    kern_only= sum(1 for i in range(n_buckets) if kern_b[i] and not h2d_b[i])
    idle_b   = sum(1 for i in range(n_buckets) if not h2d_b[i] and not kern_b[i])

    stats = {
        "total_ms":       round(total_ms, 1),
        "n_queries":      len(boundaries),
        "h2d_mb":         round(sum(nbytes for start, end, stream, kind, nbytes
                                    in db.execute("SELECT start,end,streamId,copyKind,bytes "
                                                  "FROM CUPTI_ACTIVITY_KIND_MEMCPY WHERE copyKind=1")
                                    .fetchall()) / 1e6, 1),
        "interleaved_pct":round(both     / n_buckets * 100, 1),
        "h2d_only_pct":   round(h2d_only / n_buckets * 100, 1),
        "compute_pct":    round(kern_only / n_buckets * 100, 1),
        "idle_pct":       round(idle_b   / n_buckets * 100, 1),
    }

    streams_ordered = sorted(streams_set,
                             key=lambda s: stream_h2d_ms[s] + stream_kern_ms[s],
                             reverse=True)

    payload = {
        "stats":    stats,
        "cats":     CATEGORIES,
        "segments": seg_labels,
        "streams":  [{"id": s, "label": stream_labels[s],
                      "events": stream_events[s]}
                     for s in streams_ordered],
    }
    return payload


# ── HTML generation ────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,sans-serif}}
body{{background:#1a1a2e;color:#e0e0e0;display:flex;flex-direction:column;height:100vh;overflow:hidden}}
#header{{background:#16213e;padding:10px 16px;border-bottom:1px solid #0f3460;flex-shrink:0}}
#header h1{{font-size:14px;font-weight:600;color:#a8dadc}}
#header .sub{{font-size:11px;color:#888;margin-top:2px}}
#summary{{display:flex;gap:0;border-bottom:1px solid #0f3460;flex-shrink:0;height:36px}}
.stat-block{{flex:1;display:flex;align-items:center;justify-content:center;flex-direction:column;
             border-right:1px solid #0f3460;font-size:10px;color:#aaa}}
.stat-block .val{{font-size:16px;font-weight:700;color:#a8dadc}}
.stat-block.warn .val{{color:#e74c3c}}
.stat-block.ok   .val{{color:#2ecc71}}
#controls{{background:#16213e;padding:6px 12px;display:flex;align-items:center;gap:16px;
           border-bottom:1px solid #0f3460;flex-shrink:0;flex-wrap:wrap}}
#controls label{{font-size:11px;color:#aaa}}
#controls input[type=range]{{width:120px;accent-color:#a8dadc}}
#legend{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
.leg-item{{display:flex;align-items:center;gap:4px;font-size:10px;color:#ccc}}
.leg-swatch{{width:12px;height:12px;border-radius:2px;flex-shrink:0}}
#canvas-wrap{{flex:1;overflow:hidden;position:relative;cursor:crosshair}}
canvas{{display:block}}
#tooltip{{position:absolute;background:rgba(10,10,30,0.92);border:1px solid #a8dadc;
          border-radius:4px;padding:6px 10px;font-size:11px;pointer-events:none;
          display:none;max-width:320px;line-height:1.5;z-index:10}}
#tooltip .t-name{{color:#a8dadc;font-weight:600;margin-bottom:2px}}
#tooltip .t-row{{color:#ccc}}
#tooltip .t-row span{{color:#fff}}
</style>
</head>
<body>
<div id="header">
  <h1>GPU Stream Timeline — {title}</h1>
  <div class="sub">{subtitle}</div>
</div>
<div id="summary">
  <div class="stat-block warn" title="GPU busy only with H2D, no compute running — pure scan stall">
    <div class="val" id="s-h2d"></div><div>H2D stall</div></div>
  <div class="stat-block" title="H2D + kernels running concurrently — pipelining working">
    <div class="val" id="s-iw"></div><div>Interleaved</div></div>
  <div class="stat-block ok" title="Pure compute, no H2D — GPU winning">
    <div class="val" id="s-c"></div><div>Compute only</div></div>
  <div class="stat-block" title="GPU idle — sync / overhead gaps">
    <div class="val" id="s-idle"></div><div>GPU idle</div></div>
  <div class="stat-block" title="Total H2D data transferred">
    <div class="val" id="s-mb"></div><div>H2D GB</div></div>
  <div class="stat-block" title="Detected query invocations">
    <div class="val" id="s-q"></div><div>Invocations</div></div>
</div>
<div id="controls">
  <label>Zoom: <input type="range" id="zoom-slider" min="0.5" max="40" step="0.1" value="1"></label>
  <label>Offset: <input type="range" id="pan-slider" min="0" max="100" step="0.1" value="0"></label>
  <label><input type="checkbox" id="chk-labels" checked> Query labels</label>
  <label><input type="checkbox" id="chk-grid" checked> Time grid</label>
  <div id="legend"></div>
</div>
<div id="canvas-wrap">
  <canvas id="cv"></canvas>
  <div id="tooltip"></div>
</div>
<script>
const DATA = {data_json};

// layout constants
const ROW_H   = 28;
const LABEL_W = 150;
const AXIS_H  = 24;
const SEG_H   = 18;  // height reserved above rows for segment labels

let zoom = 1, panMs = 0;
let dragging = false, dragStartX = 0, dragStartPan = 0;

const cv    = document.getElementById('cv');
const ctx   = cv.getContext('2d');
const wrap  = document.getElementById('canvas-wrap');
const tip   = document.getElementById('tooltip');

// populate summary
const s = DATA.stats;
document.getElementById('s-h2d').textContent  = s.h2d_only_pct  + '%';
document.getElementById('s-iw').textContent   = s.interleaved_pct + '%';
document.getElementById('s-c').textContent    = s.compute_pct  + '%';
document.getElementById('s-idle').textContent = s.idle_pct     + '%';
document.getElementById('s-mb').textContent   = (s.h2d_mb/1024).toFixed(1);
document.getElementById('s-q').textContent    = s.n_queries;

// populate legend
const legDiv = document.getElementById('legend');
for (const [key, cat] of Object.entries(DATA.cats)) {{
  const el = document.createElement('div');
  el.className = 'leg-item';
  el.innerHTML = `<div class="leg-swatch" style="background:${{cat.color}}"></div>${{cat.label}}`;
  legDiv.appendChild(el);
}}

// segment bg colors (alternating cold/warm)
function segBg(lbl) {{
  if (lbl.includes('cold')) return 'rgba(52,152,219,0.07)';
  if (lbl.includes('warm')) return 'rgba(231,76,60,0.07)';
  return 'rgba(255,255,255,0.03)';
}}

function resize() {{
  cv.width  = wrap.clientWidth;
  cv.height = wrap.clientHeight;
  render();
}}

function msToX(ms) {{
  return LABEL_W + (ms - panMs) * zoom * (cv.width - LABEL_W) / s.total_ms;
}}

function xToMs(x) {{
  return panMs + (x - LABEL_W) * s.total_ms / ((cv.width - LABEL_W) * zoom);
}}

function render() {{
  const W = cv.width, H = cv.height;
  const showLabels = document.getElementById('chk-labels').checked;
  const showGrid   = document.getElementById('chk-grid').checked;

  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#1a1a2e';
  ctx.fillRect(0, 0, W, H);

  const nStreams = DATA.streams.length;
  const contentH = AXIS_H + SEG_H + nStreams * ROW_H;

  // row backgrounds
  DATA.streams.forEach((stream, si) => {{
    const y = AXIS_H + SEG_H + si * ROW_H;
    if (stream.is_divider) {{
      ctx.fillStyle = 'rgba(168,218,220,0.08)';
      ctx.fillRect(LABEL_W, y, W - LABEL_W, ROW_H);
      return;
    }}
    const base = stream.is_cpu ? 'rgba(46,204,113,0.04)' : 'rgba(255,255,255,0.02)';
    const alt  = stream.is_cpu ? 'rgba(46,204,113,0.02)' : 'rgba(0,0,0,0.1)';
    ctx.fillStyle = si % 2 === 0 ? base : alt;
    ctx.fillRect(LABEL_W, y, W - LABEL_W, ROW_H);
  }});

  // segment backgrounds + labels
  if (showLabels) {{
    DATA.segments.forEach(seg => {{
      const x0 = Math.max(LABEL_W, msToX(seg.start));
      const x1 = Math.min(W,       msToX(seg.end));
      if (x1 < LABEL_W || x0 > W) return;
      // background stripe
      ctx.fillStyle = segBg(seg.label);
      ctx.fillRect(x0, AXIS_H, x1 - x0, SEG_H + nStreams * ROW_H);
      // boundary line
      ctx.strokeStyle = seg.label.includes('cold') ? 'rgba(100,180,255,0.4)'
                      : seg.label.includes('warm') ? 'rgba(255,100,100,0.4)'
                      : 'rgba(200,200,200,0.2)';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x0, AXIS_H); ctx.lineTo(x0, contentH); ctx.stroke();
      // label
      if (x1 - x0 > 28) {{
        ctx.fillStyle = 'rgba(200,200,200,0.7)';
        ctx.font = '9px monospace';
        ctx.fillText(seg.label, x0 + 3, AXIS_H + 12);
      }}
    }});
  }}

  // time grid + axis
  if (showGrid) {{
    const visibleMs = s.total_ms / zoom;
    const gridStep = niceStep(visibleMs / 8);
    const startGrid = Math.ceil(panMs / gridStep) * gridStep;
    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.fillStyle   = 'rgba(200,200,200,0.5)';
    ctx.font = '9px monospace';
    ctx.lineWidth = 1;
    for (let ms = startGrid; ms < panMs + visibleMs + gridStep; ms += gridStep) {{
      const x = msToX(ms);
      if (x < LABEL_W || x > W) continue;
      ctx.beginPath(); ctx.moveTo(x, AXIS_H); ctx.lineTo(x, contentH); ctx.stroke();
      ctx.fillText(fmtMs(ms), x + 2, AXIS_H - 4);
    }}
  }}

  // events
  DATA.streams.forEach((stream, si) => {{
    const y  = AXIS_H + SEG_H + si * ROW_H + 2;
    const rh = ROW_H - 4;
    stream.events.forEach(ev => {{
      const x0 = msToX(ev.s);
      const x1 = msToX(ev.e);
      if (x1 < LABEL_W || x0 > W) return;
      const pw = Math.max(0.5, x1 - x0);
      const col = DATA.cats[ev.c] ? DATA.cats[ev.c].color : '#888';
      ctx.fillStyle = col;
      ctx.fillRect(Math.max(LABEL_W, x0), y, Math.min(pw, W - Math.max(LABEL_W, x0)), rh);
    }});
  }});

  // stream labels (left panel)
  ctx.fillStyle = '#1a1a2e';
  ctx.fillRect(0, 0, LABEL_W, H);
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(LABEL_W, 0); ctx.lineTo(LABEL_W, H); ctx.stroke();

  DATA.streams.forEach((stream, si) => {{
    const y = AXIS_H + SEG_H + si * ROW_H;
    if (stream.is_divider) {{
      ctx.fillStyle = 'rgba(168,218,220,0.15)';
      ctx.fillRect(0, y, LABEL_W, ROW_H);
      ctx.fillStyle = '#a8dadc';
      ctx.font = 'bold 9px monospace';
      ctx.fillText(stream.label, 4, y + ROW_H / 2 + 3);
      ctx.strokeStyle = 'rgba(168,218,220,0.3)';
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
      return;
    }}
    const base = stream.is_cpu ? 'rgba(46,204,113,0.06)' : 'rgba(255,255,255,0.02)';
    const alt  = stream.is_cpu ? 'rgba(46,204,113,0.03)' : 'rgba(0,0,0,0.1)';
    ctx.fillStyle = si % 2 === 0 ? base : alt;
    ctx.fillRect(0, y, LABEL_W, ROW_H);
    ctx.fillStyle = stream.is_cpu ? '#7dcea0' : '#ccc';
    ctx.font = '10px monospace';
    ctx.fillText(stream.label, 6, y + ROW_H / 2 + 4);
    ctx.strokeStyle = 'rgba(255,255,255,0.05)';
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }});

  // axis header background
  ctx.fillStyle = '#16213e';
  ctx.fillRect(0, 0, W, AXIS_H);
  ctx.strokeStyle = 'rgba(255,255,255,0.1)';
  ctx.beginPath(); ctx.moveTo(0, AXIS_H); ctx.lineTo(W, AXIS_H); ctx.stroke();
}}

function niceStep(approx) {{
  const mag = Math.pow(10, Math.floor(Math.log10(approx)));
  const f = approx / mag;
  return mag * (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10);
}}

function fmtMs(ms) {{
  return ms >= 1000 ? (ms/1000).toFixed(2)+'s' : ms.toFixed(0)+'ms';
}}

// ── interactions ──────────────────────────────────────────────────────────────
function clampPan() {{
  const visMs = s.total_ms / zoom;
  panMs = Math.max(0, Math.min(panMs, s.total_ms - visMs));
  const slider = document.getElementById('pan-slider');
  const maxPan = Math.max(0, s.total_ms - visMs);
  slider.max   = maxPan.toFixed(1);
  slider.value = panMs.toFixed(1);
}}

cv.addEventListener('wheel', e => {{
  e.preventDefault();
  const rect = cv.getBoundingClientRect();
  const mx   = e.clientX - rect.left;
  const focMs = xToMs(mx);
  const factor = e.deltaY < 0 ? 1.15 : 1/1.15;
  zoom = Math.max(0.5, Math.min(200, zoom * factor));
  panMs = focMs - (mx - LABEL_W) * s.total_ms / ((cv.width - LABEL_W) * zoom);
  clampPan();
  document.getElementById('zoom-slider').value = zoom.toFixed(2);
  render();
}}, {{passive: false}});

cv.addEventListener('mousedown', e => {{
  dragging = true; dragStartX = e.clientX; dragStartPan = panMs;
  cv.style.cursor = 'grabbing';
}});

cv.addEventListener('mouseup',  () => {{ dragging = false; cv.style.cursor = 'crosshair'; }});
cv.addEventListener('mouseleave',() => {{ dragging = false; tip.style.display = 'none'; }});

cv.addEventListener('mousemove', e => {{
  const rect = cv.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;

  if (dragging) {{
    const dMs = (dragStartX - e.clientX) * s.total_ms / ((cv.width - LABEL_W) * zoom);
    panMs = dragStartPan + dMs;
    clampPan();
    render();
    return;
  }}

  // hit test
  if (mx <= LABEL_W) {{ tip.style.display = 'none'; return; }}
  const tMs = xToMs(mx);
  const si  = Math.floor((my - AXIS_H - SEG_H) / ROW_H);
  if (si < 0 || si >= DATA.streams.length) {{ tip.style.display = 'none'; return; }}
  const stream = DATA.streams[si];
  let hit = null;
  for (const ev of stream.events) {{
    if (tMs >= ev.s && tMs <= ev.e) {{ hit = ev; break; }}
  }}
  if (!hit) {{ tip.style.display = 'none'; return; }}

  const cat  = DATA.cats[hit.c] || {{label: hit.c, color: '#888'}};
  const dur  = hit.e - hit.s;
  const cnt  = hit.n ? ` (×${{hit.n}} merged)` : '';
  tip.innerHTML = `
    <div class="t-name" style="color:${{cat.color}}">${{cat.label}}</div>
    <div class="t-row">Duration: <span>${{dur.toFixed(1)}} ms</span></div>
    <div class="t-row">Stream: <span>${{stream.label}}</span></div>
    <div class="t-row">Detail: <span>${{hit.l}}${{cnt}}</span></div>
    <div class="t-row">Time: <span>+${{hit.s.toFixed(0)}}ms → +${{hit.e.toFixed(0)}}ms</span></div>`;
  tip.style.display = 'block';
  const tx = Math.min(e.clientX + 12, window.innerWidth - 340);
  const ty = Math.min(e.clientY + 12, window.innerHeight - 120);
  tip.style.left = tx + 'px'; tip.style.top = ty + 'px';
}});

document.getElementById('zoom-slider').addEventListener('input', e => {{
  const cx = (cv.width - LABEL_W) / 2 + LABEL_W;
  const focMs = xToMs(cx);
  zoom = parseFloat(e.target.value);
  panMs = focMs - (cx - LABEL_W) * s.total_ms / ((cv.width - LABEL_W) * zoom);
  clampPan(); render();
}});

document.getElementById('pan-slider').addEventListener('input', e => {{
  panMs = parseFloat(e.target.value); clampPan(); render();
}});

document.getElementById('chk-labels').addEventListener('change', render);
document.getElementById('chk-grid').addEventListener('change', render);

new ResizeObserver(resize).observe(wrap);
resize();
</script>
</body>
</html>
"""


def load_cpu_profile(cpu_json_path: str, gpu_total_ms: float) -> list:
    """
    Convert cpu_timeline.py JSON output into stream-like rows for the renderer.
    Each DuckDB worker thread becomes one row; states R→cpu_run, S→cpu_wait, D→cpu_disk.
    All iterations are concatenated with a fixed offset so they line up with GPU segments.
    Returns list of stream dicts to append to payload["streams"].
    """
    data = json.loads(Path(cpu_json_path).read_text())
    iters = data["iters"]

    # Collect all thread names across all iters to build stable rows
    all_tids_names = {}
    for it in iters:
        for tid, info in it["threads"].items():
            all_tids_names[tid] = info["name"]

    # Filter to interesting threads (skip the main duckdb process if all it does is wait)
    # Keep threads that spend at least 10ms running across all iters
    active_tids = []
    for tid, info_list in [(tid, [it["threads"].get(tid) for it in iters
                                   if tid in it["threads"]]) for tid in all_tids_names]:
        run_ms = sum(seg["e"] - seg["s"]
                     for info in info_list if info
                     for seg in info["segs"] if seg["st"] in ("R", "D"))
        if run_ms >= 5:
            active_tids.append(tid)

    # Sort: most-running threads first
    def total_run(tid):
        return sum(seg["e"] - seg["s"]
                   for it in iters if tid in it["threads"]
                   for seg in it["threads"][tid]["segs"] if seg["st"] in ("R", "D"))
    active_tids.sort(key=total_run, reverse=True)

    # Build per-iter time offsets to align with GPU query segments
    # Simple heuristic: spread iters evenly across gpu_total_ms
    iter_ms = gpu_total_ms / max(len(iters), 1)

    state_to_cat = {"R": "cpu_run", "S": "cpu_wait", "D": "cpu_disk"}

    streams = []
    for tid in active_tids:
        name = all_tids_names[tid]
        events = []
        for it_idx, it in enumerate(iters):
            if tid not in it["threads"]:
                continue
            offset = it_idx * iter_ms
            phase  = "cold" if it_idx % 2 == 0 else "warm"
            for seg in it["threads"][tid]["segs"]:
                cat = state_to_cat.get(seg["st"], "cpu_wait")
                dur = seg["e"] - seg["s"]
                if dur < 1.0 and cat == "cpu_wait":
                    continue  # skip tiny waits
                events.append({
                    "s": round(offset + seg["s"], 1),
                    "e": round(offset + seg["e"], 1),
                    "c": cat,
                    "l": f"{phase} {seg['st']} ({dur:.0f}ms)",
                })
        if events:
            streams.append({
                "id":     f"cpu_{tid}",
                "label":  f"CPU {name[:12]} ({tid})",
                "events": events,
                "is_cpu": True,
            })

    # Add operator summary row (one bar per query phase showing top operator)
    for it_idx, it in enumerate(iters):
        ops = it.get("operators", [])
        if not ops:
            continue
        offset = it_idx * iter_ms
        wall   = it["wall_ms"]
        phase  = "cold" if it_idx % 2 == 0 else "warm"
        # Build a simple cumulative Gantt from operator timings (sequential approximation)
        cursor = 0.0
        events = []
        for op in sorted(ops, key=lambda o: o["ms"], reverse=True)[:8]:
            if op["ms"] < 5:
                continue
            events.append({
                "s": round(offset + cursor, 1),
                "e": round(offset + min(cursor + op["ms"], wall), 1),
                "c": "shuffle",
                "l": f"{op['name']} ({op['ms']:.0f}ms)",
            })
            cursor += op["ms"]
            if cursor >= wall:
                break
        if events:
            if len(streams) == 0 or streams[0].get("id") != "cpu_ops":
                streams.insert(0, {"id": "cpu_ops", "label": "CPU operators",
                                   "events": [], "is_cpu": True})
            streams[0]["events"].extend(events)

    return streams


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sqlite",  help="nsys SQLite export (.sqlite)")
    ap.add_argument("output",  nargs="?", help="Output HTML path (default: <sqlite>.html)")
    ap.add_argument("--gap",   type=float, default=25.0,
                    help="Min GPU idle gap (ms) to split queries (default: 25)")
    ap.add_argument("--no-coldwarm", action="store_true",
                    help="Disable cold/warm labelling")
    ap.add_argument("--cpu-profile", default=None, metavar="CPU_JSON",
                    help="cpu_timeline.py JSON output to overlay as CPU thread rows")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    sqlite_path = args.sqlite
    output_path = args.output or (Path(sqlite_path).stem + "_timeline.html")
    title = args.title or Path(sqlite_path).stem

    print(f"Reading {sqlite_path} …", flush=True)
    db = sqlite3.connect(sqlite_path)

    n_kern  = db.execute("SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_KERNEL").fetchone()[0]
    n_memcpy= db.execute("SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_MEMCPY").fetchone()[0]
    print(f"  {n_kern:,} kernel events, {n_memcpy:,} memcpy events")

    print("Extracting GPU events …", flush=True)
    payload = extract(db, gap_ms=args.gap, cold_warm=not args.no_coldwarm)

    if args.cpu_profile:
        print(f"Loading CPU profile {args.cpu_profile} …", flush=True)
        cpu_streams = load_cpu_profile(args.cpu_profile, payload["stats"]["total_ms"])
        # Insert divider marker then CPU rows after GPU rows
        payload["streams"].append({"id": "divider", "label": "── CPU (DuckDB) ──",
                                   "events": [], "is_cpu": False, "is_divider": True})
        payload["streams"].extend(cpu_streams)
        print(f"  Added {len(cpu_streams)} CPU thread rows")

    n_ev = sum(len(s["events"]) for s in payload["streams"])
    print(f"  {n_ev:,} merged segments across {len(payload['streams'])} rows, "
          f"{payload['stats']['n_queries']} query invocations detected")

    subtitle = (f"{n_kern:,} kernel events · {n_memcpy:,} memcpy events · "
                f"gap threshold {args.gap} ms"
                + (f" · CPU: {Path(args.cpu_profile).stem}" if args.cpu_profile else "")
                + (f" · cold/warm labelled" if not args.no_coldwarm else ""))

    print("Rendering HTML …", flush=True)
    data_json = json.dumps(payload, separators=(",", ":"))
    html = HTML_TEMPLATE.format(
        title=title,
        subtitle=subtitle,
        data_json=data_json,
    )

    Path(output_path).write_text(html, encoding="utf-8")
    size_kb = Path(output_path).stat().st_size // 1024
    print(f"Written: {output_path}  ({size_kb} KB)")


if __name__ == "__main__":
    main()
