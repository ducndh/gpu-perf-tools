# gpu-perf-tools

nsys profiling tools and generated HTML timelines for Sirius GPU engine analysis.

## nsys_timeline.py

Generates a self-contained interactive HTML timeline from an nsys SQLite export.

```bash
# Export nsys report to SQLite first:
nsys export --type=sqlite --output=profile.sqlite profile.nsys-rep

# Generate HTML:
python3 nsys_timeline.py profile.sqlite [output.html]

# Options:
#   --gap <ms>       Min GPU idle gap to detect query boundaries (default: 25)
#   --no-coldwarm    Disable cold/warm labelling
#   --title <str>    Override page title
```

Shows per-GPU-stream Gantt chart with:
- H2D transfer / D2D / decode / join / agg / other kernels — color coded
- Auto-detected query boundaries with cold/warm labels
- Summary bar: exposed H2D stall % / interleaved % / compute % / idle %
- Zoom (mouse wheel) + pan (drag) + hover tooltips

For GPU memory overlay: re-capture with `nsys profile --cuda-memory-usage=true ...`

For CPU thread view: re-capture with `nsys profile --trace=osrt,pthread ...`
then open the `.nsys-rep` in Nsight Systems GUI (the SQLite export does not
include OSRT data in a format this tool currently parses).

## profiles/

Generated HTML timelines — open directly in any browser, no install needed.

Large `.nsys-rep` files are attached to GitHub Releases (too large for git).

## Uploading new profiles

```bash
# Small HTML outputs — commit directly:
git add profiles/my_query_timeline.html
git commit -m "add Q21 SF=300 timeline"
git push

# Large .nsys-rep files — attach to a GitHub Release:
gh release create v<date> --title "SF=300 profiles" \
  /tmp/all22_sf300.nsys-rep
```
