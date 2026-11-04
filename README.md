# agent-runtime-instruments

**Measure what a coding-agent session actually costs — and don't let the measurement lie to you.**

Six read-only instruments, no dependencies, standard-library Python. Plus the thing that is
actually rare: **every control that failed, published alongside the ones that worked.**

---

## Start with the question you have

### "What is my agent costing me?" — one person, one machine

```bash
./what-is-my-agent-costing-me
```

About a minute. No setup, no database, no model requests spent. Answers:

- RAM per session, **RSS *and* PSS** — read PSS if you want to know what one more session costs,
  because the agent binary's pages are shared and RSS counts them once per session.
- **How much of the last 7 days your agent was actually working**, versus sitting open holding a
  process. Measured from your own transcripts. Almost nobody measures this about themselves, and
  it is usually the surprising one.
- How long a fresh agent process takes to be ready.
- Whether an agent left something running that you have forgotten about.

### "Can I pool this?" — building a system on agents

```bash
./can-i-pool-this          # SKIP_PAID=1 to run only the free parts
```

Spends ~8-10 cheap model requests. The four facts a pool design stands on:

- The **concurrency distribution** that sizes the pool — active sessions against resident ones.
- Per-session resident cost in **PSS**, which is what capacity arithmetic needs.
- Whether a **prompt-cache entry survives the process that wrote it** — because if it does not,
  turn-scoping has a per-turn token cost. It does. Measured token-exactly; see below.
- What a turn pays to start, and **where those seconds actually go** (mostly inference, not tool
  connect).

---

## The idea worth taking even if you never run these

Every instrument here answers one question in its own docstring and under `--help-blind`:

> **What would this print if it were blind?**

For three of the six, the answer is *"exactly what it prints when the finding is genuinely
zero."* An instrument like that has no business being believed until something distinguishes the
two cases. That question, plus publishing the controls that failed, is this repository's actual
contribution — the measurements are examples of it.

It is not a slogan. It found four real defects in these instruments, each of which had already
produced a confident, plausible number. They are written up in **[`docs/method.md`](docs/method.md)**
with the reproduction for each.

---

## The instruments

| File | Answers | Cost |
|---|---|---|
| `measure_session_footprint.py` | RAM per live session, RSS and PSS | free, `/proc` |
| `measure_active_concurrency.py` | mid-turn vs resident concurrency | free, reads transcripts |
| `bench_cold_start.py` | boot to ready, and to a finished turn | boot free; `turn` = 1 request |
| `bench_mcp_connect.py` | time to a **usable** MCP tool | 1 request per rep |
| `census_background_work.py` | work a turn boundary would kill | free, **self-testing** |
| `bench_cache_residency.py` | does a cache hit need the writer alive? | ~6 requests |
| `measure_token_breakdown.py` | where a session's tokens went, by tool / subagent / subsession, plus mechanical loop flags | free, reads transcripts |
| `flags.py` | the same detectors as **named, callable flag functions** over a *recent window* of one transcript, for a caller polling a live session | free, reads part of one transcript |
| `bench_window_read.py` | what that windowed read actually costs vs parsing the whole file | free, reads transcripts |
| `measure_request_construction.py` | what a client **puts in every request** — the fixed per-turn part, and which of it is billed | free, reads a capture |

`instruments/echo_mcp_server.py` is a 60-line stdio MCP server with one tool, so
`bench_mcp_connect.py` runs out of the box without access to anyone's private servers.
`instruments/capture_proxy.py` is the same idea for `measure_request_construction.py`: a
loopback endpoint that records what a client sends and never calls a provider, so a capture
costs $0.00 by construction. `instruments/drive_dsh.py` drives one DeepSeek Harness turn
headlessly, which nothing else can do — see the engine table below for why.
`instruments/engines.py` is the engine adapter table — see below.

All take `--help` and `--help-blind`; most take `--anonymise`, which hashes session identifiers
so output is safe to publish and **never alters a token count or a timing**.

`measure_token_breakdown.py` also carries two **mechanical loop flags** — tool calls per span
between consecutive real user turns, and runs of assistant turns that called no tool — computed
from transcript structure, never from judging what was said. They are **flags, not verdicts**,
and they ship **no default threshold**, because no published one is grounded — the agent
frameworks' own loop ceilings span 10 to 1000, each read at first hand and sourced in
`docs/citations.md`. So an unset threshold prints `threshold: NOT SET -> NOT COMPUTED` rather
than a reassuring clean result. Set one with `--flag-span-tool-calls N` /
`--flag-no-tool-streak N`, or the matching `ARI_FLAG_*` env vars; a value below 1 is refused.
See `samples/measure_token_breakdown.loop-flags-FLAGGED.out` for a flagged run.

Read-only, with one disclosed exception: `census_background_work.py`'s self-test spawns short-lived
decoy processes of its own and kills them again.

### `flags.py` — the same detectors, on recent activity, for an outside caller

Everything above runs **once, over a whole transcript**. That is right for a retrospective
study and wrong for a process that wants to look at a live session every few turns, which is
what `instruments/flags.py` adds: a **windowed read path** plus the detectors re-exposed as
**named, independently callable functions with one output shape**. The module docstring is the
full contract (`python3 instruments/flags.py --help-blind` prints it); this is the short version.

**Read a window.** Pass exactly one of these, or none for the whole file:

```python
w = F.read_window(path, last_turns=40)    # the newest 40 logical assistant turns
w = F.read_window(path, since="2026-10-20T12:00:00Z")
w = F.read_window(path, from_offset=K)    # everything appended since byte K
```

`from_offset` is the one built for a polling caller: keep `w["window"]["next_offset"]` and pass
it back next time, and the read costs what the session *appended*, not what it has accumulated.
A file that has not grown returns an empty window; a file **shorter** than the cursor is treated
as truncated or replaced and falls back to a full read rather than reporting an idle session.

**Then call the flags.** Three, each citing where its logic came from rather than re-deriving it:

| Flag | Fires on | Logic from |
|---|---|---|
| `batchable_bash` | consecutive single-Bash turns that could have been **one** call | alm#831's `witnesses()`/`jaccard()` dependency proxy, used as-is |
| `repeated_invocations` | one identical tool+arguments signature, over and over | this repo's own signal 2 in `measure_token_breakdown.py` |
| `agent_reuse_gap` | a long run of delegable-looking main-thread work with **no** subagent dispatch | alm#833's three-class delegability proxy and its dollar-derived break-even |

Every flag returns the same dict. Four fields decide what a caller does:

- **`state`** — `"measured"` / `"not_computed"` / `"unmeasured"`. **Read this first.** `fired:
  False` is a trustworthy clean result *only* when state is `"measured"`; under the other two it
  means nothing, and a watchdog that treats `"unmeasured"` as healthy will read a dead session as
  a calm one. `all_flags()` returns the `unmeasured` list at the top so it is hard to miss.
- **`measure`** vs **`threshold`** — a flag fires when `measure > threshold`, strictly greater,
  for all three. `measure` is *not* always `len(evidence)`: `measure_of` says what it counts.
- **`evidence`** — fingerprints, labels and counts. **Never a raw argument**: Bash commands become
  closed-vocabulary `Bash:<cluster>` labels plus a digest, and alm#831's literal dependency
  witnesses are reported only as a *count*. These flags are built to be shipped to a monitoring
  process that may log them.
- **`window`** — the provenance of what was read. When `spans_are_local` is true the window has a
  horizon and **every count is a floor**: a run that began earlier is seen only from the window's
  start, and a span index from a window must never be compared with one from a full read.

**No flag ships a default threshold**, for the same reason the loop flags above don't. With none
set, you get real counts and `state: "not_computed"`.

**Samples, one per outcome a reader must be able to tell apart** — the point being that four of
these five are *not* "nothing flagged", and a caller that cannot distinguish them has no signal:

| Sample | What it shows |
|---|---|
| `samples/flags.FIRED.out` | `FIRED -- safely-mergeable consecutive single-Bash-call pairs` |
| `samples/flags.clean.out` | not flagged, and it is `a MEASURED clean result` — trustworthy |
| `samples/flags.no-threshold.out` | `NOT_COMPUTED: no threshold given`, with the counts still real |
| `samples/flags.unmeasured.out` | `UNMEASURED: no tool call was seen at all in the window` — nothing to look at, **not** a clean result |
| `samples/flags.agent-gap-gated.out` | the gap *exceeded* its threshold and still did not fire: the longest content run was `below the 3-turn break-even`, so the work was orchestration and could not have moved |

`samples/flags.windowed.out` is the same detector over a 4-turn window, and prints
`SPANS ARE LOCAL to this window` — the warning that every count in it is a floor.
`samples/flags.json.out` is the shape a caller consumes.

#### What the window actually costs

Measured, not assumed, by `instruments/bench_window_read.py`. The full capture is
`samples/bench_window_read.out`, over a `corpus: 10 transcripts >= 524288 bytes, largest first`
on one box. Reading the newest 40 turns instead of the whole file, each line below quoted from
that artefact. `median`, `best` and `worst` are across that corpus — deliberately not tied to
a named file, because the corpus is whatever happens to be largest at the moment of the run
and the largest transcript in this one is excluded from the medians anyway (see the residual
below):

```
   WINDOW / FULL, bytes read    median 0.237   best 0.075   worst 0.387
   WINDOW / FULL, lines parsed  median 0.271   best 0.080   worst 0.414
   WINDOW / FULL, wall-clock    median 0.139   best 0.052   worst 0.270
```

The **resume** path — the one a polling caller actually uses — is cheaper again, because it
reads only what arrived rather than a fixed window:
`RESUME / FULL, bytes read    median 0.097   best 0.046   worst 0.161`. In absolute terms over
those files: `bytes:                FULL 43.7 MB, WINDOW 8.0 MB, RESUME 3.7 MB`, and
`lines fed to json.loads: FULL 16307, WINDOW 3336, RESUME 1486`.

**Quote a row, not the median.** An independent re-run on the same box reproduced every
per-file ratio to three decimals and got a different corpus median, because the median depends
on which sessions happen to be largest at the moment of the run — the instrument says so in its
own output. The per-file ratios are the reproducible figure.

One residual, measured rather than described, because it is a property of the *shared* turn
parser and not of the window: `collect_turns` keys a logical turn by `message.id` and merges
every entry carrying it, which is right for the split turn it was built for. A **resumed**
session can replay the same ids much later in the same file — on one real local transcript,
769 entries apart — and then the full read collapses both occurrences into one turn while a
window holding only the later one does not. **4 of 151** windowing comparisons over 45 real
transcripts, confined to the resumed files. That is a rate for "the two reads do not line up",
not for "the window is wrong": the window's own counts are internally consistent, and they are
what every flag is computed from. The benchmark labels such a file `RESUMED TRANSCRIPT, DISCLOSED RESIDUAL` rather than
`MISMATCH`, so a documented residual cannot occupy the slot reserved for an unexplained
defect — on the current corpus that is the single largest transcript, which is why it is
excluded from the figures above. The self-test reproduces it from a deliberate fixture.

Two things that *were* contract violations, found by review and now gone — both verified over
those 151 comparisons at eight window sizes: `last_turns=N` could **return more than N turns**
(it returned 437 for N=400, because the cut was chosen by turn identity and a replayed id
dragged it back to the file's start), and `read_fraction` could **exceed 1** — up to `1.934`, a
"window" charging for nearly twice the file, partly from that and partly from the growth loop
re-reading. The cut is positional now and the loop extends its buffer backwards instead: 0
over-returns and a maximum `read_fraction` of exactly `1.000`.

Four honest limits on those figures, each of which the instrument reports rather than hides.
**Wall-clock is the weakest of the three** — it carries one box's load and page cache, so when
the timing ratio and the byte ratio disagree, believe the bytes. **The saving is proportional to
how much of the session the window is not reading**, so a session shorter than the window shows
no saving, and those rows are printed rather than filtered out. **A live session can grow
between the two reads**, in which case the window correctly sees a turn the full read never did;
such a row is marked `GREW DURING MEASUREMENT` and excluded, because that is not the read-path
disagreement the `MISMATCH` label is reserved for — a control forces the case deterministically
(`a file that grew mid-measurement`, in `samples/bench_window_read.selftest.out`), since whether
a live run happens to catch a session mid-turn is luck. And **the first tail read's size is itself a
tuned number**: at 256 KB, 10 of 10 transcripts needed a second read
(`        256K        10.00      20           10 of 10`), so `flags.FIRST_CHUNK` is 1 MB
(`       1024K        10.00      10            0 of 10` — one read per file, no growth step).
Extending the buffer rather than re-reading has since flattened that curve, so the sweep in
`samples/bench_window_read.first-chunk-sweep.out` now makes 128 KB marginally cheaper in bytes
and markedly more expensive in syscalls; 1 MB is kept for the property that is stable. It is a per-call parameter, because
the optimum is a property of how many bytes a turn takes in your own corpus.

### `measure_request_construction.py` — what the client *constructs*, not what the transcript says

Everything above reads a session after the fact. This reads the other end: the request bodies a
client actually puts on the wire, captured by `instruments/capture_proxy.py` before any provider
sees them. No upstream call is ever made, so a capture costs nothing — and nothing that needs the
model to make progress (compaction, context growth) is measurable this way. What *is* measurable
is the **fixed part of every turn**: the system prompt plus the tool schemas.

Two real captures are committed as fixtures, one per newly-verified engine, measured at a
non-repository working directory on one box:

| | DeepSeek Harness (`sdk-minimal`) | Hermes (custom OpenAI provider) |
|---|---|---|
| wire bytes, first request | `8225` | `48820` |
| fixed per-turn part (system + tools) | `fixed 1665 bytes` | `49426` B |
| tools exposed | 3 | 20 |
| model-input bytes | `model input 2161 bytes` | `49655` B |
| provider metadata | `6519 bytes (75.1%) of provider metadata` | none at all |

In the one sentence the instrument's own `--check-claims` pins: the dsh arm's first request put
`8225 bytes on the wire`, of which `fixed 1665 bytes` is the system prompt plus three tool schemas
that every later turn pays again, `model input 2161 bytes` is what a per-token bill is computed
from, and `6519 bytes (75.1%) of provider metadata` is charged to the network and to nothing else.

**The right-hand column of that table is why the instrument exists.** `dsh` sends two top-level
fields that are not part of the Anthropic API — `dsh_session_log` and `dsh_plugin_packages` — and
on this capture they are **75.1% of the body**. The vendor's own installed documentation says both
cost *"Zero model-input tokens"* and *"only increase HTTP request bytes"*; those quotations are in
`instruments/measure_request_construction.py` beside the set that encodes them. Counting them as
model input is not a rounding error: in the study this was ported from it inverted the published
headline's sign, from *cheaper* to *more expensive*, on identical bytes.

So a decomposition that knows only `system`/`tools`/`messages` would have reported a quarter of
that payload as the whole request, printing a complete-looking table with no symptom. The
instrument therefore checks its own split against the JSON structure: per-field bytes **plus
framing predicted from the keys** must equal the whole body exactly, and an unknown top-level field
exits 4 naming itself rather than vanishing into a `.get()`.

Three things it refuses to do, each with a committed artefact:

| Sample | What it shows |
|---|---|
| `samples/measure_request_construction.probes-only-NO-MEASUREMENT.out` | a **real** capture of a run in which the agent never constructed a turn — only Hermes's two startup probes. Counting capture *lines* would have called that a measurement; it says `NONE is measurable` and exits 2. |
| `samples/measure_request_construction.unknown-field-REFUSED.out` | `UNKNOWN BODY FIELD(S): totally_new_vendor_field` — named, not dropped, exit 4 |
| `samples/measure_request_construction.faithfulness-FAILED.out` | `--reference` against the wrong arm's capture: `REPRODUCTION FAILED`, with the field that differs named, exit 5 |
| `samples/measure_request_construction.claims-drift-FAILED.out` | `DRIFT: a figure above is not in the prose`, exit 6 |

`--reference` is the ported discipline that nothing built on a capture is believable until a fresh
capture reproduces a stored one to the digit (`samples/measure_request_construction.faithful.out`
is the passing side). `--check-claims` is its prose counterpart — every headline figure measured
must be quoted in a prose file — and it compares **whitespace-insensitively**, because the gate it
came from once failed purely on a claim that had wrapped across a markdown line break.
`samples/measure_request_construction.claims.out` checks this README and ends
`every figure measured here is quoted in the prose`; the RED side's fixture gets one digit wrong
*and* wraps a correct claim across a line break, so the only thing the gate can be firing on is the
drift.

Bytes, not tokens, are the measured unit: this repository has no dependencies and a bundled
tokeniser would be both a dependency and a guess at the provider's own. Pass
`--tokeniser tiktoken:cl100k_base` and tokens appear; without it the token column says
`NOT MEASURED -- no tokeniser given` rather than a byte/4 estimate
(`samples/measure_request_construction.tokeniser-unavailable.out`). **Bytes are a proxy for tokens
and the proxy is not uniform** — a tool-schema array is dense in JSON punctuation — so a bytes
ratio between two arms is not their token ratio. Measured on these two captures with an externally
installed tokeniser (`samples/measure_request_construction.tokens.out`), dsh's payload runs at
`3.24 bytes/token` and Hermes's at `4.23 bytes/token`: a 30% difference in the proxy itself, which
is exactly why the instrument will not apply it on your behalf.

The controls are pinned the same way `flags.py`'s are, by reverting each fix and requiring a
control to turn RED:

```
REVERT-EACH-FIX GATE (request construction) -- 13 cases
PASSED -- all 13 fixes are pinned: reverting any one of them turns a control RED.
```

That gate's **own first run was RED on 3 of its 13 cases**, and the run is kept at
`samples/gate-evidence/test_request_construction_controls.RED.out`. One of the three is worth
reading: defining JSON framing as `whole - sum(fields)` is *arithmetically identical* to predicting
it from the keys whenever the split is complete, so reverting that alone changes nothing — it is
only the enabler. The case now reverts it **together** with the narrowed split, which is the
implementation somebody would really write, and a new control requires the split to NAME every
top-level field.

### Which engines are actually supported

`instruments/engines.py` holds one adapter per engine, and **every instrument prints which
adapters are measured and which are guesses, on every run**:

- **Verified** (run, with output kept in `samples/`): Claude Code, its ACP host, Codex,
  **DeepSeek Harness (`dsh`)** and **Hermes**.
- **Untested adapters** — patterns we believe from documentation, *not* measurements: goose,
  aider, opencode, cursor-agent. Each says in the file why it may be wrong (opencode is
  client/server, so its sessions are probably not separate processes at all).

`dsh` and `hermes` were added by running them, on one box, against `capture_proxy.py` — no
credential, no spend. The process evidence is `samples/measure_session_footprint.dsh-hermes.out`,
a live snapshot in which both appear as matched trees; the request evidence is the two capture
fixtures above. Four things that verification established, none of which the documentation says:

- **`dsh` needs Node ≥ 24.9.0 and on an older node it produces zero output and exits 0** — no
  error, nothing to grep. `drive_dsh.py` therefore refuses up front rather than launching it, and
  an explicitly-named node that is wrong is refused instead of being silently replaced by whatever
  is on `PATH` (`samples/drive_dsh.wrong-node-REFUSED.out`).
- **`dsh`'s transcript lives at `$DSH_HOME/sessions/<cwd-slug>/<session-id>/session.v4.jsonl`**,
  read off disk rather than from documentation. It is *not* the Claude Code transcript schema, so
  this repo's transcript readers cannot parse it; the path is recorded because that is where the
  data is, not because they can.
- **Hermes has no transcript files at all**, so its adapter's `transcripts` is `None` rather than a
  directory that is always empty: a session's history goes into `$HERMES_HOME/state.db` (SQLite,
  tables `sessions`/`messages`), and `$HERMES_HOME/sessions` stayed empty across a verified run.
  Every transcript-reading instrument here is therefore structurally inapplicable to Hermes.
- **Hermes's launcher `exec`s into an inline Python bootstrap**, so `argv[0]` stops being `hermes`
  while a session runs — matching the launcher name alone finds nothing. It also runs a *shared*
  gateway process that the same pattern matches, so for Hermes a tree **count** is session
  processes plus one gateway, not a session count. Both are said in the adapter's own note.

One thing that verification found about the *other* instruments, worth knowing before you trust a
zero: `measure_session_footprint.py` folds a session into the tree of any matched engine that is
its **ancestor**. A `dsh` launched from inside a Claude Code shell is counted under Claude Code and
no `dsh` row appears at all — which looks exactly like an adapter that does not match. Launch it
detached (`setsid`) when the per-engine row is what you want.

For an untested engine, a zero means "this adapter found nothing", which is **not** evidence the
engine was absent. If you run one and it works, flipping that flag and adding your output is the
most useful contribution this repo can get. If it doesn't work, that's equally useful.

---

## What these measured here

One 16 GB Linux box, Claude Code 2.1.197, Codex 0.147.0, 2026-10-16, **under heavy load**.
Readings, not constants — [`docs/limits.md`](docs/limits.md) says which moved and why.

- `session trees: 20`, `summed RSS: 7483 MB` against `summed PSS: 5175 MB` — `PSS/RSS = 0.69`.
  Per session, RSS `p50=350 MB`, PSS `p50=236 MB`.
- `sessions with any activity in 7d: 42`, but `mean=0.15` sessions active per minute and
  `busy_minutes=964/10080`. **The resident count and the working count are not the same number,
  and the gap is the whole case for pooling.**
- Boot: `ready p50=1.88s` full harness, `ready p50=0.65s` with `--bare`, `ready p50=2.49s` for a
  full trivial turn. But `init` is the *first* stream event, with MCP servers still `pending`:
  time to a *usable* MCP tool was `min=5.22s  max=6.97s  spread=1.75s` (n=2).
- `LIVE sessions owning background work older than 900s: 0`, across `LIVE session cgroups: 20`,
  from an instrument that exits non-zero when it cannot see its own planted decoys.

### A prompt-cache entry outlives the process that wrote it

Process **A** writes a cache under a unique marker and **exits**. **B** then resumes with the same
marker, **C** with a different one; they differ in nothing else.

```
A (writer; exited):     read=14848  creation=11416
B (SAME marker):        read=26264  creation=2875
C (DIFFERENT marker):   read=14848  creation=14307

B_read(26264) == C_read(14848) + A_creation(11416)
```

Reproduced on an independent run: `B_read(26265) == C_read(14848) + A_creation(11417)`, and again
by an independent reviewer on a differently-loaded box. **B read the whole of A's prompt after A
was gone.** For a pooling decision, that removes the objection that looks most expensive on paper.

**Three things this is not**, all raised in review:

1. **It is one fact, not three.** The design forces `C_read == A_read`, so the identity reduces to
   `B_read == A_read + A_creation` — "B read all of A's prompt". The single degree of freedom is
   *all* rather than *some*. It is not three numbers independently coinciding.
2. **It is fresh→fresh.** A has exited, so this compares two fresh processes. It does **not**
   address whether a *resident* process renders its prefix differently from a fresh one — that
   needs a resident arm. Fresh→fresh is, however, exactly the pooling case.
3. **n=1 per arm per run.** And the instrument's *other* control — arm 4, a model switch — held in
   only two of five observed runs, with an unverified premise: the documentation never says caches
   are model-scoped. It ships as a control we do not trust.

---

## Before you publish any output

```bash
bash scripts/preflight.sh                      # every local gate, under set -e
bash scripts/check_remote_identities.sh        # asks the REMOTE what it still serves
bash scripts/regen_samples.sh --write          # re-capture every reproducible sample
ln -sf ../../scripts/preflight.sh      .git/hooks/pre-commit
ln -sf ../../scripts/pre-push-hook.sh  .git/hooks/pre-push
```

Verify both symlinks resolve. A **dangling** hook symlink is silently skipped by git —
`git push` exits 0 with no warning — so a reader following these two lines against a file
that isn't in the repo installs nothing while being told they are covered. That happened
here.

Two more things about those two lines, both found by running them (ARI#28):

- **In a linked worktree the `ln` fails outright**, because `.git` there is a *file*. Hooks
  live in the main checkout's `.git/hooks`, which every worktree shares — so install the
  symlink there and let `preflight.sh` work out which tree is being committed, which it now
  does. Resolving by the script's own location would gate the **main checkout's** files
  while you commit a worktree's.
- **Run as a hook, these gates used to recurse infinitely.** git exports `GIT_DIR` into a
  hook, and `GIT_DIR` overrides directory discovery for every later `git` call — including
  the real commits `scripts/test_identity_allowlist.sh` plants inside its own throwaway
  clones. With `GIT_DIR` still set those commits land in *this* repository, firing the
  pre-commit hook, which runs the suite, which plants again. Observed at ~36 nested runs
  before it was killed. `preflight.sh` now unsets the exported git variables first. The
  same `${BASH_SOURCE[0]}` problem also meant a symlinked hook resolved its root to `.git`
  and failed on the very first gate — so that hook could never have passed, which is the
  less dangerous half of the same bug.

Install both hooks, not just the first. A pre-commit run **cannot** see a new commit's
author identity or its blobs in history, because neither exists yet when it runs — and a
commit was pushed twice here carrying an identity the pre-commit run had no way to
inspect. (The immediate cause is worth knowing: in `VAR=x git add -A && git commit`, the
environment prefix applies only to the **first** command, and `GIT_AUTHOR_EMAIL` overrides
`git config`.)

The scrubber exists because a leak is the only irreversible mistake here, and it is deliberately
paranoid: it refuses tracked binaries outright, because a compiled artefact can embed an absolute
build path with no sign of it in the diff. That is not hypothetical — it happened here, and the
gate certified the tree clean, because `grep -I` skips binary files and `git log -G` does not
search binary diffs. Both legs now have decoys.

One gate asks a different question: not "does the code pass its controls" but **"do the
controls protect what the code changed"**. `scripts/test_flags_controls.sh` reverts each fix
`flags.py` makes — back to the code that was really there, one at a time — and requires a
control to turn RED:

```
REVERT-EACH-FIX GATE -- 13 cases
PASSED -- all 13 fixes are pinned: reverting any one of them turns a control RED.
```

It exists because review found the opposite. Thirteen of fifty-three mutants left the suite
green, and the cause was not carelessness: the controls written after the first review round
pinned *the reviewer's findings* rather than *the fixes*. Four of six changed behaviours
could be reverted with everything still green, and two of those reversions lost data — a
turn silently dropped on a transcript holding a literal U+2028, and four turns returned when
one was asked for. "The reviewer already found that one" is a record of one person having
looked once; it is not coverage. This gate's own first run was **RED**, on a fix whose
control did not exist yet, which is the only evidence that it can fail.

`samples/` holds real output from every instrument, **including the runs that failed**: a caching
run that exited 3, a census self-test that missed its decoys, the MCP counterfactual that refuses
a server which never connected, and `samples/gate-evidence/check_figures.RED.out` — the figure
gate observed failing before the figures were corrected, because a gate whose first run is green
tells you nothing.

### Merging a reviewed branch into `main`

Squash it **locally, under the bot identity, then push** — not with `gh pr merge`. GitHub's
squash-merge sets the resulting commit's *author* to whoever's token performed the merge, and this
workspace has exactly one credential: the maintainer's own account. That is not a setting; it is
what the merge API does. It is also how `40372496f26` — the squash of PR #2 — came to carry a real
personal address permanently, which the identity gate then refused on *every* branch cut from
`main`, and why that one commit is grandfathered by SHA in
[`scripts/identity_allowlist.sh`](scripts/identity_allowlist.sh) — an exception keyed to one
immutable commit, with its own controls in `scripts/test_identity_allowlist.sh`, which plant the
*same* personal address on a different commit and require the gate to still refuse it. All three
identity checks — the scrubber's, the pre-push hook's, and the one that asks the remote what it
still serves — read that same file, so the exception cannot exist in one of them and quietly not
in another. `main` has
no branch protection,
so nothing enforces this recipe up front; the gate only catches it afterwards, once the commit
exists and the fix has become a history rewrite.

**Use the script; it is this recipe with the mistakes made impossible:**

```bash
bash scripts/merge_to_main.sh <branch> <pr-number> "<commit subject>"
bash scripts/merge_to_main.sh --dry-run <branch> <pr-number> "<subject>"   # stops before the push
```

It sets all four identity variables, **reads the resulting commit's identity back** rather than
assuming the variables took effect, runs the full gate suite *on the merge commit while it is
still private*, and pushes only if all of that passed — refusing and cleaning up otherwise. It
cannot close the PR (`gh` is not installed here) and prints the exact command, with the real SHA.

It also cannot stop anyone clicking **Squash and merge** on github.com. Only branch protection
can, and that needs repo-admin access no session here has (ARI#14).

The recipe it automates, for reference:

```bash
BOT_NAME='workain'
BOT_MAIL="${BOT_NAME}@users.noreply.github.com"   # assembled rather than written out: the
                                                  # scrubber's own `email addresses` pattern
                                                  # matches a literal one, even in this file
git fetch origin
git worktree add /tmp/merge-main origin/main      # detached; leaves your own checkout alone
cd /tmp/merge-main
git merge --squash "origin/<branch>"

# All four are set explicitly because GIT_AUTHOR_*/GIT_COMMITTER_* in the environment
# OVERRIDE `git config`, and a session may well have them set to something personal.
GIT_AUTHOR_NAME="$BOT_NAME"    GIT_AUTHOR_EMAIL="$BOT_MAIL" \
GIT_COMMITTER_NAME="$BOT_NAME" GIT_COMMITTER_EMAIL="$BOT_MAIL" \
  git commit -m "<subject> (#<pr>)"

git log -1 --format='%an <%ae> | %cn <%ce>'       # read it before pushing, not after
bash scripts/scrub_check.sh --all                 # the identity leg, while it is still private
git push origin HEAD:main

cd - && git worktree remove /tmp/merge-main
gh pr close <pr> --comment "squash-merged to main as <sha>"   # a pushed squash does not
                                                              # close the PR by itself
```

The verification line is the point of the recipe. Everything above it can be got right by habit;
what makes the identity wrong is an environment you did not look at, and `git log -1` is the only
step that sees it before the commit is public.

---

## Decisions

**Apache-2.0.** The projects these sit beside — the Agent Client Protocol, Codex, goose — are
Apache-2.0, so output and derivations move between them without a compatibility question, and it
carries an explicit patent grant that MIT does not.

**One canonical copy.** These were extracted from a private study; that study's copy is a frozen
audit record and is deliberately not maintained. Fixes land here. Two *maintained* copies of a
self-testing instrument is the worst outcome available, because each copy's self-test keeps
passing while the two drift.

**Name.** Descriptive, and deliberately not `-metrics`: no exporter, no dashboard, no daemon.

---

## Contributing

The bar is the method. A new instrument needs a `--help-blind` answer, at least one control that
can **fail**, a non-zero exit when it cannot measure, and real output in `samples/` including what
went wrong. An engine adapter needs honesty about whether you ran it.

**If you reproduce a number here and get something different, that is the most useful thing you
can report.** Several figures moved by 2-3x between runs minutes apart on one box.

- **[`ARTICLE.md`](ARTICLE.md)** — the method: five ways a runtime measurement lies, with worked
  examples.
- **[`docs/method.md`](docs/method.md)** — the rules, and the four defects that produced them.
- **[`docs/citations.md`](docs/citations.md)** — every third-party quotation, verified, with an
  enumerated audit of the 7 of 13 that needed correction.
- **[`docs/limits.md`](docs/limits.md)** — what these cannot tell you.
