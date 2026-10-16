# Limits — what these instruments cannot tell you

Stated so the gaps are visible rather than implied absent.

## About the figures in this repository

- **One box, one day.** A 16 GB Linux box, Claude Code 2.1.197, Codex 0.147.0, 2026-10-16. Every
  number is a reading, not a constant.
- **The box was heavily loaded**, and it shows: within one three-rep run the full-turn arm spans
  `min=1.96s  max=3.65s`, and time-to-usable-MCP-tool read
  `min=5.22s  max=6.97s  spread=1.75s` on n=2. Treat all timings as single-digit-second
  magnitudes, not values.
- **This paragraph carried four stale figures through a review round.** They were corrected in
  `ARTICLE.md` and missed here — in the document whose entire job is honesty about readings. Two
  of the four existed in no sample at all. `scripts/check_figures.sh` now pins this file's quoted
  timings too: 24 pairs covering only README and ARTICLE left the gate green straight across the
  defect, which is what an admitted coverage limit looks like when it goes unaddressed.
- **Two different corpora appear in this repo and must not be mixed.** The source study's
  concurrency figures (146 transcripts, 97 sessions, mean 0.43 active) came from two transcript
  roots; the sample here (`264 transcripts scanned`,
  `sessions with any activity in 7d: 42`, `mean=0.15`, `busy_minutes=964/10080`) uses only the
  vendor-default root. Same instrument, different populations, so different numbers. The *shape* reproduces — a
  low active mean against a much larger resident count — the integers do not.
- **Codex was unauthenticated** (HTTP 401), so its boot figure is boot-to-first-event, and its
  capability set was read from `--help`, not exercised.
- **The caching result is n=1 per arm.** Arm 5 ran in three of the four published runs and the
  relation held in all three. That is three data points, not a distribution.
- **Arm 4 of the caching instrument has an unverified premise.** It perturbs the prefix by
  switching model, assuming caches are keyed per model. The cited documentation does not state
  that (see `docs/citations.md` #2), and the arm held in only one of the three runs where the
  current criterion was applied — on a shared box other processes keep the alternate model's
  cache warm. It is published as a control we no longer trust. Both outcomes are in `samples/`:
  `PRIMARY (hit-fraction) -> control HOLDS` in `bench_cache_residency.out`, and
  `PRIMARY (hit-fraction) -> control FAILED` in `bench_cache_residency.run3-redacted.out`.
  An independent reviewer's own run also HELD, which is why the tally is "two of five observed"
  rather than "uniformly failed".

## About the instruments themselves

### `measure_session_footprint.py`
- Sees only process shapes matching its patterns. `--list-unmatched` is the check; without
  running it, a zero is not interpretable.
- `/proc` under `hidepid=2`, or a container not sharing the host PID namespace, hides other
  users' trees **silently**. The instrument cannot detect this; you have to know.
- A tree with any member whose `smaps_rollup` is unreadable reports PSS `n/a` and is **omitted
  from the summed PSS**, which is therefore a floor. The count of such trees is printed.

### `measure_active_concurrency.py`
- A transcript records a turn that *wrote something*. A session sitting open with a live process
  and nothing being written — the thing that costs money — leaves no trace here. `/proc` is the
  authority for residency; this instrument is deliberately not.
- "Active" is minute-granular and gap-filled by assumption. Both series are printed so the
  assumption's effect is visible.
- One session = one containing directory. A harness that stores several sessions' transcripts in
  one directory, or one session's across several, will be counted wrongly, and the instrument
  cannot tell.

### `bench_cold_start.py`
- `boot` stops at the first stream event, before any tool is callable. It is a floor by
  construction.
- `procs=3` in the benchmark against ~6 in a real session: the benchmark process tree is
  smaller than a live session's, so its peak RSS is also a floor.
- Measures the CLIs on `PATH`. A missing binary is `SKIPPED` **visibly**, never reported as zero
  (`samples/bench_cold_start.no-binaries.out`).

### `bench_mcp_connect.py`
- The bundled echo server does **no work**, so the default figure is the protocol floor —
  connect, handshake, dispatch. A real server's cost can dominate it. Point `MCP_TOOL` at a real
  server to measure that; the two numbers are not interchangeable, and the run labels which it
  used.
- The model must choose to call the tool. A run that ends without the call prints `NEVER-CALLED`
  — a different finding from a failed connect, distinguished by the `init` server-status line.
- Other MCP servers configured on the box appear in `init` and affect timing. One of our two reps
  had a third server appear; that is most of the `spread=1.75s`.

### `census_background_work.py`
- Sees a session's own cgroup. Work handed to a systemd unit, another cgroup, or a remote host is
  invisible **by design**.
- **Where several sessions share one cgroup** — a multiplexer rail, e.g. all sessions under one
  tmux service — work is detected but **cannot be attributed per session**. It is reported as
  rail-level. A shared cgroup is flagged by counting session *root* entrypoints.
- A rail is discovered only while it holds a live agent entrypoint. This is correct, but it means
  a rail whose sessions have all exited is simply absent rather than reported empty.
- One instant, not a distribution. A dev server started a minute ago is below `MIN_AGE_S`.
- **Without per-session cgroups it degrades rather than refusing**, which is the case on any
  ordinary local machine. In that mode it walks process parentage — this instrument's own v1
  method, which under-counted — exits **4** rather than 0, and prints a `FLOOR DEMONSTRATION`:
  it plants a decoy that genuinely reparents to pid 1 and shows the parentage walk failing to find
  it. So the degraded zero is labelled a floor by live evidence, not by a caveat.
  `samples/census_background_work.degraded.out`.
- Three versions gave three answers (0 of 16, 2 of 15, 0 of 15). The third is the first with a
  control that can fail. That is the reason to believe it — not that it agrees with the first.

### `bench_cache_residency.py`
- Spends ~6 model requests per run.
- Arms 1 and 2 are **not prefix-identical**: the resident arm runs both turns in one process, so
  by turn 2 it has a longer cached prefix. "Resident read X vs fresh read Y" is not a
  like-for-like comparison and must not be quoted as residency being cheaper.
- Arm 3 is a published **failed** control (suffix, not prefix, perturbation).
- Documented caching behaviour this instrument does not test: a cache entry "only becomes
  available after the first response begins", so N parallel workers on an identical prefix all
  pay full price; and the lookback window is 20 *positions*, which a tool-heavy turn can exceed.

## Not evaluated at all

No sandbox or microVM platform was measured. No hosted agent rail was called. Nothing here
measures token cost, latency under concurrency, or quality of output — only what a session costs
to *hold*.
