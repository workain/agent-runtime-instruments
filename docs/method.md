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

**The same defect arrived again by a different route, and the route is the point.** The
loop-flag thresholds in `measure_token_breakdown.py` rest on what four agent frameworks use as
their own loop ceilings, and those numbers reached this repository through **an agent's research
brief** — a survey of published practice, written by reading *about* the frameworks rather than
reading them. Re-fetching all four changed two of the numbers: one default was reported as a
range where its own source assigns a single value, and another was cited at a version the
project has since superseded. Neither error changed the conclusion the brief was commissioned to
reach — the defaults still span two orders of magnitude, which is the whole argument — and that
is precisely why neither would have been caught by anyone checking whether the conclusion held.
A summary written by a model is not a weaker form of the primary source; it is the same hazard
as a search-result snippet in a more confident voice, and **Rule 4 does not care which produced
it**.

The same pass found a claim attached to the wrong part of a paper it correctly cited: a ranking
quoted to an abstract that does not rank anything, while the figures supporting it sit in the
body. "Fetch the page" means the page the sentence is actually on. Audit: `docs/citations.md`,
the loop-flag table.

---

## Small traps, each of which cost us a measurement

- **`pgrep -f` finds itself.** The pattern matches the searching command's own command line. Read
  cmdlines from `/proc` instead. The bash equivalent bites too: a `for` loop over `/proc` with a
  `case` match found its own subshell and reported two processes that did not exist.
- **Kernel files report size 0.** `stat` on `cgroup.procs` returns 0 bytes, so `test -s` on it is
  always false and a guard written that way never runs. Read the content.
- **A clean merge is not a correct one, and generated artefacts are where that bites.** Git
  compares two branches' versions of a file to each other; it cannot compare either to the code
  that is supposed to produce it. So a captured-output fixture can go stale with no conflict to
  resolve and nothing to review. It happened twice here. Once a branch ADDED a fixture while
  another branch changed the output format: there was no copy of the new file on the other side,
  so git had nothing to merge and carried the pre-change capture forward untouched. Once a clean
  merge produced a fixture holding one branch's header above another branch's body. Every gate
  was green both times, including the figure gate, because a stale fixture still contains the
  substring that gate pins. The only check that sees this class re-runs the instrument and
  compares byte-for-byte: `scripts/check_fixtures_fresh.sh`. Which means the producing command
  for every fixture has to be committed rather than remembered — if nobody can say what command
  made a file, nothing can tell whether it is still true.
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
  they are real. Dividing by busy minutes only turns a `mean=0.15` into about 1.4 and deletes
  the idle floor the whole argument rests on.
- **A single negative probe proves nothing — ask both surfaces, with the full SHA.** This
  repo has been misled by one-sided removal checks **twice, in opposite directions**. A
  `.pyc` blob the GitHub API 404'd was still served by `git fetch origin <full-sha>`. An
  orphaned commit the API served happily was refused by `git fetch origin <short-sha>` —
  so a short-SHA probe reported it gone. Check the API *and* a full-40-character fetch,
  and treat agreement between them as the only clean answer.
- **An absence claim needs a control proving the instrument can detect PRESENCE.**
  Otherwise "absent" and "I measured nothing" are the same output. This landed on both
  sides of this repo's review in its final minutes. A reviewer's absence sweep reported
  five figures absent; the reading was void, because `grep` on that box is **ugrep**, not
  GNU grep, it was returning **exit 2 (an error)** rather than 1 (a real no-match), and a
  `sed` in the pipeline had swallowed the status anyway. They caught it by asserting the
  same loop could find a string they knew was present — it called that absent too, which
  is how they knew the instrument was broken rather than the tree clean. The same claim
  here had been made from *empty output with no control*: same answer, reached by luck.
  So: run the positive control first, read the **exit status** rather than inferring from
  empty output, and treat a status other than 0-or-1 as an error, never as absence.
- **Configuring anonymity is not demonstrating it.** Stripping `GITHUB_TOKEN`/`GH_TOKEN`/
  `GIT_ASKPASS` and disabling the credential helper *configures* an unauthenticated probe.
  It does not prove you got one — and without that proof, "not anonymously fetchable" is
  indistinguishable from "fetched with a credential I forgot I had, and the object simply
  is not there." The demonstration is a third control: point the same probe at a
  **private** repo and require it to FAIL (`could not read Username`). This generalises
  the env-var trap elsewhere in this file: **the environment you think you set is not the
  environment you got until something proves it.**
- **Verify against an artefact that survives the machine you are typing on.** Deleting
  and recreating a repository is two operations, and the second can fail: here
  `gh repo delete` succeeded and `gh repo create` then hit a rate limit, so for some
  minutes the repository did not exist. "I had confirmed the local copy" is one copy on
  one disk. Push a complete mirror first (`git push --mirror` to a holding repo, or
  `git bundle create`) and verify the mirror's **commit count and tip SHA** before the
  delete. The rate limit is a recurring environmental fact rather than bad luck — it
  also broke the reviewer's own `gh repo view` in the same session, which is why their
  visibility check used REST.
- **A single POSITIVE probe can be measuring the wrong thing**, which is the twin of the
  rule above and was learned the same way. A removal check printed "still served" because
  its clone had failed, so the `cd` failed and `git cat-file` ran in the *local* repo,
  where the object exists. Make a probe state its own ground — print the tree it is
  running in — and refuse to conclude when its setup fails. The reviewer's version adds
  the control that makes it conclusive: count the objects **before and after** the fetch,
  because "not our ref" in a tree that already holds the object is indistinguishable from
  a real removal.
- **A force-push does not delete anything.** It makes an object unreachable, and every
  reachable-objects gate then goes green and is *correct* to: `git log --all` and a
  pre-push ancestry scan both stop at reachability. The remote keeps serving the object
  with no detector on your side at all. `scripts/check_remote_identities.sh` exists
  because that was the only blind spot nothing in this repo could see.
- **House style for counts in prose: write `label: N`, not `N things`.** The figure gate
  catches `label: N` by shape, for any label. Number-first counts are caught only via a
  hand-maintained noun list, so every new countable noun is a new blind spot — which is
  exactly how an unbackticked `103 transcripts` survived four commits. This convention
  costs nothing and needs no list. (A reviewer proposed deriving the noun list from
  `samples/` instead and then measured it: derivation surfaced `bytes`, `input`, `cache`,
  `did` and `follows` at high frequency while `transcripts` and `lines` appeared once
  each — longer, noisier, and a false-positive generator. They withdrew it. Testing a
  recommendation before making it changed the answer, for the second time in that
  review.)
- **A dangling hook symlink is silently skipped.** `ln -sf ../../scripts/<name>
  .git/hooks/pre-push` pointing at a file that is not in the repository installs
  *nothing*: git skips it, `git push` exits 0, and no warning is printed — while the
  README says you are covered. A gate that exists only on its author's disk is the
  gate-provenance failure in its purest form, and it happened here inside the remedy for
  a leak.
- **A flat offset is invisible to comparison.** A constant added to every arm leaves all ratios
  intact, so nothing looks wrong. Capture and print stderr; that is where ours was.
