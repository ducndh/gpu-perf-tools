# DuckDB-native GPU scan — I/O path performance

Performance analysis for the GPU-native `.duckdb` scan (merged to sirius `dev` 2026-05: scan operator + decode kernels for numeric / bitpacking / RLE / ALP / strings).

## Environment

- GPU: NVIDIA A100-SXM4-40GB (sm_80, 40 GB HBM2e), driver 580.126.09
- Host: 64 GB RAM, CUDA 12.9 (pixi)
- Method: session-mode (one process, all queries), untimed warmup, median of 2 reps. CPU column = DuckDB with the GPU extension disabled.
- Cold = `posix_fadvise(POSIX_FADV_DONTNEED)` on the data file before each variant.

## Baseline runtimes (GPU-native scan, `pread` → pageable → `cudaMemcpyAsync`)

| Benchmark | GPU warm | GPU cold | DuckDB CPU |
|---|---|---|---|
| TPC-H SF=10 (22 queries) | ~7.7–9.3 s | ~10.6–11.2 s | ~2.6–4.0 s |
| TPC-H SF=100 (22 queries) | ~64–66 s | ~92–96 s | ~15–20 s |
| TPC-H SF=100, 5 GiB scan batch | ~84–89 s | — | ~14–15 s |
| ClickBench 10M-row subset | ~5.7–6.0 s | ~7.0–7.3 s | ~0.8–0.9 s |
| ClickBench-full (25 GB) | ~40–41 s | ~57–60 s | ~5 s |

TPC-H is 22/22 byte-identical to DuckDB CPU. ClickBench DIFFs are pre-existing GROUP BY ordering non-determinism (present on the CPU path too).

## I/O variant comparison (ratio to the pageable baseline; < 1.0 = faster)

Three implementations of the scan's host-to-device staging, same source tree:

- **baseline** — `pread` into pageable heap, then `cudaMemcpyAsync` (driver makes pageable copies synchronous).
- **io_uring (`O_DIRECT`)** — async device reads through the io_uring reactor with a per-transfer completion callback.
- **bounce slab** — `pread` into a thread-local 2-slot pinned ring (16 MB/slot), then true-async `cudaMemcpyAsync`.
- **io_uring (fixed)** — the reactor with the two changes below.

| benchmark | bounce slab | io_uring (fixed) | io_uring (`O_DIRECT`, first cut) |
|---|---|---|---|
| TPC-H SF=10 warm | 0.69–0.83× | 0.75× | 2.79–3.32× |
| TPC-H SF=100 warm | 0.73–0.83× | 0.79× | 3.85–3.97× |
| TPC-H SF=100 5gi warm | 0.67× | 0.64× | 3.13–3.34× |
| ClickBench warm | 0.85× | 0.86× | 1.97–2.04× |
| ClickBench-full warm | 0.82–0.95× | 0.90× | 3.10–3.26× |
| TPC-H SF=10 cold | 0.76–0.78× | 0.73× | 2.47–2.58× |
| TPC-H SF=100 cold | 0.76–0.86× | 0.80× | 2.85–3.02× |
| ClickBench cold | 0.96–1.06× | 0.88× | 1.70–1.75× |
| ClickBench-full cold | 0.95–0.98× | 0.95× | 2.23–2.32× |

## Why the first io_uring cut was slow (nsys, q6 TPC-H SF=100 warm)

| Metric | baseline | io_uring (first cut) |
|---|---|---|
| `cudaMemcpyAsync` API time | 1473 ms | 579 ms (−61%) |
| `pread` syscall time | 18.2 s | 10.9 s (−40%) |
| `cudaStreamAddCallback` | none | 534 ms / 24,419 calls |
| scan-op time per task | 138 ms | 1,100 ms (8×) |

io_uring did its job on the read and the copy. The regression was entirely a **host function stitched into the CUDA stream per H2D transfer** (`cudaStreamAddCallback`, used to recycle pinned slots): ~25k callbacks blocked kernel dispatch for ~534 ms/query — about 6× the actual kernel time.

(The underlying nsys `.nsys-rep` captures were on ephemeral scratch and have since been wiped — re-profile with `capture.sh` if the raw traces are needed again.)

## The two fixes that recover it

1. Replace the per-transfer `cudaStreamAddCallback` with a single `cudaEventRecord` + `cudaEventQuery` poll for slot reclamation — no host code in the stream. Preserves error-state reclamation (`cudaEventQuery` returns the stream's sticky error).
2. Open the device fd buffered instead of `O_DIRECT`, recovering page-cache hits on warm workloads. Keep `O_DIRECT` as an opt-in for cold / working-set-larger-than-RAM cases.

With both, io_uring matches or beats the bounce slab and is faster than the pageable baseline in every measured configuration. The substrate was sound; the per-transfer callback was the cost.

## Cold-cache note

"`O_DIRECT` should win on cold reads" does **not** hold at SF=100 on 64 GB RAM: only the first query is truly cold, the OS re-caches what it touches, and `O_DIRECT` keeps paying disk cost on every subsequent query while the cached `pread` baseline gets hits. A genuine cold win needs a working set larger than RAM across the whole sweep (≈ SF=300, ~75 GB).
