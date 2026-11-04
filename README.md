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
| wire bytes, first request — *observed* | `8225 bytes on the wire` | `48820 bytes on the wire` |
| normalised body — *re-serialised* | `normalised body 8680 B` | `normalised body 49655 B` |
| fixed per-turn part (system + tools) | `fixed 1665 bytes, paid again on every later turn` | `fixed 49426 bytes, paid again on every later turn` |
| tool schemas exposed | `3 tool schema(s) exposed` | `20 tool schema(s) exposed` |
| model-input bytes | `MODEL INPUT 2161 B` | `MODEL INPUT 49655 B` |
| provider metadata | `provider metadata 6519 B (75.1% of the body)` | `provider metadata 0 B` |

Every cell above is a clause the instrument itself pins, for **both** arms. **What that does
and does not buy:** the digits can no longer be kept while the verb is flipped in place, which is
a real narrowing. It is not protection from an inversion — a review kept all four clauses
byte-identical and still in this file, relabelled them as a retracted draft, and asserted the
opposite around them, with every gate green. A clause is checked for PRESENCE, and presence cannot
distinguish a statement from a quotation of one; nothing in this repository reads meaning. See
`docs/limits.md`. It used to pin the
left column only — `headline_claims` read `rows[0]` — so a review could rewrite the entire
right-hand column, or swap the two, with every gate green. Bare table cells were worse than
unpinned: a one-digit figure like `9` occurs in all but a handful of this repository's
artefacts, so no exclusion list can protect it, and the fix is to publish a clause instead of a
number. (That claim deliberately carries no count. An earlier draft said "62 artefacts", measured
over a corpus that was not the one the gate searches -- the same commit excluded
`samples/raw-payloads/` from the search -- and no pattern reads it, so nothing would have caught
the drift.)

**Only the first row is an observation.** Every other row is computed over a re-serialisation of
the parsed body with `json.dumps`' default separators, and a client that writes `{"a":1}` where
`json.dumps` writes `{"a": 1}` sends fewer bytes than the split accounts for. On the Hermes
capture that difference is large enough to invert the comparison a reader would expect:
`NOTE: model input 49655 B and fixed 49426 B EXCEED wire 48820 B (101.7%)`. The instrument prints
that line itself rather than leaving a reader to find a `fixed` figure larger than the request and
distrust the whole table. Quote the wire row for what the network paid.

Each claim carries the *direction* of its figure and not only the digits. For dsh:
`model input 2161 bytes of a 8680-byte body, which is what a per-token bill is computed from`, and
`6519 bytes (75.1% of the body) of provider metadata, which the vendor documents at ZERO
model-input tokens`. For Hermes, whose payload is all model input:
`model input 49655 bytes of a 49655-byte body, which is what a per-token bill is computed from`,
and `no provider metadata at all, so its model input and its whole body are the same 49655 bytes`.

Each of those claims used to be a bare figure, and the first published version of the sentence
joining them was **wrong**: it named `8225 bytes on the wire` and then divided by it, while the
`75.1%` had been computed over the 8680-byte body. 6519/8225 is 79.3%, so the sentence named one
denominator and quoted the other's answer, and the gate passed because it only ever asked whether
each figure *string* occurred in the prose. A review did the division. Both halves are fixed: the
claim now says *of the body*, and `scripts/check_figures.sh` learned to read percentages at all.

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

Seven things it refuses to do, each with a committed artefact:

| Sample | What it shows |
|---|---|
| `samples/measure_request_construction.probes-only-NO-MEASUREMENT.out` | a **real** capture of a run in which the agent never constructed a turn — only Hermes's two startup probes. Counting capture *lines* would have called that a measurement; it says `NONE is measurable` and exits 2. |
| `samples/measure_request_construction.unknown-field-REFUSED.out` | `UNKNOWN BODY FIELD(S): totally_new_vendor_field` — named, not dropped, exit 4 |
| `samples/measure_request_construction.faithfulness-FAILED.out` | `--reference` against the wrong arm's capture: `REPRODUCTION FAILED`, with the field that differs named, exit 5 |
| `samples/measure_request_construction.claims-drift-FAILED.out` | `DRIFT: a figure above is not in the prose`, exit 6 |
| `samples/measure_request_construction.corrupt-REFUSED.out` | one unreadable line refuses the whole capture — a partial read shrinks every total, which is the one failure that looks like good news |
| `samples/measure_request_construction.not-an-object-REFUSED.out` | a line that is valid JSON but the wrong shape: `is JSON but not an object (it is a list)`, naming the file and the line. It used to raise `AttributeError: 'list' object has no attribute 'get'` from `is_probe` and exit 1, which is not in this instrument's documented set of exit codes |
| `samples/measure_request_construction.duplicate-key-REFUSED.out` | `repeats the JSON key(s) system` — `json.loads` keeps the last value, so a long system prompt silently became three bytes while the coverage invariant still held, because the *parsed* body really is self-consistent |
| `samples/measure_request_construction.unparseable-REFUSED.out` | three requests the recorder could not parse, with their `144000 raw bytes between them` named. They carry no `tools` and no `messages`, so they were being filed as startup probes: real traffic not omitted but **mislabelled**, under a 100%-coverage banner, at exit 0 |

**Both captures were taken twice, from separate processes, and `--reference` compared the second
run against the committed first.** Hermes reproduced byte for byte: the second capture file is
identical to the committed one, and `samples/measure_request_construction.hermes-rerun-faithful.out`
ends `every field reproduces to the digit` at exit 0.

dsh **failed the check**, at exit 5, and the failure is the more useful half
(`samples/measure_request_construction.dsh-rerun-REPRODUCTION-FAILED.out`). Exactly two of the
eight compared fields differ, by the same four bytes:

```
  provider_metadata_bytes            6515         6519  -4  <-- DIFFERS
  model_input_bytes                  2161         2161  MATCH
```

The cause is `dsh_session_log`, which embeds the session id, and the two runs chose ids four
characters apart. Everything else — `system_bytes`, `tools_bytes`, `conversation_bytes`,
`n_tools`, and the model-input figure above — reproduces exactly. So the quantity a bill is
computed from is stable across runs while the payload carrying it is not, which is the same
distinction the provider-metadata split exists to make. The check refuses anyway, rather than
calling four bytes close enough, and that is the behaviour worth having.

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
ratio between two arms is not their token ratio.

Measured on these two captures with an externally installed tokeniser
(`samples/measure_request_construction.tokens.out`), that non-uniformity is worth being careful
about in a specific way. Over the **whole body** the two engines run at
`3.24 bytes/token over the whole body` and `4.23 bytes/token over the whole body` — but three
quarters of the dsh body is provider metadata and none of the Hermes body is, so most of that gap
is the metadata's own density and not the engines'. Over **model input**, the quantity a bill is
actually computed from, they are `4.11 bytes/token over MODEL INPUT` and
`4.23 bytes/token over MODEL INPUT`. The instrument prints both, labelled, because a review found
the whole-body pair quoted in a paragraph that was arguing about cost — where the model-input pair
is the one that answers the question.

That path has a limit worth stating plainly: `tiktoken` is deliberately not a dependency and is
not installed on the box this was written on, so **no gate here executes the token code**. A
review replaced the encoder with `len(bytes)//4` and every check in the repository stayed green
while the output still called itself `tiktoken:cl100k_base`. What now runs is the one check that
can run without the dependency — at load time the encoder is asked to count three probe strings,
and if it returns exactly `len(bytes)//4` for all of them it is refused rather than labelled as a
tokeniser. The sample itself is listed unreproducible in `scripts/regen_samples.sh` for the same
reason, with the command that produced it recorded in its header.

The controls are pinned the same way `flags.py`'s are, by reverting each fix and requiring a
control to turn RED:

```
REVERT-EACH-FIX GATE (request construction) -- 51 cases
PASSED -- all 51 fixes are pinned: reverting any one of them turns this gate RED.
```

All but thirteen of them -- the gate's original set, below -- were added after four
independent reviews of this branch returned **BLOCK**. Each reverts a fix to the implementation the review substituted and found the entire
preflight green against — including `coverage_ok`'s `==` relaxed to `<=` (one character, and it
passes every deficit, which is the only direction a dropped field can go), framing predicted as
`len(key) + 2` (indistinguishable from the real thing for every short ASCII key, which is what
every fixture used), and deriving the wire figure from the parsed body instead of reading what the
recorder observed.

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

The storage claims in those two notes are evidenced by
`samples/engine_evidence.dsh-hermes-storage.out` — the on-disk tree, the two transcript schemas'
record keys side by side, the empty Hermes sessions directory, and `state.db`'s table list with
row counts — rather than asserted in a comment. A review found that the notes stated where each
engine keeps its history while the only committed artefacts were about processes and request
bytes, and that `check_figures.sh`'s adapter leg was satisfied by *any* tracked sample a note
happened to name. The specific facts are now pinned to the file that shows them.

**Both adapters match only the shape each engine was observed to have, and deliberately
under-match.** Each originally carried a second, bare-name alternative — `dsh` or `hermes` as a
word — to cover a launcher install that was never exercised here. As substrings those fired on
ordinary command lines: `rm -rf /tmp/dsh`, `du -sh /mnt/data/dsh`, `ls -la /srv/hermes`,
`tar -czf backup.tgz /var/lib/hermes`. Every one would have been reported as a live agent
session, which is defect v3 in `docs/method.md` recreated in a new adapter, against that
document's own rule to match structurally rather than by substring. The unexercised alternatives
were removed rather than caveated: a launcher install is simply not matched, and
`measure_session_footprint.py --list-unmatched` is how you find that out. Dropping them changed
nothing about the committed run, whose `dsh` row was matched by the package's `bin.js` path.

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

- **Interrupting the suite can leave a mutating gate's subject replaced.**
  `scripts/test_fixture_freshness.sh` swaps `scripts/regen_samples.sh` for a stub, checks that
  the freshness gate notices, and restores it. Killed mid-run — a timeout, a `Ctrl-C` — it
  restores nothing, and the *next* run of the suite fails with a confusing wall of "these
  tracked samples are in neither list", because the table it reads is a seven-line stub. It
  refuses to start if that file is already modified, which is the right guard and is also why
  the recovery is easy: `git checkout -- scripts/regen_samples.sh`. Worth knowing before
  debugging the wrong thing; it cost a detour here.

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

`scripts/check_figures.sh` itself got three changes out of the same review, each because the gate
was demonstrated unable to fail:

- **`samples/fixtures/` is excluded from the artefact search**, alongside
  `samples/gate-evidence/`. Fixtures are instrument *inputs* — captured JSON bodies, and a
  markdown file whose entire purpose is to hold a wrong figure for a drift gate to catch — and an
  input is not evidence of anything. With this branch's two capture fixtures in the
  searched corpus, the
  engine-comparison table could be rewritten to claim a `max_tokens` value from inside the dsh
  fixture as its wire-byte figure, with this gate and the whole preflight still green. Near-miss
  digit flips were caught, so the gate had teeth only against values adjacent to the truth.
- **Every instrument's `--help-blind` answer is now read as prose.** `docs/method.md`'s first rule
  rests on that answer, and nothing checked the figures in it. One instrument's blind answer
  stated its central finding using the *source study's* numbers — three bytes from this
  repository's own in both numerator and total, and present in no artefact here — and reasoned a
  factor-of-five conclusion from them. Thirteen instruments' answers are scanned now; the figures
  that are not measurements are listed with reasons, same as the prose exemptions.
- **The extractor reads percentages, and the citation guard ignores line breaks.** No pattern
  matched a bare `75.1%` before, which is how a sentence that named one denominator and quoted
  another's answer survived. Adding the pattern immediately surfaced a second, unrelated published
  error — an `87%` swing that is 86% — which is the gate working on its first run. The
  whitespace tolerance is the other half: pinning whole *clauses* rather than bare figures (so an
  inversion cannot keep the digits and flip the verb) means every long claim wraps in markdown,
  and a gate that fires on reflow is one that gets switched off.

A second review verified those fixes and found ten more things, four of them mutations that
survived the whole preflight. Three are worth stating because of *where* they were:

- **A second capture on the same command line downgraded every refusal to exit 0.** `measure()`
  appended the refusal and carried on, and the only hard stop fired when *nothing* had measured —
  so `measure_request_construction.py <any-bad-capture> <the-dsh-capture>` printed the refusal,
  then a complete table, then exited 0. That is this instrument's own headline danger in the one
  invocation shape a two-engine comparison uses. Every one of the then-22 `measure()` calls in the
  self-test passed exactly one capture, which is why nothing looked there. Any refusal is fatal
  now: a capture this instrument refused is a hole in the total, whatever the others did.
- **The fixtures exclusion did not close the laundering hole, and the prose claiming it did was
  itself wrong.** That same `max_tokens` value had moved into the dsh event log committed *in the
  same round* as finding 15's fix — a verbatim payload trace, not a fixture — so the table could
  still claim it.
  Verbatim traces now live in `samples/raw-payloads/`, excluded for the same reason. The other
  half has no exclusion-list answer at all: a one-digit figure like `9` occurs in all but a
  handful of the artefacts here, so a bare table cell is unpinnable, and the fix is to publish a
  *clause* instead.
- **`capture_proxy.py` is where the observation is made, and it had no protection.** Its captures
  are committed as inputs, so `check_fixtures_fresh.sh` never re-runs the recorder, and its own
  self-test checked `raw_bytes` against `len(json.dumps(body).encode())` — which is exactly what a
  mutant computing it from the parsed body produces, so the check could not tell them apart. Four
  mutations survived the full preflight, including round 1's own fourth survivor: a recorder that
  drops an unparseable body reproduces the mislabelled-as-probe finding word for word, and no
  reader-side refusal can see a line that was never written. The self-test now sends *compact*
  bytes and compares against the length it knows it sent, sends a body that cannot be parsed, and
  sends nothing at all.

A third review was asked to lead with the regressions round 2's fixes had introduced, because
round 1's fixes had already introduced one. It found two, plus four more mutations that survived
both self-tests and the whole preflight. Three are worth stating:

- **The joiner that fixed wrapped spans could HIDE a figure the per-line extractor caught.**
  Joining re-*pairs* backticks: a span left open on one line swallows the opening backtick of the
  next line's span, and that line's figure ends up between two backticks that are no longer a
  delimiter pair. The demonstration published a five-digit `fixed` figure in README — present in
  no artefact — with this gate and the whole preflight green. (Not quoted here, for the reason
  `samples/raw-payloads/README.md` gives: an exemption for a laundered figure re-opens the hole
  the paragraph is describing.) The first fix was not a smarter joiner —
  which would still have been one re-pairing away from the same thing — but reading the file as
  written **in addition to** the joined copy, on the reasoning that no arrangement of line breaks
  can subtract from a union. **That fix was itself reverted a round later**, because the union
  *adds*: see the round-4 note below.
- **The empty-POST fix turned a loud refusal into a silent startup probe.** `if not raw: body =
  None` cannot tell *the client sent no body* from *this recorder never read the body it was
  sent*, and `do_POST` framed every request by `int(Content-Length or 0)` — so a **chunked** POST
  read zero bytes and was recorded as a request that carried none. A review put 40391 real bytes
  through it and got `1 measurable, 1 startup probes` at exit 0. Before the fix the same request
  was refused, so the round traded a false refusal for a silent misclassification, which this
  repository's own notes call the worse of the two. Chunked bodies are now de-chunked and
  measured; a body that genuinely cannot be framed is marked with a null length, because length
  unknown is not length zero, and the reader refuses it by name.
- **The overrun note had no ceiling, so it explained away a 2.9x lie.** One word of the recorder
  (`utf-8` → `ascii`) survived both self-tests and the whole preflight, because every body the
  self-test sent was ASCII — while `samples/fixtures/request_construction.hermes.jsonl` already is
  not. On a CJK payload the replacement characters tripled the measured body, and the only visible
  symptom was round 1's overrun note *telling the reader it was normal*. A normalised split can
  legitimately exceed the wire figure, but only by one byte per separator; past that it is not a
  re-serialisation of those bytes at all. That bound is now computed per body and printed, an
  overrun beyond it is refused, and the self-test's two fixtures either side of the line differ by
  **one byte** of wire.

The other three: a six-character mutation (`parts.append(instr[:1])`) dropped every instruction
message after the first from four published columns at exit 0, because no fixture carried two of
them and the leg asserted `system_bytes > 5000` where the exact value was available; two captures
sharing an arm label collapsed into one row, which is round 2's own "one engine pinned by nothing"
reached by label collision instead of by `rows[0]`; and one of round 2's own gate cases was
arbitrary damage rather than a revert — it neutered the first of four checks and left the rest, so
the mutant refused everything with a body and went RED for a reason the case does not name. A
control that goes red for the wrong reason reads exactly like one that works.

A fourth review was given the same brief as the third, and found that round 3's own fix for the
first of those regressions had introduced the next one. Both blocking findings were in that
commit:

- **Reading each prose file as written *as well as* joined fixed hiding and started inventing.**
  On the continuation line of a legitimate wrapped span, the same mis-pairing that hides a figure
  *emits the prose between one span's closer and the next one's opener* — so an ordinary sentence
  in this branch's own style failed the gate with `-- 6519 of 8680 B -- while hermes publishes` as
  its "figure". A contributor is told a figure drifted when no figure exists, and the only ways
  out are reflowing a line or an exemption for a sentence fragment. Both directions have one root:
  per-line backtick pairing is ambiguous and no arrangement of greps resolves it. The span leg
  reads the joined copy only now — code spans as markdown defines them — and the hidden-span case
  is covered where nothing is ambiguous, by a bare-`B` unit pattern over the file as written. That
  pattern turned out to be reading **15** figures in the blind answers where 13 were read before,
  and twelve published byte figures in README and `docs/limits.md` that only the span leg had
  seen.
- **There were suddenly two places that write header names, and the gate case anchored on one of
  them.** The `__unread__` branch carried its own copy of `sorted(headers.keys())` — the line this
  file's own docstring calls "the one line in this file whose mistake is irreversible". Mutating
  the *new* copy to `.items()` wrote a real `Bearer` value into a capture with both self-tests,
  the 50-case controls gate and the whole preflight green. Fixed by having one write site again
  rather than by adding a second gate case: there is nothing left for a future branch to forget to
  copy.

It also found **nine of thirteen** one-point mutations surviving the full preflight, seven of them
in code round 3 had just added. Three were real defects rather than gaps: dropping the EOF case
from the chunked trailer loop makes the recorder **hang forever** and record nothing (a hang reads
from outside as a client that never arrived); reading `Transfer-Encoding` as a single token instead
of a list turns the legal `identity, chunked` into a fatal refusal; and `_write`'s docstring called
`ensure_ascii=False` "load-bearing" while pointing at a note that says nothing about it — the flag
changes no figure, and what it actually did was **lose a whole request**, because a lone surrogate
(what a client emits when it truncates a string mid-character, and `JSON.stringify` of a sliced
emoji really does) cannot be encoded as UTF-8 and took the handler down before anything was
written. That request now records with the surrogate escaped, and the reader refuses it by name:
recorded but unmeasurable is a finding, silently absent is not.

The recorder has now been the weak file three rounds running, for a structural reason worth
naming: its captures are committed as *inputs*, so `check_fixtures_fresh.sh` never re-runs it, and
its own `--self-test` is the only thing standing behind every byte in the report. Four of the
round-4 legs needed a hand-built HTTP request, because `urllib` cannot construct a chunked body, a
negative `Content-Length`, a half-closed stream or a stray blank line between chunks — which is
exactly why none of those behaviours had ever been exercised.

A fifth pass -- this one over the round-4 diff alone -- found that the round-4 fixes had done it
again, in both of the places round 4 had just touched. Two findings, and the second is the more
embarrassing:

- **"One write site" was one *expression*.** The round-4 fix de-duplicated
  `sorted(headers.keys())` and left **two complete record literals**, each with its own
  `"header_names"`. `C12` had been re-anchored onto the assignment, so the assignment was pinned
  and *neither* consumption site was — and mutating the `__unread__` branch's copy wrote a live
  `Bearer` value into a capture with every self-test check and the whole preflight green. The same
  finding as the round before, one line over, published under a docstring asserting it could not
  happen. There is one record literal now: the branches decide only `raw_bytes` and `body`. And the
  credential leg had never covered that path at all — every credentialed request went through
  `post_bytes` and landed on the other literal, so the one request that reaches `__unread__` now
  carries an Authorization header on purpose.
- **The gate case written to pin the other round-4 fix mutated the file into a `SyntaxError`.** It
  left a dangling `except`, so its RED came from a mutant that could not be imported — which is
  *precisely* the defect an earlier round found in an earlier round's cases, and whose lesson this
  README quotes two sections up. It also falsified three published statements, including the pinned
  "all N fixes are pinned". The case is a compiling revert now. The leg behind it asserted only the
  record's *length*, which a lossy `.encode("utf-8", "replace")` fallback preserves — that mutation
  survived every gate, and its consequence is worse than the crash it replaces: the body becomes
  encodable, so the reader's refusal never fires and a **mangled** body is measured and published
  at exit 0. The leg asserts the round trip now, and `C51` pins it.

**The structural answer, which should have come three rounds earlier.** Every other gate here has a
harness proving it can fail — `test_scrub_check.sh`, `test_fixture_freshness.sh`,
`test_identity_allowlist.sh`, `test_flags_controls.sh`. The *figure* gate had none, and it is the one
with three consecutive regressions in its span extraction. That is why each fix was invisible:
deleting the union leg, or deleting the bare-`B` pattern that replaced it, left the whole preflight
green, and the only evidence either had ever worked was a hand-written artefact that nothing re-ran.

`scripts/test_figure_gate.sh` now runs on every preflight. It plants four defects and requires each
to go RED — the hidden-span attack, a figure wrapped across a line break, a one-digit drift in a
pinned clause, and the attack again with the bare-`B` pattern **deleted from the gate's own source**,
which is what proves that pattern is the thing catching it. It also requires three ordinary-prose
cases to stay GREEN, including the exact wrapped-clause sentence that the reverted union leg
false-alarmed on, and requires the unmodified tree to pass so that every RED is caused by its own
planted defect.

`scripts/preflight.sh` runs `--self-test` for every instrument that offers the flag, picking them
up with no list to maintain — which means an instrument *without* the flag is silently not
covered. `drive_dsh.py` was the only one in that position and now has nine checks, over the
discovery logic where its real traps are: an explicitly-chosen node that is missing, one below the
floor, one exactly *at* the floor, an unparseable version, and a launcher shim whose own node
choice would make the version gate decide nothing. What it cannot cover — the handshake, the
notification reader, the no-turn exit — needs a real Node ≥ 24.9.0 and the real package, so the
evidence for those is the live run plus its verbatim event log
(`samples/raw-payloads/drive_dsh.live-events-redacted.out`), which makes that run's `"notifications": 16`
countable rather than a verdict the file asserts about itself.

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

**Who runs it is not the author.** A session never merges a PR it wrote, whatever tool executes
it — this script included. Merge authority sits with the manager, after either an independent
ROAST that returned PASS or explicit operator approval. The rule is not this repository's own: it
is fleet-level, stated in the operating instructions the managing session works under, and this
line is a restatement — so if the two ever disagree, that one wins and this is the stale copy.
A draft of this paragraph cited the governing file by name and `scripts/scrub_check.sh` refused
it, correctly: the name is an internal one and this is a public tree. The citation is therefore
*described* rather than named, which does cost a reader the ability to go and check it — the
sentence has to be followable from here alone, and it is. "Independent" means independent of
**interest** and not merely of context: an adversarial pass a session dispatches itself, in a
fresh subagent context, against its own branch is real evidence of effort and finds real defects,
and it is still the author grading the author. It does not discharge this gate. `--dry-run` is the author's half of
the recipe, and it stops before the push for exactly that reason.

Nothing inside this repository said so until now, which is how the question came up on #29: this
section specified the recipe in detail and said nothing about who may run it, so a session could
read the whole README, follow it exactly, and merge its own PR.

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
