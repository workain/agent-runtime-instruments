# agent-runtime-instruments

**Six read-only instruments that measure what a coding-agent session actually costs — and a
method for not fooling yourself with them.**

There are published numbers for per-session agent memory. There are very few published
*instruments*, and almost no published *methods*. This repository is the instruments, the raw
output they produced, **and every control that failed** — because the failed controls are the
only reason to believe the rest.

Everything here is `python3`, standard library only, no dependencies, one file per instrument.

---

## Why the failures are kept

Four of the defects found while building these were found *by the instruments themselves*, and
every one produced a confident, publishable-looking number first:

| Instrument | What it reported | What was actually true |
|---|---|---|
| `census_background_work.py` v1 | "0 of 16 sessions own background work" | Its filter discarded `npm exec vite`, `sh -c npm run dev` — **the realistic shapes** — before the age test. Only an unwrapped `vite --host` could ever have matched. |
| `census_background_work.py` v2 | "2 of 15" | Both hits were dead sessions' orphans; and one whole class of session lived in a different cgroup subtree it never looked at. Wrong in **numerator and denominator**. |
| `bench_mcp_connect.py` | `mcp_tool_result=3.87s` | The server under test reported `"status": "failed"` **in the same output**. The model had called `ToolSearch` with `query: "select:mcp__echo__ping"`, so the target tool's name appeared inside *another* tool's arguments, and a substring match timed a failed tool *search*. |
| `census_background_work.py` v3 | "15 live session cgroups" | Discovery used the broad "is this agent-internal?" pattern, which includes a bare `/bin/bash`, so every cgroup on the box containing a shell was counted — on a CI host that meant the **build runners** were reported as live agent sessions. |

None of these was caught by being careful. Each was caught by one question, which every
instrument here now answers in its own docstring and under `--help-blind`:

> **What would this print if it were blind?**

If the answer is "the same thing it prints when the finding is genuinely zero", the instrument
needs a control before any number it produces is worth reading.

---

## The instruments

| File | Answers | Cost |
|---|---|---|
| `measure_session_footprint.py` | What does one live session cost in RAM, **RSS and PSS**? | free, `/proc` only |
| `measure_active_concurrency.py` | How many sessions are *mid-turn* versus *resident*? | free, reads transcripts |
| `bench_cold_start.py` | How long to boot, and to a first finished turn? | boot arms free; `turn` arm = 1 request |
| `bench_mcp_connect.py` | How long to a **usable** MCP tool — the figure `init` hides? | 1 request per rep |
| `census_background_work.py` | Which sessions own work a turn boundary would kill? | free, cgroup-based, **self-testing** |
| `bench_cache_residency.py` | Does a prompt-cache hit need the writing process to still exist? | ~6 requests |

Plus `instruments/echo_mcp_server.py`, a 60-line stdio MCP server with one tool, so
`bench_mcp_connect.py` runs out of the box with no access to anyone's private MCP servers.

Every instrument takes `--help` and `--help-blind`. Several take `--anonymise`, which hashes
session identifiers so output is safe to publish; the **token counts and timings are never
altered**, only identifiers.

```bash
python3 instruments/measure_session_footprint.py --anonymise
python3 instruments/measure_session_footprint.py --list-unmatched   # "is my zero real?"
python3 instruments/census_background_work.py                        # exits 3 if blind
ROOTS=~/.claude/projects DAYS=7 python3 instruments/measure_active_concurrency.py
```

Read-only, with exactly one disclosed exception: `census_background_work.py`'s self-test spawns
two short-lived decoy processes of its own and kills them again. Nothing here writes to, moves
or deletes anything else — no database, no network calls, no configuration changes.

`samples/` holds real output from each one, **including the runs that failed**:
`bench_cache_residency.run1-redacted.out` (exit 3, a control that misread its own data),
`census_background_work.selftest-FAILED.out` (exit 3), and
`bench_mcp_connect.broken-server.out` (the counterfactual proving the fix discriminates).

---

## What these actually measured

Measured on one 16 GB Linux box running Claude Code 2.1.197 and Codex 0.147.0, 2026-10-16.
**These are readings, not constants** — see `docs/limits.md`, and note that the box was under
heavy load, which is itself visible in the spreads.

- **19 resident session trees** (`session trees: 19`): `summed RSS: 7505 MB` against
  `summed PSS: 6302 MB`, i.e. `PSS/RSS = 0.84`. Per session, RSS `p50=331 MB` and PSS
  `p50=254 MB`, 6 processes each.
- **Use PSS, not RSS, for capacity.** The CLI is a single ~323 MB compiled binary, so its
  file-backed pages are shared across every concurrent session and summed RSS counts them once
  per session. An independent third-party measurement reaches the same conclusion on different
  hardware — quoted and verified in `docs/citations.md`.
- **Readiness is not tool-ready.** Boot to the `system/init` event: `ready p50=4.35s` with the
  full harness, `ready p50=0.98s` with `--bare`, and `ready p50=3.31s` for a full trivial turn
  to `result`. But `init` is the *first* event in the stream and lists MCP servers as `pending`.
  Time to a *usable* MCP tool, against a server that does no work at all:
  `min=6.83s  max=7.68s  spread=0.85s` (n=2).
- **A prompt-cache entry outlives the process that wrote it** — and this one is token-exact.
  See below.
- `LIVE sessions owning background work older than 900s: 0`, across `LIVE session cgroups: 12`,
  by an instrument that fails loudly when it cannot see its own planted decoys. Two earlier
  versions answered this confidently and both were wrong.

### The result worth the repository

`bench_cache_residency.py` arm 5. Process **A** writes a cache under a unique marker and
**exits**. Process **B** then resumes with the *same* marker; process **C** resumes with a
*different* one. B and C differ in nothing else.

```
A (writer; exits before B and C run):   read=14848  creation=11416
B (new process, SAME marker):           read=26264  creation=2875
C (new process, DIFFERENT marker):      read=14848  creation=14307

B_read(26264) == C_read(14848) + A_creation(11416)        <- token-exact
```

B read **precisely** the bytes A wrote, on top of the prefix both share. Reproduced on an
independent run: `B_read(26265) == C_read(14848) + A_creation(11417)`
(`samples/bench_cache_residency.run3-redacted.out`), and the relation holds in a third run's
data too (`'read': 26264, 'create': 2875` in
`samples/bench_cache_residency.run2-redacted.out`, which predates the instrument printing the
arithmetic itself).

**A cache entry outlived the process that created it and was read, exactly, by a different
one.** For anyone deciding between resident sessions and a turn-scoped worker pool, that
removes the objection which looks most expensive on paper.

**What this does not establish:** n=1 per arm per run. Arm 5 ran in three runs and the
relation held in all three. But the *other* control in the same instrument — arm 4, a model
switch — held in only **one** of the three runs where the current criterion was applied
(`PRIMARY (hit-fraction) -> control HOLDS` in `samples/bench_cache_residency.out`, against
`PRIMARY (hit-fraction) -> control FAILED` in
`samples/bench_cache_residency.run3-redacted.out`). The reason is worth knowing: on a shared
box other processes keep the alternate model's cache warm, so switching models does not
reliably force a miss — and the documentation never actually says caches are model-scoped,
which was arm 4's whole premise. Arm 5 does not depend on arm 4 (it carries its own control,
C), which is why its result stands while arm 4's does not.

---

## Install, and what runs where

```bash
git clone <this repo> && cd agent-runtime-instruments
python3 instruments/measure_session_footprint.py --help
```

Linux-only in practice: `measure_session_footprint.py` and `bench_cold_start.py` read `/proc`,
and `census_background_work.py` needs cgroup v2 at `/sys/fs/cgroup`. The two benchmark
instruments that spend model requests need the `claude` CLI on `PATH` and will **skip visibly**
rather than crash or report zero if it is absent — see `samples/bench_cold_start.no-binaries.out`.

Before publishing any output, run the scrubber:

```bash
bash scripts/scrub_check.sh --all      # working tree + every blob in git history
bash scripts/test_scrub_check.sh       # 12 patterns, one decoy each: proves it can FAIL
```

---

## Decisions, and the reasons

**Licence: Apache-2.0.** It matches the licences of the projects these instruments are most
likely to be used alongside — the Agent Client Protocol, Codex and goose are all Apache-2.0 —
so output and derivations can move between them without a compatibility question. It also
carries an explicit patent grant, which a bare MIT licence does not; for measurement tooling
that may end up quoted in procurement or capacity arguments, that is worth having.

**This repository is the single source of truth for these instruments.** They were extracted
from a private study, and that study's copy is a frozen audit record of what was run on the day
— it is deliberately *not* maintained, and carries a pointer here. Fixes land here. The reason
to be explicit: two maintained copies of a *self-testing* instrument is the worst available
outcome, because each copy's self-test keeps passing while the two drift.

**Name.** Descriptive rather than clever, and deliberately not `-metrics`: there is no exporter,
no dashboard and no daemon here. These are instruments you run when you want a number.

---

## Contributing

The bar is the method, not the style. A new instrument needs: a `--help-blind` answer, at least
one control that can **fail**, a non-zero exit when it cannot measure, and real output in
`samples/` — including the runs that went wrong. An instrument whose failed controls have been
tidied away looks more reliable than it is.

If you reproduce any of these numbers and get something different, that is the most useful
thing you can report. Several of the figures here moved by 2-3x between runs minutes apart on
the same box -- `ready p50=4.35s` above was 1.15 s on an earlier, quieter run of the same arm.
`docs/limits.md` says which, and why.

---

## Further reading in this repo

- **`ARTICLE.md`** — the method: five things that were measured and the mistakes that produced
  them. Start here if you want the reasoning rather than the tools.
- **`docs/method.md`** — the rules, stated so they can be applied to instruments that are not
  these.
- **`docs/citations.md`** — every third-party quotation, with its verification status. 7 of 13
  attributed quotations in the source study needed correction; this file is the audit.
- **`docs/limits.md`** — what these instruments cannot tell you.
