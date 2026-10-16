# Measuring what an agent session costs — and five ways the measurement lies to you

*2026-10-16. Measured on one 16 GB Linux box running Claude Code 2.1.197 and Codex 0.147.0.
Every instrument, every raw output and every failed control is in this repository.*

We wanted one number: if a fleet pays resident CPU and RAM per *session* held open, how much
would it save by paying per *turn* instead — spawning a headless process when a turn arrives and
letting it exit?

Published per-session memory figures exist. Published *instruments* mostly do not, and published
*methods* almost never. So we built six instruments. What follows is not the capacity answer —
that is site-specific and ours is in the README. It is the five things that turned out to be
non-obvious, each of which we learned by getting it wrong first.

One theme runs through all five. **Every one of these produced a confident, plausible,
publishable number before we noticed.** Not one was caught by care. Each was caught by asking a
single question:

> What would this instrument print if it were blind?

If the answer is "the same thing it prints when the finding is genuinely zero", you have no
measurement yet — you have a number.

---

## 1. A prompt-cache entry outlives the process that created it

This is the one that decides the architecture, and it is the one we could not settle by reading.

The worry about turn-scoped workers is that a resident process holds a warm prompt cache and a
fresh process loses it, re-sending the whole transcript at full price every turn. The vendor
documentation describes caching as a prefix match with a TTL and says nothing about the client
process — but "the docs don't mention it" is not a measurement. A reviewer put forward a concrete
counter-mechanism: a resident process might re-render its prefix identically while a fresh
`--resume` process renders it *slightly* differently, and so miss.

Our first attempt measured a resident process's second turn against a fresh process's second
turn. Both hit. We wrote that up as settled.

**It was not, and the reason is the most useful mistake in this project.** Both arms had been
pre-warmed by an earlier run. A pre-warmed prefix hits whether or not residency matters, so the
experiment *could not have detected the counter-mechanism it was being cited as refuting*. "It
did not miss" rested on a sample in which nothing could have missed. A null result that reads as
confirmation is worse than no result, because it ends the enquiry.

The fix was to stop comparing arms and start isolating the writer. Process **A** writes a cache
under a unique marker and **exits**. Then two new processes resume the same session: **B** with
the same marker, **C** with a different one. B and C differ in nothing else.

```
A (writer; has exited before B and C run):   read=14848   creation=11416
B (new process, SAME marker):                read=26264   creation=2875
C (new process, DIFFERENT marker):           read=14848   creation=14307
```

C read 14,848 — the prefix it shares with A, and nothing of A's own write. B read 26,264.

```
B_read(26264) == C_read(14848) + A_creation(11416)
```

**Token-exact.** B read precisely the bytes A wrote, on top of the shared prefix. Reproduced on
an independent run — `B_read(26265) == C_read(14848) + A_creation(11417)` — and again by an
independent reviewer on a differently-loaded box. All four of our runs are in `samples/`.

**Three qualifications, all from review, two of which narrow the claim.**

*It is one fact, not three.* The design forces `C_read == A_read`: A and C both read exactly the
pre-marker prefix. So the identity reduces to `B_read == A_read + A_creation` — "B read the whole
of A's prompt". The one empirical degree of freedom is whether B read *all* of it or only some.
Good result; not three numbers independently coinciding, and calling it "token-exact, not
directional" oversold it.

*It is fresh→fresh, so it does not settle the objection that prompted it.* A has exited, so this
compares two **fresh** processes agreeing with each other. The counter-mechanism was that a
*resident* process might render its prefix identically while a fresh `--resume` renders it
differently — testing that needs a resident arm. What fresh→fresh answers is precisely the
**pooling** question, where every worker is fresh. That is the decision we care about, but it is a
narrower claim than "the objection is answered".

So a cache entry outlives the process that wrote it, and a different process reads it exactly.
Residency buys nothing here.

*The instrument's other control is unreliable.* Arm 4 switches model, assuming caches are keyed
per model so that a switch forces a miss. It held in **two of five** observed runs. And
re-reading the documentation showed **the cited page never states caches are model-scoped at
all** — arm 4's entire premise. We had been citing an assumption.

n=1 per arm per run. Arm 5's result stands because it carries its own control, C; arm 4's does
not. We publish both, and the run that exited 3.

---

## 2. Use PSS, not RSS — or your capacity number is wrong by construction

Measured here: `session trees: 17`, with `summed RSS: 7513 MB` against `summed PSS: 5356 MB`.
Per session, RSS `p50=392 MB` and PSS `p50=292 MB`.

The gap is not noise and not rounding. The CLI is a single compiled binary of about 323 MB, so
its file-backed pages are *shared* between every concurrent session. Summed RSS counts those
pages once per session; summed PSS counts them once. Every session you add makes the error
bigger. A capacity claim built on summed RSS is pessimistic **by construction** — it is not a
conservative estimate, it is an arithmetic mistake with a direction.

This is the one finding here that someone else published independently, on different hardware,
and their sentence makes the accounting argument better than ours:

> "Summing RSS instead would count the shared runtime once per agent: the same ramp reads 340 MB
> per session in RSS against 238 MB in PSS."
>
> — [omg.dev/tools/coding-agent-capacity](https://omg.dev/tools/coding-agent-capacity)

Different box, different session count, same ratio and same conclusion. When an independent
measurement lands on your number, say so — it is the cheapest corroboration available.

A smaller trap inside the same instrument: `smaps_rollup` is not always readable, and a tree
whose PSS cannot be read must print `n/a`, never `0`. A missing measurement that renders as zero
makes a session look free.

---

## 3. "Ready" is not "can use its tools"

Boot to the readiness event, measured: `ready p50=1.88s` with the full harness, `ready p50=0.65s`
with `--bare`, and `ready p50=2.49s` for a full trivial turn through to `result`.

Both figures are floors, and for a reason that is easy to miss: `system/init` is the **first**
event in the stream, and it lists every MCP server with `"status": "pending"`. The clock stops
before any tool is callable. If your workers need tools — and ours do — the boot number is not
the number you care about.

Time to a *usable* MCP tool, against a server bundled in this repo that does no work at all, so
this is the protocol floor rather than any real server's cost: `min=5.22s  max=6.97s  spread=1.75s`
(n=2). Several seconds past readiness, for a tool that returns a constant.

**And this is where we nearly mis-attributed our own number.** It is tempting to read that gap as
the cost of connecting MCP. It is not. An experiment that isolates connect properly — call the
same tool twice in one session, so the second call cannot pay connect, and subtract — puts MCP
connect at **25-58 ms**. (That experiment is **not** in this repository and there is no artefact
for it here, so treat the figure as reported rather than reproduced; the method is stated above so
you can run it yourself. What matters for this section is the *direction*, which is not in doubt:
connect is milliseconds, the gap is seconds, so the gap is not connect.) The seconds between readiness and a usable tool are dominated by **model
inference**, which in that same experiment ranged from 1.6 s to 26 s between steps. "Time to a
usable MCP tool" is an honest label; "therefore MCP costs seconds" is not, and the label invites
it.

Here the documentation also corrected us. The headless docs say:

> "Claude Code waits for still-pending servers before running the first turn, up to the
> `MCP_TIMEOUT` startup timeout, 30 seconds by default."

We had cited that as the ceiling on what the boot figure omits — and dropped the clause that
opens the sentence, which is **"When you pass `--mcp-config` with `-p`,"**. Our boot runs did not
pass `--mcp-config`. The same page then adds:

> "The wait requires Claude Code v2.1.221 or later."

**The box we measured runs 2.1.197.** So there are *two* independent reasons the wait never
happened during our measurements, and the 30-second ceiling never applied to them — the connect cost lands on the first tool call
instead, which is exactly what arm 5 of our boot instrument times. We had quoted a real sentence
from the right page and attached it to a measurement it did not govern. Reading the clause after
the quote is not optional.

---

## 4. The instrument is a writer, and its artefacts hide in plain sight

For two runs, every boot arm was about **+3 seconds** slower than it should have been.

The cause was in the stderr we were not reading:

```
Warning: no stdin data received in 3s, proceeding without it.
```

The benchmark never set `stdin` on the child process, so the child **inherited** the invoking
shell's — and when that descriptor is a pipe nobody writes to, the CLI waits for input it will
never get.

Two properties made this nearly invisible:

- **It was flat across every arm.** A constant added to all arms leaves every *ratio* intact.
  Full harness against `--bare` still looked plausible; the comparison the benchmark existed to
  make was undisturbed. Nothing in the numbers looked wrong.
- **"Inherited" means environment-dependent**, so it did not happen every time. One re-run read
  4.04 s and 3.48 s where the same arms otherwise read 1.15 s and 0.65 s. Intermittent, which
  reads as noise.

It was found by reading stderr on a re-run, not by the numbers looking wrong. `stdin=DEVNULL` is
now load-bearing in that instrument and labelled as such, and stderr is captured and printed
rather than discarded.

**The same class bit us twice more in this repository, in different costumes.**

`bench_mcp_connect.py` matched its target tool by looking for the tool's name and the string
`"tool_result"` in the same line of JSON. It reported `mcp_tool_result=3.87s` — while the server
under test reported `"status": "failed"` in the very same output. What happened: the tool did not
exist, so the model called `ToolSearch` with `query: "select:mcp__echo__ping"`. The target tool's
name appeared *inside another tool's arguments*. The instrument timed a failed tool **search** and
labelled it "time to a usable MCP tool". It now matches structurally — block type, exact name,
`tool_use_id` — and refuses the rep outright if the server reports `failed` or the result carries
`is_error`.

And while looking for a *decoy* process, never use `pgrep -f`: the pattern matches the searching
command's own command line, so it finds itself. We hit the bash equivalent of this while writing
this article — a `for` loop over `/proc` matched its own subshell and reported two sessions that
did not exist.

**The sharpest instance was in the tool meant to catch all the others.** This repository has a
scrubber that refuses site-specific material before anything is published, with a suite of decoys
proving each of its patterns fires. It reported the tree CLEAN while a compiled `.pyc` sat in it
embedding an absolute build path. Two reasons, both structural rather than a rotted regex: `grep
-I` *skips binary files*, and `git log -G` does not search binary diffs. Every decoy in the suite
was text written to a `.txt`, so the file-type gate was never exercised at all — twelve green
checks, and the one dimension that mattered untested.

The same suite was hiding a second hole of its own making: the "absolute home path" pattern
required a **trailing slash**, and its decoy always supplied one. A bare `/home/<user>` was
invisible for as long as the suite was green. A decoy that is more convenient than the real
payload is not a control; it is a mirror.

---

## 5. Dispersion is the finding, not the noise around it

Twice in this work the unstable spread *was* the defect, while the median looked fine.

Across this work the same full-harness boot arm has read p50 1.15 s, 2.38 s, 4.35 s and
`ready p50=1.88s` on one box within a few hours, purely as its load changed. Within a single
three-rep run, the full-turn arm spanned `min=1.96s  max=3.65s` — an 87% swing with nothing
changed but the moment it ran. Our time-to-usable-MCP-tool readings came out
`min=5.22s  max=6.97s  spread=1.75s` on n=2, where the difference between the two reps was that a
third MCP server happened to appear in one of them. A p50 drawn from any of those pairs would be a
tidy number standing for nothing.

So every instrument here prints **every repetition**, not just a midpoint, and the one with the
smallest n prints the spread explicitly and tells you to report it. Where a figure moved between
runs, this repository publishes both runs rather than the nicer one.

The related discipline: a count with no denominator beside it cannot disagree with anything.
Our active-concurrency instrument counts minutes in which *no* session was working, because they
are real — the denominator is the whole window, not the busy part of it. Measured:
`sessions with any activity in 7d: 35`, `mean=0.14`, `busy_minutes=931/10080`. Divide by busy
minutes instead and that `mean=0.14` becomes about 1.5, and the idle floor the entire argument
turns on disappears.

---

## What we changed about how we work

Four rules, each of which exists because something above went wrong first. They are in
`docs/method.md`, applied to the instruments here, and they are not specific to agents:

1. **Every instrument answers "what would this print if it were blind?"** in its own docstring,
   and under `--help-blind`. Where blind output is indistinguishable from a real zero, it needs
   a control before anyone reads its number.
2. **A control must be shown to fail.** `census_background_work.py` plants two decoy processes
   and exits 3 if it cannot see them — two, not one, because a single decoy passed identically
   on the broken and the fixed version and therefore discriminated nothing. A control that has
   never failed is decoration.
3. **Keep the failed controls in the published artefact.** Arm 3 of the caching instrument is
   shipped *as a failure*, with the reason it is unusable. Run 1 is shipped with its exit 3.
   An instrument whose failures have been tidied away looks more reliable than it is — and an
   independent reviewer told us this is precisely why the review of the underlying study
   converged in three rounds instead of six.
4. **Re-read the page before you quote it.** Of 13 quotations the source study attributed to
   third parties, **7 needed correction** when we fetched the pages ourselves: one sentence did
   not exist on the cited page at all, one was a table row presented as prose, one was on a
   different page than cited, and one — the cache-key description — was nowhere in the source.
   The mechanism was not carelessness: several quotations had been taken from *search-result
   summaries*, which paraphrase, and then printed in quotation marks as page-verbatim. The audit
   is in `docs/citations.md`, including the one case where getting it right made our argument
   weaker.

---

## What we are not claiming

The capacity conclusion in this repository is one box on one day, and the box was heavily loaded.
Codex was unauthenticated here, so its boot figure is boot-to-first-event and its capabilities
were read rather than exercised. The caching result is n=1 per arm across two runs. The
background-work census found zero, by an instrument that fails loudly when blind — but its third
version is the first one we believe, and where several sessions share one cgroup it can detect
work without attributing it. `docs/limits.md` is the full list.

The instruments are the contribution. If you run them and get something different, that is the
most useful thing you can tell us.
