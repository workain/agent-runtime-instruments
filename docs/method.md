# The method

## The defects that produced these rules

Each was found **by re-running an instrument**, not by reviewing it, and each had already produced
a confident, publishable-looking number:

| Instrument | What it reported | What was true |
|---|---|---|
| `census_background_work.py` v1 | "0 of 16 sessions own background work" | Its filter discarded `npm exec vite`, `sh -c npm run dev` — **the realistic shapes** — before the age test. Only an unwrapped `vite --host` could ever have matched. |
| `census_background_work.py` v2 | "2 of 15" | Both hits were dead sessions' orphans, and one whole class of session lived in a cgroup subtree it never looked at. Wrong in **numerator and denominator**. |
| `bench_mcp_connect.py` | `mcp_tool_result=3.87s` | The server under test reported `"status": "failed"` **in the same output**. The model had called `ToolSearch` with `query: "select:mcp__echo__ping"`, so the target tool's name appeared inside *another tool's arguments*, and a substring match timed a failed tool **search**. |
| `census_background_work.py` v3 | "15 live session cgroups" | Discovery used the broad "is this agent-internal?" pattern, which matches a bare `/bin/bash`, so every cgroup holding a shell counted — on a CI host, the **build runners** were reported as live agent sessions. |

Three more came out of review rather than re-running, and they are the same shape:

- A **`.pyc` committed into the repo** embedded an absolute build path, and the scrubber certified
  the tree CLEAN — `grep -I` skips binary files and `git log -G` does not search binary diffs. All
  twelve decoys were text, so the file-type gate was never exercised.
- The scrubber's "absolute home path" pattern **required a trailing slash**, and its decoy always
  supplied one. A bare `/home/<user>` was invisible for as long as the suite was green.
- The census self-test called its classifier with `min_age=1` against a two-second decoy while the
  published criterion was 900 s, so **`age()` was only ever exercised in a 1-2 s band**. Clamping
  `age()` at 10 s produced a confident false zero that still printed SELF-TEST PASSED. The fix was
  a leg that compares `age()` against an **independent re-computation** on a long-lived process.

Four rules. Each exists because an instrument in this repository produced a confident wrong
number first. None is specific to agents — they apply to any measurement you intend to publish.

---

## 1. Answer "what would this print if it were blind?"

Write the answer in the instrument, not in your head. Every instrument here has it in its
docstring and behind `--help-blind`.

The rule bites when **blind output is indistinguishable from a real finding**:

| Instrument | Blind output | Real-zero output |
|---|---|---|
| `measure_session_footprint.py` | `session trees: 0` | `session trees: 0` |
| `measure_active_concurrency.py` | `mean=0.00`, every percentile 0 | `mean=0.00`, every percentile 0 |
| `census_background_work.py` | `owning background work: 0` | `owning background work: 0` |

All three are identical. So each one needs something that distinguishes them, and the remedy
differs per instrument:

- `measure_session_footprint.py` has `--list-unmatched`, which prints the heaviest processes it
  did *not* classify. If your agent is in that list, the zero is a pattern defect.
- `measure_active_concurrency.py` **refuses** (exit 2) on an empty corpus, and separately on a
  corpus where no file yielded a parseable timestamp. "I looked in the wrong directory" must not
  share a shape with "nothing was active".
- `census_background_work.py` plants decoys and exits 3 if it misses them (rule 2).

Three things to look for when a result feels too clean: **uniformity** (a detector that fires on
everything discriminates nothing — one of ours reported 15 of 15 cgroups as "shared"), **a sum
that misses a known contributor**, and **instability across identical calls**.

## 2. A control must be shown to fail — and one decoy is often not enough

A control that has never failed is decoration. Demonstrate it fires:

```
$ EXTRA_INTERNAL_RE='sleep ' python3 instruments/census_background_work.py
SELF-TEST [cgroup handle (v1 reparenting defect)]: decoy pids=[...] detected=NO
SELF-TEST FAILED -- the counts below are UNMEASURED.
...
SELF-TEST FAILED -- do NOT believe the counts above.
$ echo $?
3
```

**Why two decoys.** The census self-test originally planted one, `setsid sleep N`. That decoy is
excluded by *neither* the broken filter nor the fixed one — so it passed identically on both
versions and discriminated nothing. The second decoy, `setsid sh -c 'sleep N'`, is the one the
broken filter rejects and the fixed one admits. **Each decoy must fail on a version the others
pass.** A reviewer built that second decoy; we had not.

**Three states, never two.** A skipped control must not report as a passed one. With
`SELF_TEST=0` the census prints "SELF-TEST NOT RUN — the counts above are uncontrolled", because
collapsing *skipped* into *passed* is how a control that never ran ends up claiming it fired.

**Prove a fix counterfactually.** When you fix a detector, re-run it against the exact case it
previously got wrong and keep that output. `samples/bench_mcp_connect.broken-server.out` is the
case that used to print `mcp_tool_result=3.87s` and exit 0; it now prints `NEVER-CALLED`, logs
`REFUSING this rep`, and exits 3. Without that file, the fix is an assertion.

## 3. Keep the failed controls in the published artefact

`samples/` contains runs that failed, on purpose:

- `bench_cache_residency.run1-redacted.out` — exit 3. The control criterion of the time called
  the control FAILED while cache creation had gone 2,875 → 14,005 under the perturbation. The
  control had plainly fired; the test could not see it. (Its session ids were replaced post-hoc,
  because the run predates the `--anonymise` flag; the header on the file says so.)
- `census_background_work.selftest-FAILED.out` — exit 3, both decoys missed.
- `bench_mcp_connect.broken-server.out` — the counterfactual above.
- Arm 3 of `bench_cache_residency.py` ships **as a failed control**, in the code, with the
  reason: `--append-system-prompt` lands at the *end* of the system block, so the prefix before
  it still matches. It is a suffix perturbation, not a prefix one — a design error, not a finding
  about caching.

**A criterion changed after seeing data must be declared.** The caching instrument's control test
was replaced after a run it misread. That is exactly how results get manufactured, so **both
criteria are printed on every run** and the reader can see what the change did. The reason for
the change has to stand independently of the outcome: a read-only test cannot distinguish a
*partial* miss from a hit, because a partial miss still reads the prefix that matched. True
regardless of which way that run came out.

## 4. Re-read the page before you quote it

Fetch the page. Find the sentence. Copy it. Then **read the clause after it** — in this
repository a correctly-quoted sentence about an MCP startup wait was attached to measurements it
did not govern, because the next sentence said the behaviour requires a newer version than the
box was running.

Of 13 quotations re-checked for this repository, 7 needed correction. The mechanism was
quotations taken from **search-result summaries** and printed as page-verbatim. A summary
paraphrases; quoting it as the source's own words is the defect. Audit: `docs/citations.md`.

---

## Small traps, each of which cost us a measurement

- **`pgrep -f` finds itself.** The pattern matches the searching command's own command line. Read
  cmdlines from `/proc` instead. The bash equivalent bites too: a `for` loop over `/proc` with a
  `case` match found its own subshell and reported two processes that did not exist.
- **Kernel files report size 0.** `stat` on `cgroup.procs` returns 0 bytes, so `test -s` on it is
  always false and a guard written that way never runs. Read the content.
- **Don't cache a clock.** `/proc/uptime` sampled once at import gave every later-started process
  a *negative* age, which silently failed every `age > threshold` test. The enumeration was
  correct and the clock was wrong; the self-test is what caught it.
- **A reparented process leaves the tree but stays in its cgroup.** Backgrounding with `&`
  reparents to pid 1, which is why parentage-walking missed everything and cgroups did not.
- **Match structurally, not by substring.** Searching a JSON line for a tool's name and
  `"tool_result"` matched a *different* tool whose arguments contained the target's name. Parse
  the JSON; check the block type and the exact field.
- **An error result is still a result.** A `tool_result` carrying `is_error: true` is a
  `tool_result`. Timing it reports a working server that is not working.
- **A missing field is not a zero.** Unreadable PSS prints `n/a`; an absent `/proc/meminfo` key
  prints `n/a (absent)`. A missing measurement that renders as 0 makes a session look free.
- **Use a denominator that can disagree with you.** Count the minutes in which nothing happened;
  they are real. Dividing by busy minutes only turns a `mean=0.14` into about 1.5 and deletes
  the idle floor the whole argument rests on.
- **A flat offset is invisible to comparison.** A constant added to every arm leaves all ratios
  intact, so nothing looks wrong. Capture and print stderr; that is where ours was.
