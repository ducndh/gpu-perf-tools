#!/usr/bin/env python3
"""
nsys_timeline.py  –  Interactive per-query GPU/CPU timeline HTML from nsys SQLite export

Usage:
    python3 nsys_timeline.py <profile.sqlite> [output.html]
    python3 nsys_timeline.py --gap <ms> --cpu-profile <cpu.json> <profile.sqlite> [out.html]

Options:
    --gap <ms>          Min idle gap (ms) to split query invocations. Default: 25.
    --no-coldwarm       Disable cold/warm labelling.
    --cpu-profile <f>   cpu_timeline.py JSON to overlay as CPU thread rows.
    --title <str>       Override page title.

The HTML includes:
  - Sidebar: per-query list, click → split cold/warm panes aligned to t=0
  - Full-timeline view with zoom+pan (default)
  - Per-query split view: cold (left) | warm (right), same time scale
  - H2D-stall / interleaved / compute / idle summary bar
  - Hover tooltips on every event
  - CPU thread rows below GPU streams when --cpu-profile is given

GPU memory overlay: re-profile with --cuda-memory-usage=true.
CPU thread view:    re-profile with --trace=osrt,pthread (or use cpu_timeline.py).
"""

import sqlite3, json, sys, os, argparse
from pathlib import Path
from collections import defaultdict

# ── kernel classification ───────────────────────────────────────────────────────
CATEGORIES = {
    "h2d":      {"label": "H2D transfer",       "color": "#3498db"},
    "d2d":      {"label": "D2D (GPU-GPU)",       "color": "#85c1e9"},
    "d2h":      {"label": "D2H transfer",        "color": "#5dade2"},
    "decode":   {"label": "Decode / unpack",     "color": "#e67e22"},
    "join":     {"label": "Join / hash-tbl",     "color": "#e74c3c"},
    "agg":      {"label": "Agg / reduce",        "color": "#9b59b6"},
    "shuffle":  {"label": "Partition / scan",    "color": "#1abc9c"},
    "other":    {"label": "Other kernel",        "color": "#7f8c8d"},
    "cpu_run":  {"label": "CPU thread running",  "color": "#2ecc71"},
    "cpu_wait": {"label": "CPU thread sleeping", "color": "#2c3e50"},
    "cpu_disk": {"label": "CPU disk wait",       "color": "#f39c12"},
}

def classify_kernel(name: str) -> str:
    n = name.lower()
    if any(x in n for x in ("bitpack", "decode", "gather_dict", "gather_fsst",
                             "kernel_decode", "kernel_gather")):
        return "decode"
    if name in ("mixed_join", "mixed_join_count", "insert_if_n",
                "retrieve", "contains_if_n", "hash_join_kernel"):
        return "join"
    if any(x in n for x in ("reduce", "agg", "count", "shmem_agg")):
        return "agg"
    if any(x in n for x in ("partition", "scan_kernel", "select_sweep",
                             "copy_block", "row_partition", "batch_memcpy",
                             "fused_concat", "static_kernel", "transform")):
        return "shuffle"
    return "other"


# ── GPU event extraction ────────────────────────────────────────────────────────
def extract(db, gap_ms, cold_warm):
    t0 = db.execute("""
        SELECT MIN(t) FROM (
            SELECT MIN(start) t FROM CUPTI_ACTIVITY_KIND_MEMCPY
            UNION ALL SELECT MIN(start) FROM CUPTI_ACTIVITY_KIND_KERNEL)
    """).fetchone()[0]

    names = dict(db.execute("SELECT id, value FROM StringIds").fetchall())
    streams_set = set()
    raw_events  = []

    kind_map = {1: "h2d", 2: "d2h", 8: "d2d"}
    for start, end, stream, kind, nbytes in db.execute(
        "SELECT start,end,streamId,copyKind,bytes FROM CUPTI_ACTIVITY_KIND_MEMCPY"
    ).fetchall():
        cat = kind_map.get(kind, "other")
        mb  = nbytes / 1e6
        lbl = f"{cat.upper()} {mb:.1f}MB" if mb >= 0.1 else f"{cat.upper()} {nbytes}B"
        raw_events.append((stream, (start-t0)/1e6, (end-t0)/1e6, cat, lbl))
        streams_set.add(stream)

    for start, end, stream, nm_id, _ in db.execute(
        "SELECT start,end,streamId,shortName,demangledName FROM CUPTI_ACTIVITY_KIND_KERNEL"
    ).fetchall():
        short = names.get(nm_id, "?")
        cat   = classify_kernel(short)
        dur   = (end - start) / 1e6
        raw_events.append((stream, (start-t0)/1e6, (end-t0)/1e6, cat,
                           f"{short} ({dur:.1f}ms)"))
        streams_set.add(stream)

    raw_events.sort(key=lambda e: e[1])

    stream_h2d_ms  = defaultdict(float)
    stream_kern_ms = defaultdict(float)
    for s, st, en, cat, _ in raw_events:
        d = en - st
        (stream_h2d_ms if cat in ("h2d","d2h","d2d") else stream_kern_ms)[s] += d

    stream_labels = {}
    for s in streams_set:
        h, k = stream_h2d_ms[s], stream_kern_ms[s]
        stream_labels[s] = (f"S{s} scan/H2D" if h > k*3 else
                            f"S{s} compute"  if k > h*3 else f"S{s} mixed")

    # ── boundary detection ──
    intervals = sorted((st, en) for _, st, en, _, _ in raw_events)
    boundaries = []
    if intervals:
        boundaries.append(intervals[0][0])
        prev_end = intervals[0][1]
        for st, en in intervals[1:]:
            if st - prev_end >= gap_ms:
                boundaries.append(st)
            prev_end = max(prev_end, en)

    total_ms = max(en for _, _, en, _, _ in raw_events) if raw_events else 0
    seg_ends = boundaries[1:] + [total_ms + 1]

    segments = []
    q = 1
    for i, (seg_start, seg_end) in enumerate(zip(boundaries, seg_ends)):
        if cold_warm:
            phase = "cold" if i % 2 == 0 else "warm"
            if i % 2 == 0 and i > 0:
                q += 1
            lbl = f"Q{q} {phase}"
        else:
            lbl = f"Seg {i+1}"
        segments.append({"start": round(seg_start, 1),
                         "end":   round(seg_end,   1),
                         "label": lbl})

    # ── merge events ──
    merge_gap_ms = 2.0
    min_dur_ms   = 0.05
    stream_events = defaultdict(list)
    for s, st, en, cat, lbl in raw_events:
        if en - st < min_dur_ms:
            continue
        evs = stream_events[s]
        if evs and evs[-1]["c"] == cat and st - evs[-1]["e"] < merge_gap_ms:
            evs[-1]["e"] = max(evs[-1]["e"], round(en, 1))
            evs[-1]["n"] = evs[-1].get("n", 1) + 1
        else:
            evs.append({"s": round(st,1), "e": round(en,1), "c": cat, "l": lbl})

    # ── summary stats ──
    n_buck = int(total_ms) + 2
    h2d_b  = bytearray(n_buck)
    kern_b = bytearray(n_buck)
    for s, st, en, cat, _ in raw_events:
        b0, b1 = max(0,int(st)), min(n_buck, int(en)+1)
        if cat == "h2d":
            for i in range(b0, b1): h2d_b[i] = 1
        elif cat not in ("d2h","d2d"):
            for i in range(b0, b1): kern_b[i] = 1

    both      = sum(1 for i in range(n_buck) if h2d_b[i] and kern_b[i])
    h2d_only  = sum(1 for i in range(n_buck) if h2d_b[i] and not kern_b[i])
    kern_only = sum(1 for i in range(n_buck) if kern_b[i] and not h2d_b[i])
    idle_b    = sum(1 for i in range(n_buck) if not h2d_b[i] and not kern_b[i])

    h2d_mb = sum(nb for _,_,_,k,nb in db.execute(
        "SELECT start,end,streamId,copyKind,bytes FROM CUPTI_ACTIVITY_KIND_MEMCPY"
        " WHERE copyKind=1").fetchall()) / 1e6

    stats = {
        "total_ms":        round(total_ms, 1),
        "n_queries":       len(segments),
        "h2d_mb":          round(h2d_mb, 1),
        "interleaved_pct": round(both     / n_buck * 100, 1),
        "h2d_only_pct":    round(h2d_only / n_buck * 100, 1),
        "compute_pct":     round(kern_only / n_buck * 100, 1),
        "idle_pct":        round(idle_b   / n_buck * 100, 1),
    }

    streams_ordered = sorted(streams_set,
                             key=lambda s: stream_h2d_ms[s]+stream_kern_ms[s],
                             reverse=True)
    return {
        "stats":    stats,
        "cats":     CATEGORIES,
        "segments": segments,
        "streams":  [{"id": s, "label": stream_labels[s],
                      "events": stream_events[s]}
                     for s in streams_ordered],
    }


# ── CPU profile loader ──────────────────────────────────────────────────────────
def load_cpu_profile(cpu_json_path, gpu_total_ms):
    data  = json.loads(Path(cpu_json_path).read_text())
    iters = data["iters"]

    all_tids = {}
    for it in iters:
        for tid, info in it["threads"].items():
            all_tids[tid] = info["name"]

    def total_run(tid):
        return sum(seg["e"]-seg["s"]
                   for it in iters if tid in it["threads"]
                   for seg in it["threads"][tid]["segs"] if seg["st"] in ("R","D"))

    active = sorted([t for t in all_tids if total_run(t) >= 5],
                    key=total_run, reverse=True)

    iter_ms    = gpu_total_ms / max(len(iters), 1)
    state_cat  = {"R": "cpu_run", "S": "cpu_wait", "D": "cpu_disk"}
    streams    = []

    for tid in active:
        events = []
        for ii, it in enumerate(iters):
            if tid not in it["threads"]:
                continue
            offset = ii * iter_ms
            phase  = "cold" if ii % 2 == 0 else "warm"
            for seg in it["threads"][tid]["segs"]:
                cat = state_cat.get(seg["st"], "cpu_wait")
                dur = seg["e"] - seg["s"]
                if dur < 1.0 and cat == "cpu_wait":
                    continue
                events.append({"s": round(offset+seg["s"],1),
                               "e": round(offset+seg["e"],1),
                               "c": cat,
                               "l": f"{phase} {seg['st']} ({dur:.0f}ms)"})
        if events:
            streams.append({"id": f"cpu_{tid}",
                            "label": f"CPU {all_tids[tid][:10]}",
                            "events": events, "is_cpu": True})

    return streams


# ── HTML template ───────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,sans-serif}}
body{{background:#1a1a2e;color:#e0e0e0;display:flex;flex-direction:column;height:100vh;overflow:hidden}}
#hdr{{background:#16213e;padding:8px 14px;border-bottom:1px solid #0f3460;flex-shrink:0}}
#hdr h1{{font-size:13px;font-weight:600;color:#a8dadc}}
#hdr .sub{{font-size:10px;color:#888;margin-top:1px}}
#summary{{display:flex;border-bottom:1px solid #0f3460;flex-shrink:0;height:32px}}
.sb{{flex:1;display:flex;align-items:center;justify-content:center;flex-direction:column;
     border-right:1px solid #0f3460;font-size:9px;color:#999}}
.sb .v{{font-size:14px;font-weight:700;color:#a8dadc}}
.sb.warn .v{{color:#e74c3c}} .sb.ok .v{{color:#2ecc71}}
#ctrl{{background:#16213e;padding:5px 10px;display:flex;align-items:center;gap:12px;
       border-bottom:1px solid #0f3460;flex-shrink:0;flex-wrap:wrap}}
#ctrl label{{font-size:10px;color:#aaa}}
#ctrl input[type=range]{{width:100px;accent-color:#a8dadc}}
#leg{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.li{{display:flex;align-items:center;gap:3px;font-size:9px;color:#bbb}}
.ls{{width:10px;height:10px;border-radius:2px;flex-shrink:0}}
#body{{display:flex;flex:1;overflow:hidden}}
/* ── sidebar ── */
#sidebar{{width:130px;flex-shrink:0;background:#16213e;border-right:1px solid #0f3460;
          display:flex;flex-direction:column;overflow:hidden}}
#sb-hdr{{padding:6px 8px;font-size:10px;font-weight:600;color:#a8dadc;
         border-bottom:1px solid #0f3460;flex-shrink:0}}
#sb-list{{overflow-y:auto;flex:1}}
.qbtn{{width:100%;padding:5px 7px;background:transparent;border:none;border-bottom:1px solid #0f3460;
       color:#bbb;font-size:10px;text-align:left;cursor:pointer;line-height:1.3}}
.qbtn:hover{{background:rgba(168,218,220,0.08);color:#e0e0e0}}
.qbtn.active{{background:rgba(168,218,220,0.15);color:#a8dadc;font-weight:600}}
.qbtn .qlbl{{color:#a8dadc;font-weight:600}}
.qbtn .qt{{font-size:9px;color:#888;margin-top:1px}}
/* ── canvas area ── */
#cwrap{{flex:1;overflow:hidden;position:relative;cursor:crosshair}}
canvas{{display:block}}
#tip{{position:absolute;background:rgba(8,8,24,0.95);border:1px solid #a8dadc;
      border-radius:4px;padding:5px 9px;font-size:10px;pointer-events:none;
      display:none;max-width:300px;line-height:1.5;z-index:10}}
#tip .tn{{color:#a8dadc;font-weight:600;margin-bottom:1px}}
#tip .tr{{color:#bbb}} #tip .tr span{{color:#fff}}
/* ── split mode labels ── */
#split-labels{{display:none;position:absolute;top:0;left:0;width:100%;
               pointer-events:none;z-index:5}}
.sp-lbl{{position:absolute;top:2px;font-size:11px;font-weight:600;
          padding:2px 8px;border-radius:3px}}
.sp-cold{{color:#3498db;background:rgba(52,152,219,0.15)}}
.sp-warm{{color:#e74c3c;background:rgba(231,76,60,0.15)}}
</style>
</head>
<body>
<div id="hdr"><h1>{title}</h1><div class="sub">{subtitle}</div></div>
<div id="summary">
  <div class="sb warn" title="H2D with no concurrent compute — pure scan stall">
    <div class="v" id="s0"></div><div>H2D stall</div></div>
  <div class="sb" title="H2D + kernels concurrent — pipelining working">
    <div class="v" id="s1"></div><div>Interleaved</div></div>
  <div class="sb ok" title="Pure compute, no H2D — GPU winning here">
    <div class="v" id="s2"></div><div>Compute only</div></div>
  <div class="sb" title="GPU idle — sync/overhead gaps">
    <div class="v" id="s3"></div><div>GPU idle</div></div>
  <div class="sb"><div class="v" id="s4"></div><div>H2D GB</div></div>
  <div class="sb"><div class="v" id="s5"></div><div>Invocations</div></div>
</div>
<div id="ctrl">
  <label>Zoom <input type="range" id="zoom-sl" min="0.5" max="80" step="0.1" value="1"></label>
  <label>Pan  <input type="range" id="pan-sl"  min="0"   max="100" step="0.1" value="0"></label>
  <label><input type="checkbox" id="chk-lbl" checked> Labels</label>
  <label><input type="checkbox" id="chk-grid" checked> Grid</label>
  <div id="leg"></div>
</div>
<div id="body">
  <div id="sidebar">
    <div id="sb-hdr">Queries</div>
    <div id="sb-list"></div>
  </div>
  <div id="cwrap">
    <canvas id="cv"></canvas>
    <div id="split-labels">
      <span class="sp-lbl sp-cold" id="lbl-cold">COLD</span>
      <span class="sp-lbl sp-warm" id="lbl-warm">WARM</span>
    </div>
    <div id="tip"></div>
  </div>
</div>
<script>
const DATA = {data_json};

// ── layout ──────────────────────────────────────────────────────────────────
const ROW_H  = 26, LABEL_W = 140, AXIS_H = 22, SEG_H = 16;

// ── state ───────────────────────────────────────────────────────────────────
let zoom=1, panMs=0, dragging=false, dragX=0, dragPan=0;
let splitMode=false, selGroup=null;

const cv   = document.getElementById('cv');
const ctx  = cv.getContext('2d');
const wrap = document.getElementById('cwrap');
const tip  = document.getElementById('tip');

// ── summary bar ─────────────────────────────────────────────────────────────
const st = DATA.stats;
document.getElementById('s0').textContent = st.h2d_only_pct+'%';
document.getElementById('s1').textContent = st.interleaved_pct+'%';
document.getElementById('s2').textContent = st.compute_pct+'%';
document.getElementById('s3').textContent = st.idle_pct+'%';
document.getElementById('s4').textContent = (st.h2d_mb/1024).toFixed(1);
document.getElementById('s5').textContent = st.n_queries;

// ── legend ───────────────────────────────────────────────────────────────────
const leg = document.getElementById('leg');
for (const [k,c] of Object.entries(DATA.cats)) {{
  const d=document.createElement('div'); d.className='li';
  d.innerHTML=`<div class="ls" style="background:${{c.color}}"></div>${{c.label}}`;
  leg.appendChild(d);
}}

// ── query groups ─────────────────────────────────────────────────────────────
function buildGroups() {{
  const groups=[];
  for (const seg of DATA.segments) {{
    const isWarm = seg.label.includes('warm');
    const isCold = seg.label.includes('cold');
    if (!isWarm || groups.length===0 || groups[groups.length-1].warm) {{
      groups.push({{label:seg.label.replace(' cold','').replace(' warm','').trim(),
                    cold:seg, warm:null}});
    }}
    if (isWarm && groups.length>0 && !groups[groups.length-1].warm) {{
      groups[groups.length-1].warm=seg;
    }}
  }}
  return groups;
}}
const GROUPS = buildGroups();

// ── sidebar ───────────────────────────────────────────────────────────────────
(function buildSidebar() {{
  const list = document.getElementById('sb-list');

  function btn(lbl, sub, gi) {{
    const b=document.createElement('button'); b.className='qbtn';
    b.innerHTML=`<div class="qlbl">${{lbl}}</div><div class="qt">${{sub}}</div>`;
    b.dataset.gi=gi;
    b.onclick=()=>selectGroup(gi===null ? null : GROUPS[gi], b);
    list.appendChild(b);
    return b;
  }}

  const allBtn = btn('All queries','full timeline',null);
  allBtn.classList.add('active');

  GROUPS.forEach((g,i) => {{
    const cold_s = g.cold ? ((g.cold.end-g.cold.start)/1000).toFixed(2)+'s' : '—';
    const warm_s = g.warm ? ((g.warm.end-g.warm.start)/1000).toFixed(2)+'s' : '—';
    btn(g.label, `cold ${{cold_s}} · warm ${{warm_s}}`, i);
  }});
}})();

function selectGroup(group, btnEl) {{
  document.querySelectorAll('.qbtn').forEach(b=>b.classList.remove('active'));
  btnEl.classList.add('active');

  if (!group) {{
    splitMode=false; selGroup=null;
    document.getElementById('split-labels').style.display='none';
    render();
    return;
  }}
  splitMode=true; selGroup=group;
  render();
}}

// ── helpers ───────────────────────────────────────────────────────────────────
function msToX(ms) {{
  return LABEL_W + (ms-panMs)*zoom*(cv.width-LABEL_W)/st.total_ms;
}}
function xToMs(x) {{
  return panMs + (x-LABEL_W)*st.total_ms/((cv.width-LABEL_W)*zoom);
}}
function clampPan() {{
  const vis=st.total_ms/zoom;
  panMs=Math.max(0,Math.min(panMs,st.total_ms-vis));
  const ps=document.getElementById('pan-sl');
  ps.max=Math.max(0,st.total_ms-vis).toFixed(1);
  ps.value=panMs.toFixed(1);
}}
function niceStep(a){{const m=Math.pow(10,Math.floor(Math.log10(a)));const f=a/m;
  return m*(f<1.5?1:f<3.5?2:f<7.5?5:10);}}
function fmtMs(ms){{return ms>=1000?(ms/1000).toFixed(2)+'s':ms.toFixed(0)+'ms';}}

// ── full timeline render ──────────────────────────────────────────────────────
function renderFull() {{
  const W=cv.width, H=cv.height;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#1a1a2e'; ctx.fillRect(0,0,W,H);

  const nS=DATA.streams.length;
  const contentH=AXIS_H+SEG_H+nS*ROW_H;

  // row backgrounds
  DATA.streams.forEach((st2,si)=>{{
    const y=AXIS_H+SEG_H+si*ROW_H;
    if(st2.is_divider){{ctx.fillStyle='rgba(168,218,220,0.06)';ctx.fillRect(LABEL_W,y,W-LABEL_W,ROW_H);return;}}
    ctx.fillStyle=st2.is_cpu?(si%2===0?'rgba(46,204,113,0.04)':'rgba(46,204,113,0.02)'):
                             (si%2===0?'rgba(255,255,255,0.02)':'rgba(0,0,0,0.08)');
    ctx.fillRect(LABEL_W,y,W-LABEL_W,ROW_H);
  }});

  // segment backgrounds + labels
  if(document.getElementById('chk-lbl').checked) {{
    DATA.segments.forEach(seg=>{{
      const x0=Math.max(LABEL_W,msToX(seg.start)), x1=Math.min(W,msToX(seg.end));
      if(x1<LABEL_W||x0>W) return;
      ctx.fillStyle=seg.label.includes('cold')?'rgba(52,152,219,0.06)':
                    seg.label.includes('warm')?'rgba(231,76,60,0.06)':'rgba(255,255,255,0.02)';
      ctx.fillRect(x0,AXIS_H,x1-x0,SEG_H+nS*ROW_H);
      ctx.strokeStyle=seg.label.includes('cold')?'rgba(100,180,255,0.35)':
                      seg.label.includes('warm')?'rgba(255,100,100,0.35)':'rgba(200,200,200,0.15)';
      ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x0,AXIS_H);ctx.lineTo(x0,contentH);ctx.stroke();
      if(x1-x0>24){{ctx.fillStyle='rgba(200,200,200,0.6)';ctx.font='8px monospace';
        ctx.fillText(seg.label,x0+3,AXIS_H+11);}}
    }});
  }}

  // time grid
  if(document.getElementById('chk-grid').checked) {{
    const vis=st.total_ms/zoom;
    const step=niceStep(vis/8);
    ctx.strokeStyle='rgba(255,255,255,0.05)';ctx.fillStyle='rgba(200,200,200,0.4)';
    ctx.font='8px monospace';ctx.lineWidth=1;
    for(let ms=Math.ceil(panMs/step)*step;ms<panMs+vis+step;ms+=step){{
      const x=msToX(ms); if(x<LABEL_W||x>W) continue;
      ctx.beginPath();ctx.moveTo(x,AXIS_H);ctx.lineTo(x,contentH);ctx.stroke();
      ctx.fillText(fmtMs(ms),x+2,AXIS_H-4);
    }}
  }}

  // events
  DATA.streams.forEach((st2,si)=>{{
    if(st2.is_divider) return;
    const y=AXIS_H+SEG_H+si*ROW_H+2, rh=ROW_H-4;
    st2.events.forEach(ev=>{{
      const x0=msToX(ev.s),x1=msToX(ev.e);
      if(x1<LABEL_W||x0>W) return;
      const pw=Math.max(0.5,x1-x0);
      ctx.fillStyle=DATA.cats[ev.c]?.color||'#888';
      ctx.fillRect(Math.max(LABEL_W,x0),y,Math.min(pw,W-Math.max(LABEL_W,x0)),rh);
    }});
  }});

  drawLabels(W,H,nS);
}}

// ── split view render ─────────────────────────────────────────────────────────
function renderSplit(group) {{
  const W=cv.width, H=cv.height;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#1a1a2e'; ctx.fillRect(0,0,W,H);

  const hasCold=!!group.cold, hasWarm=!!group.warm;
  const midX=hasCold&&hasWarm ? Math.floor(LABEL_W+(W-LABEL_W)/2) : W;

  if(hasCold) drawPane(group.cold, LABEL_W, midX, 'cold');
  if(hasWarm)  drawPane(group.warm, midX+(hasCold?1:0), W, 'warm');

  // divider
  if(hasCold&&hasWarm){{
    ctx.fillStyle='rgba(168,218,220,0.5)';ctx.fillRect(midX,0,2,H);
    // split labels overlay
    const sl=document.getElementById('split-labels');
    sl.style.display='block';
    const rc=cv.getBoundingClientRect();
    document.getElementById('lbl-cold').style.left=(LABEL_W+6)+'px';
    document.getElementById('lbl-warm').style.left=(midX+6)+'px';
  }}

  drawLabels(W,H,DATA.streams.length);
}}

function drawPane(seg, x0, x1, phase) {{
  const pW=x1-x0, startMs=seg.start, durMs=seg.end-seg.start;
  const mX=(ms)=>x0+(ms-startMs)/durMs*pW;
  const nS=DATA.streams.length;

  // phase tint background
  ctx.fillStyle=phase==='cold'?'rgba(52,152,219,0.04)':'rgba(231,76,60,0.04)';
  ctx.fillRect(x0,0,pW,AXIS_H+SEG_H+nS*ROW_H);

  // row backgrounds
  DATA.streams.forEach((st2,si)=>{{
    if(st2.is_divider) return;
    const y=AXIS_H+SEG_H+si*ROW_H;
    ctx.fillStyle=st2.is_cpu?(si%2===0?'rgba(46,204,113,0.04)':'rgba(46,204,113,0.02)'):
                             (si%2===0?'rgba(255,255,255,0.02)':'rgba(0,0,0,0.08)');
    ctx.fillRect(x0,y,pW,ROW_H);
  }});

  // time grid
  if(document.getElementById('chk-grid').checked){{
    const step=niceStep(durMs/6);
    ctx.strokeStyle='rgba(255,255,255,0.06)';ctx.fillStyle='rgba(200,200,200,0.5)';
    ctx.font='8px monospace';ctx.lineWidth=1;
    for(let ms=0;ms<=durMs;ms+=step){{
      const x=mX(startMs+ms); if(x<x0||x>x1) continue;
      ctx.beginPath();ctx.moveTo(x,AXIS_H);ctx.lineTo(x,AXIS_H+SEG_H+nS*ROW_H);ctx.stroke();
      ctx.fillText('+'+fmtMs(ms),x+2,AXIS_H-4);
    }}
  }}

  // events (clipped to pane)
  DATA.streams.forEach((st2,si)=>{{
    if(st2.is_divider) return;
    const y=AXIS_H+SEG_H+si*ROW_H+2, rh=ROW_H-4;
    st2.events.forEach(ev=>{{
      if(ev.e<startMs||ev.s>seg.end) return;
      const ex0=mX(Math.max(ev.s,startMs)), ex1=mX(Math.min(ev.e,seg.end));
      const pw=Math.max(0.5,ex1-ex0);
      ctx.fillStyle=DATA.cats[ev.c]?.color||'#888';
      ctx.fillRect(Math.max(x0,ex0),y,Math.min(pw,x1-Math.max(x0,ex0)),rh);
    }});
  }});

  // phase header label
  ctx.fillStyle=phase==='cold'?'rgba(52,152,219,0.2)':'rgba(231,76,60,0.2)';
  ctx.fillRect(x0,0,pW,AXIS_H);
  ctx.fillStyle=phase==='cold'?'#5dade2':'#e74c3c';
  ctx.font='bold 10px monospace';
  const dur_s=((seg.end-seg.start)/1000).toFixed(2);
  ctx.fillText(`${{phase.toUpperCase()}}  ${{dur_s}}s`,x0+6,AXIS_H-5);
}}

// ── shared label panel ────────────────────────────────────────────────────────
function drawLabels(W,H,nS) {{
  ctx.fillStyle='#16213e'; ctx.fillRect(0,0,LABEL_W,H);
  ctx.strokeStyle='rgba(255,255,255,0.1)';ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(LABEL_W,0);ctx.lineTo(LABEL_W,H);ctx.stroke();
  ctx.fillStyle='#16213e';ctx.fillRect(0,0,LABEL_W,AXIS_H);
  ctx.strokeStyle='rgba(255,255,255,0.1)';
  ctx.beginPath();ctx.moveTo(0,AXIS_H);ctx.lineTo(LABEL_W,AXIS_H);ctx.stroke();

  DATA.streams.forEach((st2,si)=>{{
    const y=AXIS_H+SEG_H+si*ROW_H;
    if(st2.is_divider){{
      ctx.fillStyle='rgba(168,218,220,0.12)';ctx.fillRect(0,y,LABEL_W,ROW_H);
      ctx.fillStyle='#a8dadc';ctx.font='bold 8px monospace';
      ctx.fillText('── CPU threads ──',4,y+ROW_H/2+3);
      ctx.strokeStyle='rgba(168,218,220,0.25)';ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();
      return;
    }}
    ctx.fillStyle=si%2===0?'rgba(255,255,255,0.02)':'rgba(0,0,0,0.08)';
    ctx.fillRect(0,y,LABEL_W,ROW_H);
    ctx.fillStyle=st2.is_cpu?'#7dcea0':'#ccc';
    ctx.font='9px monospace';
    ctx.fillText(st2.label.substring(0,20),5,y+ROW_H/2+3);
    ctx.strokeStyle='rgba(255,255,255,0.04)';ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();
  }});
}}

// ── main render dispatch ──────────────────────────────────────────────────────
function render() {{
  if(splitMode && selGroup) {{
    document.getElementById('split-labels').style.display='block';
    renderSplit(selGroup);
  }} else {{
    document.getElementById('split-labels').style.display='none';
    renderFull();
  }}
}}

function resize(){{cv.width=wrap.clientWidth;cv.height=wrap.clientHeight;render();}}

// ── interactions ──────────────────────────────────────────────────────────────
cv.addEventListener('wheel',e=>{{
  if(splitMode) return; e.preventDefault();
  const rect=cv.getBoundingClientRect();
  const foc=xToMs(e.clientX-rect.left);
  zoom=Math.max(0.5,Math.min(200,zoom*(e.deltaY<0?1.15:1/1.15)));
  panMs=foc-(e.clientX-rect.left-LABEL_W)*st.total_ms/((cv.width-LABEL_W)*zoom);
  clampPan();document.getElementById('zoom-sl').value=zoom.toFixed(2);render();
}},{{passive:false}});

cv.addEventListener('mousedown',e=>{{dragging=true;dragX=e.clientX;dragPan=panMs;cv.style.cursor='grabbing';}});
cv.addEventListener('mouseup',()=>{{dragging=false;cv.style.cursor='crosshair';}});
cv.addEventListener('mouseleave',()=>{{dragging=false;tip.style.display='none';}});

cv.addEventListener('mousemove',e=>{{
  const rect=cv.getBoundingClientRect();
  const mx=e.clientX-rect.left, my=e.clientY-rect.top;

  if(dragging&&!splitMode){{
    panMs=dragPan+(dragX-e.clientX)*st.total_ms/((cv.width-LABEL_W)*zoom);
    clampPan();render();return;
  }}

  if(mx<=LABEL_W){{tip.style.display='none';return;}}
  const si=Math.floor((my-AXIS_H-SEG_H)/ROW_H);
  if(si<0||si>=DATA.streams.length){{tip.style.display='none';return;}}
  const stream=DATA.streams[si];
  if(stream.is_divider){{tip.style.display='none';return;}}

  let tMs;
  if(splitMode&&selGroup){{
    const W=cv.width, mid=Math.floor(LABEL_W+(W-LABEL_W)/2);
    const seg=mx<mid?selGroup.cold:selGroup.warm;
    if(!seg){{tip.style.display='none';return;}}
    tMs=seg.start+(mx-(mx<mid?LABEL_W:mid+1))*(seg.end-seg.start)/((mx<mid?mid-LABEL_W:W-mid-1));
  }} else {{ tMs=xToMs(mx); }}

  let hit=null;
  for(const ev of stream.events){{if(tMs>=ev.s&&tMs<=ev.e){{hit=ev;break;}}}}
  if(!hit){{tip.style.display='none';return;}}

  const cat=DATA.cats[hit.c]||{{label:hit.c,color:'#888'}};
  const cnt=hit.n?` (×${{hit.n}} merged)`:'';
  tip.innerHTML=`<div class="tn" style="color:${{cat.color}}">${{cat.label}}</div>
    <div class="tr">Duration: <span>${{(hit.e-hit.s).toFixed(1)}} ms</span></div>
    <div class="tr">Stream: <span>${{stream.label}}</span></div>
    <div class="tr">Detail: <span>${{hit.l}}${{cnt}}</span></div>`;
  tip.style.display='block';
  tip.style.left=Math.min(e.clientX+12,window.innerWidth-320)+'px';
  tip.style.top=Math.min(e.clientY+12,window.innerHeight-100)+'px';
}});

document.getElementById('zoom-sl').addEventListener('input',e=>{{
  if(splitMode) return;
  const cx=(cv.width-LABEL_W)/2+LABEL_W,foc=xToMs(cx);
  zoom=parseFloat(e.target.value);
  panMs=foc-(cx-LABEL_W)*st.total_ms/((cv.width-LABEL_W)*zoom);
  clampPan();render();
}});
document.getElementById('pan-sl').addEventListener('input',e=>{{
  if(splitMode) return; panMs=parseFloat(e.target.value);clampPan();render();
}});
document.getElementById('chk-lbl').addEventListener('change',render);
document.getElementById('chk-grid').addEventListener('change',render);

new ResizeObserver(resize).observe(wrap);
resize();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sqlite")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--gap",          type=float, default=25.0)
    ap.add_argument("--no-coldwarm",  action="store_true")
    ap.add_argument("--cpu-profile",  default=None, metavar="JSON")
    ap.add_argument("--title",        default=None)
    args = ap.parse_args()

    sqlite_path = args.sqlite
    out_path    = args.output or (Path(sqlite_path).stem + "_timeline.html")
    title       = args.title  or Path(sqlite_path).stem

    print(f"Reading {sqlite_path} …", flush=True)
    db = sqlite3.connect(sqlite_path)
    n_kern   = db.execute("SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_KERNEL").fetchone()[0]
    n_memcpy = db.execute("SELECT COUNT(*) FROM CUPTI_ACTIVITY_KIND_MEMCPY").fetchone()[0]
    print(f"  {n_kern:,} kernel  {n_memcpy:,} memcpy events")

    payload = extract(db, gap_ms=args.gap, cold_warm=not args.no_coldwarm)

    if args.cpu_profile:
        print(f"Loading CPU profile …", flush=True)
        cpu_streams = load_cpu_profile(args.cpu_profile, payload["stats"]["total_ms"])
        payload["streams"].append({"id": "div", "label": "CPU threads",
                                   "events": [], "is_cpu": False, "is_divider": True})
        payload["streams"].extend(cpu_streams)
        print(f"  +{len(cpu_streams)} CPU thread rows")

    n_ev = sum(len(s["events"]) for s in payload["streams"])
    print(f"  {n_ev:,} segments · {len(payload['streams'])} rows · "
          f"{payload['stats']['n_queries']} invocations")

    subtitle = (f"{n_kern:,} kernels · {n_memcpy:,} memcpy · gap {args.gap}ms"
                + (" · CPU overlay" if args.cpu_profile else ""))

    html = HTML.format(title=title, subtitle=subtitle,
                       data_json=json.dumps(payload, separators=(",", ":")))
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"Written: {out_path}  ({Path(out_path).stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
