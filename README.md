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

`instruments/echo_mcp_server.py` is a 60-line stdio MCP server with one tool, so
`bench_mcp_connect.py` runs out of the box without access to anyone's private servers.
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

### Which engines are actually supported

`instruments/engines.py` holds one adapter per engine, and **every instrument prints which
adapters are measured and which are guesses, on every run**:

- **Verified** (run, with output kept in `samples/`): Claude Code, its ACP host, Codex.
- **Untested adapters** — patterns we believe from documentation, *not* measurements: goose,
  aider, opencode, cursor-agent. Each says in the file why it may be wrong (opencode is
  client/server, so its sessions are probably not separate processes at all).

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
ln -sf ../../scripts/preflight.sh      .git/hooks/pre-commit
ln -sf ../../scripts/pre-push-hook.sh  .git/hooks/pre-push
```

Verify both symlinks resolve. A **dangling** hook symlink is silently skipped by git —
`git push` exits 0 with no warning — so a reader following these two lines against a file
that isn't in the repo installs nothing while being told they are covered. That happened
here.

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
