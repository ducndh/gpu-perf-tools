#!/usr/bin/env python3
"""
build_experiment_index.py — Generate a per-experiment index.html for a
multi-config profile dir.

Layout it expects:

  profiles/<exp_dir>/
    metadata.json         (kind="experiment"; lists configs + queries)
    nsys_summary.csv      (config,query,kernel_total_ms,sync_total_ms,...)
    <config>/
      q<N>/q<N>.nsys-rep

Output: <exp_dir>/index.html — query x config matrix with kernel/sync/h2d
per cell + per-cell link to the .nsys-rep file. Plus an aggregate row.

Usage:
  python3 build_experiment_index.py <exp_dir>
"""

from __future__ import annotations
import csv
import json
import sys
from pathlib import Path


HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,sans-serif}}
body{{background:#1a1a2e;color:#e0e0e0;padding:20px 28px}}
h1{{font-size:20px;color:#a8dadc;margin-bottom:4px}}
h2{{font-size:14px;color:#a8dadc;margin:18px 0 6px}}
.sub{{font-size:12px;color:#888;margin-bottom:12px}}
.note{{font-size:12px;color:#bbb;line-height:1.5;margin-bottom:14px;
       max-width:900px;background:#0f3460;padding:10px 14px;border-radius:4px}}
table{{width:100%;border-collapse:collapse;font-size:11px;font-family:monospace}}
th{{background:#16213e;color:#a8dadc;padding:6px 8px;text-align:right;
   border-bottom:2px solid #0f3460;white-space:nowrap;font-weight:normal}}
th.left{{text-align:left}}
td{{padding:5px 8px;border-bottom:1px solid rgba(255,255,255,0.05);
    text-align:right;white-space:nowrap}}
td.left{{text-align:left}}
tr:hover td{{background:rgba(168,218,220,0.05)}}
.cfg-native{{color:#bbb}}
.cfg-enc_region{{color:#f1c40f}}
.cfg-dec_region{{color:#2ecc71}}
.win{{color:#2ecc71;font-weight:bold}}
.loss{{color:#e74c3c}}
.muted{{color:#666}}
a{{color:#a8dadc;text-decoration:none}}
a:hover{{color:#fff;text-decoration:underline}}
.commit{{color:#e67e22;font-family:monospace}}
.branch{{color:#a8dadc;font-family:monospace}}
.gpu{{color:#9b59b6;font-size:11px}}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="sub">
  <span class="branch">{branch}</span> @ <span class="commit">{commit}</span> ·
  {date} · {machine} · <span class="gpu">{gpu}</span> · SF={sf}
</div>

<div class="note">{notes}</div>
"""

FOOT = """
<div style="margin-top:16px;font-size:10px;color:#444">
  Each .nsys-rep is a single warm iteration captured between profiler_start and
  profiler_stop. Open in Nsight Systems GUI for full thread/kernel detail.
  Summary CSV: <a href="nsys_summary.csv">nsys_summary.csv</a>
</div>
</body>
</html>
"""


def fmt_speedup(ratio: float) -> str:
    if ratio >= 1.05:
        cls = "win"
    elif ratio <= 0.95:
        cls = "loss"
    else:
        cls = "muted"
    return f'<span class="{cls}">{ratio:.2f}x</span>'


def render_aggregate(configs: list[str], aggregates: dict, n_q: int) -> str:
    native_kernel = aggregates.get("native", {}).get("kernel_ms", 0)
    rows = []
    for cfg in configs:
        a = aggregates[cfg]
        sk = a["sync_ms"] / a["kernel_ms"] if a["kernel_ms"] else 0
        h2d_mb = a["h2d_b"] / (1024 * 1024)
        speedup = ""
        if native_kernel and a["kernel_ms"]:
            ratio = native_kernel / a["kernel_ms"]
            speedup = fmt_speedup(ratio)
        rows.append(
            f'<tr><td class="left cfg-{cfg}">{cfg}</td>'
            f'<td>{a["kernel_ms"]:.1f}</td>'
            f'<td>{a["sync_ms"]:.1f}</td>'
            f'<td>{sk:.2f}</td>'
            f'<td>{h2d_mb:.1f}</td>'
            f'<td>{speedup}</td></tr>'
        )
    return f"""
<h2>Aggregate sums across {n_q} queries (configs that all completed)</h2>
<table style="width:auto">
<thead><tr>
  <th class="left">config</th>
  <th>kernel_total_ms</th>
  <th>sync_total_ms</th>
  <th>sync/kernel</th>
  <th>h2d_total_MB</th>
  <th>kernel speedup vs native</th>
</tr></thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
"""


def render_matrix(configs, queries, rows_by_key, layout) -> str:
    # Two-row header: top = config name (4-col span), bottom = sub-headers.
    top_th = "".join(
        f'<th colspan="4" class="cfg-{cfg}" style="text-align:center">{cfg}</th>'
        for cfg in configs
    )
    sub_th = "".join(
        '<th>kernel_ms</th><th>sync_ms</th><th>h2d_MB</th><th>.rep</th>'
        for _ in configs
    )
    head = (
        f'<tr><th class="left" rowspan="2">query</th>{top_th}</tr>'
        f'<tr style="background:#0f3460">{sub_th}</tr>'
    )

    body = []
    for q in queries:
        cells = [f'<td class="left">q{q}</td>']
        for cfg in configs:
            r = rows_by_key.get((cfg, q))
            if r is None:
                cells.extend(['<td class="muted">—</td>'] * 4)
                continue
            kernel_ms = float(r["kernel_total_ms"])
            sync_ms = float(r["sync_total_ms"])
            h2d_mb = int(r["h2d_bytes"]) / (1024 * 1024)
            rep_path = layout.replace("<config>", cfg).replace("<N>", str(q))
            cells.extend([
                f'<td>{kernel_ms:.1f}</td>',
                f'<td>{sync_ms:.1f}</td>',
                f'<td>{h2d_mb:.2f}</td>',
                f'<td><a href="{rep_path}">.rep</a></td>',
            ])
        body.append("<tr>" + "".join(cells) + "</tr>")

    return f"""
<h2>Per-query x per-config matrix</h2>
<table>
<thead>{head}</thead>
<tbody>
{"".join(body)}
</tbody>
</table>
"""


def main():
    if len(sys.argv) < 2:
        print("usage: build_experiment_index.py <exp_dir>", file=sys.stderr)
        sys.exit(1)
    exp_dir = Path(sys.argv[1])
    meta = json.loads((exp_dir / "metadata.json").read_text())
    csv_path = exp_dir / meta.get("summary_csv", "nsys_summary.csv")

    rows_by_key: dict[tuple[str, int], dict] = {}
    with csv_path.open() as f:
        for r in csv.DictReader(f):
            rows_by_key[(r["config"], int(r["query"]))] = r

    configs = meta["experiment"]["configs"]
    queries = meta["experiment"]["queries"]

    valid_q = [q for q in queries if all((cfg, q) in rows_by_key for cfg in configs)]
    aggregates = {}
    for cfg in configs:
        a = {"kernel_ms": 0.0, "sync_ms": 0.0, "h2d_b": 0}
        for q in valid_q:
            r = rows_by_key[(cfg, q)]
            a["kernel_ms"] += float(r["kernel_total_ms"])
            a["sync_ms"] += float(r["sync_total_ms"])
            a["h2d_b"] += int(r["h2d_bytes"])
        aggregates[cfg] = a

    layout = meta.get("nsys_layout", "<config>/q<N>/q<N>.nsys-rep")

    head = HEAD.format(
        title=meta.get("label", exp_dir.name),
        date=meta.get("date", "?"),
        machine=meta.get("machine", "?"),
        gpu=meta.get("gpu", "?"),
        branch=meta.get("branch", "?"),
        commit=meta.get("commit", "?"),
        sf=meta.get("scale_factor", "?"),
        notes=meta.get("notes", ""),
    )
    page = head + render_aggregate(configs, aggregates, len(valid_q)) \
                + render_matrix(configs, queries, rows_by_key, layout) \
                + FOOT

    out = exp_dir / "index.html"
    out.write_text(page)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
