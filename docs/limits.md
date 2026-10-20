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

### `measure_token_breakdown.py`
- **By-tool attribution is turn-grain, not per-call.** A turn's `usage` is a single number
  the engine records once; a turn that calls several tools cannot have it divided exactly
  among them. The instrument groups each turn by the SET of tools it called and says so on
  every run. Read `Bash+Grep` as "the turns that called Bash and Grep together", not as a
  figure owned by either one.
- **It counts logical turns, not transcript lines.** One assistant turn is written as several
  entries that repeat the same `usage`; the instrument keys on `message.id` and counts each
  once. A future format that stops carrying a stable per-turn id would make it over-count, and
  the only signal would be a turn total larger than the session plausibly produced.
- **"By agent" means same-session sidechains**, separated by `isSidechain` and labelled by
  `attributionAgent`. It is not cross-session linkage: a child session launched as a separate
  process is invisible here unless the caller names it in a `--manifest`.
- **A `sidechain (subagent) turns: 0` line is a statement about the FILES YOU PASSED, not about
  delegation.** The
  split counts sidechain turns in the transcripts it is given, and at least one real on-disk
  layout writes each subagent's turns to a SEPARATE transcript file beside the parent's instead
  of inline. Measured across 109 real transcripts: the separate per-subagent files were 100%
  sidechain turns, and the session transcripts were 0%. Both figures are correct, and together
  they mean a caller who enumerates only session transcripts sees that zero (`"sidechain_turns": 0`
  in the digest) no matter how much work was delegated, with `by_agent` degenerating to a single
  `main thread` bucket.
  That zero is structural, not substantive. Pass the per-subagent files too (positionally, or
  named in a `--manifest`) if you want that spend attributed; the run prints this caveat next to
  the zero rather than leaving it to be inferred. Delegation to separate SESSIONS is a different
  mechanism again and belongs to the subsession rollup, not to this split -- so "0 sidechains
  but a large subsession tree" is a coherent, common reading, not a contradiction.
- **The subsession rollup is only as true as the manifest.** The caller asserts the parent/child
  tree; the instrument does not verify it. A node whose transcript is unreadable is reported as
  `n/a` and contributes nothing to its parent, never a zero -- but a WRONG parent edge sums
  silently, because nothing here can know the real shape.
- **Bash clustering is a first-token heuristic, not a parse.** It clusters a Bash turn by the
  command's first word after stripping leading `cd ...&&` chains and `VAR=` prefixes, so
  `git commit` and `git push` both land under `git`, and a command it cannot reduce to a plain
  word lands in `(unparsed)` rather than a real bucket. It does not understand pipelines,
  subshells, or what a command actually did -- read it as "what the turn started by running".
  The stripping is deliberately quote-aware and runs to a fixed point, and that is a PRIVACY
  property rather than a tidiness one: a cluster key is printed in both the text report and the
  `--json` digest, and `--anonymise` does not touch it (it hashes transcript paths and manifest
  labels, not evidence). A peel that did not know where a quoted value ends once stopped inside
  one and published a word of an env-var secret as a cluster key; the instrument's `--self-test`
  now carries a control that renders commands with quoted multi-word secret values through both
  output modes, with and without `--anonymise`, and fails if any word of a value appears in
  either. The guarantee is about the DERIVATION, not a filter on the way out: nothing downstream
  scrubs a cluster key, so the key must be incapable of carrying content in the first place.
- **The published cluster-key vocabulary is CLOSED, and that bounds what it can say.** A key is
  a word from a public-command allowlist in the instrument, `(other)`, or `(unparsed)` -- never
  anything else. This exists because the earlier guard tested only the SHAPE of a word, which
  any basename satisfies, so a locally-named script or a branch/worktree name was published
  verbatim; it was found on real data while re-vendoring this file into another repo, and the
  observed keys were internal branch, worktree and script names rather than credentials (see
  ARI#11 for the measured examples, which are deliberately not repeated here). `--anonymise`
  does not touch a key. Hashing the unrecognized word was the other candidate and was rejected:
  a command or branch name is low-entropy and its candidate space enumerable, so a hash is
  confirmable by guessing -- it obscures the name rather than removing it. Read the consequence
  both ways. Gating costs little signal where usage is ordinary -- across two independent
  surveys the public commands were the HEAD of the distribution and the bucket absorbed well
  under 1% of Bash turns -- but on a corpus that leans on local scripts `(other)` can be large,
  and then it is genuinely hiding detail from you. Its DISTINCT-COMMAND COUNT is printed and in
  the digest (`bash_other_distinct`) so you can tell one dominant unknown from twenty scattered
  ones -- cardinality, never identity. The allowlist is deliberately biased toward omission,
  since a missing word costs signal while a wrong one costs the guarantee; it is a seed list,
  not a definition of "public command", and widening it is a reviewable code change. Two
  caveats from the surveys that built it: they disagreed on their tail words, and many real
  commands appeared in exactly one session -- so it must not be frequency-thresholded, or
  rare-but-legitimate public commands would bucket.
- **The flat per-source table claims no hierarchy.** Passing several transcripts positionally
  shows each one's own total side by side; it does not infer any parent/child relationship
  between them. Use `--manifest` when a real tree exists -- the instrument never guesses one.
- **It prepares data for an LLM; it does not call one.** `--json` is a digest meant to be handed
  to a model for qualitative review. No network, no model request, no judgement of its own.

### `measure_token_breakdown.py` — the LOOP FLAGS section
- **They are flags, not verdicts, and the distinction is the whole design.** A flagged span means
  an unusual amount of work *by a threshold the caller chose*. It is not a finding that the work
  was wasted: a long span can be a correct refactor and a short one can be a pointless loop.
  Nothing here scores a session, and `--json` repeats that sentence to whatever model reads it.
  Both outcomes are published against the same pair of thresholds, so neither is a claim without
  its counterpart — `FLAGGED spans: 1 of 2` in `measure_token_breakdown.loop-flags-FLAGGED.out`
  (the fixture built to exceed them), and in `measure_token_breakdown.loop-flags-clean.out`
  (built not to):
  `FLAGGED spans: 0 of 2 -- a MEASURED clean result against this threshold`.
- **Signal 2 counts IDENTICAL calls, and "identical" means exact.** The unit is the tool name
  plus a digest of its arguments, so `Read(a)` and `Read(b)` are different calls -- which is the
  whole reason this signal could ship at all, since matching on tool name alone reports every
  Bash-heavy session as one long repetition. The cost of exactness is under-reporting: a retry
  whose arguments differ by a timestamp, a page token or a line number does not match, so a
  count here is a FLOOR, not a total. No normalisation is shipped (drop numbers? fuzzy-match
  strings?) because each candidate is a guess that would invent repeats out of legitimately
  different calls, and nothing grounds a choice between them -- the same discipline as shipping
  no threshold. Two windows are reported because neither subsumes the other: consecutive runs
  of one signature, and occurrences of one signature within a single span (`A B A B A` has no
  run longer than one but ran `A` three times before the operator said anything new). Runs are
  per THREAD, since a subagent's turns interleave into the parent's transcript and a raw-order
  scan would splice one actor's calls onto another's. Arguments are never published: they are
  compared as a digest and displayed as 8 hex characters, so this adds no privacy surface over
  the cluster keys. And a repeat is not a verdict -- polling until a condition holds, retrying
  a flaky step and re-reading a changed file all repeat legitimately, and nothing here can tell
  those from a stuck loop, because that needs state the transcript does not record.
  All three of its states are published against real runs, so none of them has to be taken on
  trust: `longest consecutive run of one signature: 8` and
  `most occurrences of one signature within a single span: 7` in
  `measure_token_breakdown.loop-flags-FLAGGED.out` (the same fixture signals 1 and 3 are pinned
  against -- its eight Bash calls carry no `command`, so they share one signature and the run is
  genuine), and `UNMEASURED: no tool call was seen at all` in
  `measure_token_breakdown.no-bash-turns.out` -- the state that must never be mistaken for a
  clean result, since "nothing could repeat" and "nothing did repeat" are different facts.
- **No threshold ships as a default, so by default nothing is flagged.** Four frameworks' own
  loop ceilings were read at first hand (`docs/citations.md`) and the defaults that exist span 10
  to 1000, each a cost backstop rather than a non-convergence detector. So an unset threshold
  prints `threshold: NOT SET -> NOT COMPUTED` — a third state, deliberately not sharing a shape
  with a clean result — while still reporting the distribution. Anything you pass is a starting
  point for recalibration, and this instrument does not know a better one.
- **Signal 1 is only as good as "which `user` entry is a person".** Most `user` entries in a real
  transcript are the engine handing back a tool result; those, `isMeta` synthetic prompts and
  engine-injected `promptSource` prompts are excluded structurally, and the counts are printed
  per reason on every run. Read them: an exclusion rule that over-fires inflates every span, and
  one that under-fires collapses them. `--survey-user-entries` prints the census for your own
  corpus. An entry this cannot classify counts as REAL, which splits a span — wrong in the
  direction that cannot manufacture a finding.
- **A transcript with no real user turn is reported `UNMEASURED: not one real user turn was found`,
  not clean.** With no boundary the whole file is one span that beats any threshold, which is the
  most convincing wrong answer available here, so nothing is flagged at all.
- **Both boundaries are strictly greater-than**, in both signals: a span of exactly the threshold
  is not flagged. A threshold below 1 is refused outright (`is below 1`) — 0 would flag every
  span including the empty ones, and a detector that fires on everything discriminates nothing.
- **Signal 3 counts streaks per thread, not in file order.** A subagent's turns are interleaved
  into the same transcript, so a raw scan would join a main-thread turn to a sidechain's and
  report a streak no thread had. The sidechain figure is still a ceiling: two different
  subagents' turns landing adjacently can merge into one apparent streak.
- **Signal 2 measures a proxy for its target, not the target.** Repeated steps rank most
  frequent of 14 in the MAST taxonomy (`docs/citations.md`, cited to the body text that ranks the
  modes rather than to the abstract, which does not rank them) — that ranking is why this signal
  was worth building rather than deferring further. But the published mode is a step repeated
  *with no state change*, and what this counts is a call repeated with identical arguments; the
  two coincide only where the arguments are the whole state. A high count is a reason to go read
  the transcript, not a failure mode detected.
- **It flags structure, not semantics.** Nothing here reads what was said; there is no LLM call
  and no notion of whether two calls achieved the same thing. "Same command run twice with no
  state change" is the signal the literature actually names, and this instrument cannot see it
  yet.

## Not evaluated at all

No sandbox or microVM platform was measured. No hosted agent rail was called. Nothing here
measures token cost, latency under concurrency, or quality of output — only what a session costs
to *hold*.
