# Citation audit

Every third-party quotation this repository makes, with how it was verified.

**Why this file exists.** The study these instruments came from attributed ~40 external sources
and had verified 6 at first hand. When 13 of its attributed quotations were re-checked by
fetching the pages, **7 needed correction.** The mechanism was not carelessness about any one
sentence: several quotations had been taken from **search-result summaries**, which paraphrase,
and then printed in quotation marks as if page-verbatim. That is a class of defect, not three
accidents, so the remedy has to be mechanical — fetch the page, find the sentence, copy it.

**Method used here.** Each URL was fetched and its body text read. Where a fetch returned a
model-written summary rather than page text, raw bytes were pulled separately and the two
cross-checked; both agree on every line below. A quotation that could not be found on the page is
marked NOT PRESENT and is **not used anywhere in this repository**.

---

## Used, and verified verbatim

| Quotation | Source | Used in |
|---|---|---|
| "Summing RSS instead would count the shared runtime once per agent: the same ramp reads 340 MB per session in RSS against 238 MB in PSS." | [omg.dev/tools/coding-agent-capacity](https://omg.dev/tools/coding-agent-capacity) | `ARTICLE.md` §2, `README.md` |
| "the median session's whole process tree measured 238 MB and the p90 measured 997 MB" | same | available, not currently quoted |
| "each additional one costs about the same 167 MB" | same | available, not currently quoted |
| "**When you pass `--mcp-config` with `-p`,** Claude Code waits for still-pending servers before running the first turn, up to the `MCP_TIMEOUT` startup timeout, 30 seconds by default." | [code.claude.com/docs/en/headless](https://code.claude.com/docs/en/headless) | `ARTICLE.md` §3 — **the opening clause was previously dropped**, which mattered: our boot runs did not pass `--mcp-config`, so that is a *second* independent reason the 30 s ceiling never applied to them. Dropping a governing clause is the same defect §3 is about. |
| "The wait requires Claude Code v2.1.221 or later." | same | `ARTICLE.md` §3 — **this is the clause that corrected us** |
| "A remote server with a cached tool list skips the wait, shows `pending` in `system/init`, and connects on its first tool call." | same | `instruments/bench_mcp_connect.py` docstring |
| "Claude Code finds the session by its ID in any project on this machine." | same (**headless** page, *not* the CLI reference) | context for `--resume` |
| "Exact matching: Cache hits require 100% identical prompt segments, including all text and images up to and including the block marked with cache control." | [platform.claude.com/docs/en/build-with-claude/prompt-caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) | `instruments/bench_cache_residency.py` |
| "For concurrent requests, note that a cache entry only becomes available after the first response begins." | same | `docs/limits.md` |
| "The lookback window is 20 blocks. The system checks at most 20 positions per breakpoint, counting the breakpoint itself as the first." | same | `docs/limits.md` |
| "By default, the cache has a 5-minute lifetime." | same | `instruments/bench_cache_residency.py` |

## Corrections — what the source study said, and what the page says

| # | Study's version | Verdict | Reality |
|---|---|---|---|
| 1 | Cache key is "derived from the exact bytes of the rendered prompt up to each `cache_control` breakpoint" | **NOT PRESENT** | Neither "exact bytes" nor "rendered prompt" occurs on the page. Its concept is a *cumulative hash of the prefix* ending at the breakpoint. **Quotation dropped; the mechanism is restated in our own words.** |
| 2 | Caches are **model-scoped** | **NOT PRESENT** | The page's "Cache storage and sharing" section names only organization- and workspace-level isolation. Model-scoping may be true of the system, but **it cannot be cited to this page** — and our own arm 4 assumed it, which is why that arm is now a questionable control. See `ARTICLE.md` §1. |
| 3 | A cache entry is readable only after the first response "begins streaming" | **WORDING** | The page says "after the first response **begins**". "Streaming" is ours. Corrected. |
| 4 | A breakpoint "walks backward at most 20 content blocks" | **WORDING** | The unit is **positions**, not content blocks, and "a run of consecutive `tool_use` blocks counts as one position" — so a turn with many parallel tool calls does not consume the window as fast as "20 blocks" implies. Corrected. |
| 5 | Cache reads at ~0.1× input price | **INCOMPLETE** | 0.1× is the documented *standard* multiplier, with **per-model exceptions**: the page's footnotes give 0.025× for Claude Fable 5.1 and Mythos 5.1, and 0.05× for Opus 5.5 and Sonnet 5.5. Any cost model built on a flat 0.1× is wrong for four current models. |
| 6 | "32 concurrent sessions totaled 5.36 GB, with a median of 171 MB per session" | **NOT A SENTENCE** | Those figures are a **table row**, not prose; the page's own prose rounds them to 5.4 GB. Printing it in quotation marks would quote a sentence that does not exist. **Not used.** |
| 7 | "finds the session by its ID in any project on this machine" cited to the CLI reference | **WRONG PAGE** | The CLI reference mentions `--resume` eleven times and contains this sentence zero times. It is on the headless page. Re-attributed. |

**One correction made our argument weaker, and it is the most important one here.** Dropping
the model-scoping claim (#2) removes the justification for arm 4 of the caching instrument — the
control we had been describing as "the one that actually works". It is now published as a
control whose premise is unverified and which failed on two of three runs. The headline caching
result survives only because arm 5 carries a different control.

Two corrections made the argument *stronger*: the version clause (#4 in `ARTICLE.md` §3) and the
RSS/PSS sentence, which is better evidence for the accounting choice than anything originally
quoted.

---

## The loop-flag thresholds: four frameworks' own defaults, read at first hand

`instruments/measure_token_breakdown.py`'s LOOP FLAGS section ships **no default threshold**, and
these four readings are the reason. Each was fetched for this repository and quoted from the
framework's own reference page or its own source file — not from a summary about it.

| Quotation | Source | Used in |
|---|---|---|
| `max_iterations: int \| None = 15` — "The maximum number of steps to take before ending the execution loop. Setting to 'None' could lead to an infinite loop." | [`langchain_classic/agents/agent.py`](https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/langchain/langchain_classic/agents/agent.py) (class attribute, read from source because the hosted API reference no longer renders it) | `measure_token_breakdown.py` docstring |
| "Starting in version 1.0.6, the default recursion limit is set to 1000 steps." | [docs.langchain.com/oss/python/langgraph/graph-api](https://docs.langchain.com/oss/python/langgraph/graph-api) | same |
| `DEFAULT_MAX_TURNS = 10` | [`openai-agents-python/src/agents/run_config.py`](https://raw.githubusercontent.com/openai/openai-agents-python/main/src/agents/run_config.py) — the docs page names the constant but not its value, so the value was read from source | same |
| `max_turns` \| `int \| None` \| default `None` \| "Maximum agentic turns (tool-use round trips)" | [code.claude.com/docs/en/agent-sdk/python](https://code.claude.com/docs/en/agent-sdk/python), `ClaudeAgentOptions` reference table | same |
| "1600+ annotated traces collected across 7 popular MAS frameworks"; "This process identifies 14 unique modes, clustered into 3 categories"; "step repetitions (FM-1.3, 15.7%)" | MAST, [arXiv:2503.13657](https://arxiv.org/abs/2503.13657) v3 — first two from the abstract, the third from §4 of the PDF | same (signal 2's deferral rationale) |

**Rule 4 fired here, on a summary written by another agent.** These numbers reached this
repository through a research brief, and re-fetching the four framework pages changed two of
them: the brief gave the OpenAI default as a "5-10" range where the source assigns a single
`DEFAULT_MAX_TURNS = 10`, and cited LangGraph's superseded pre-1.0.6 value of 25 alongside the
current one. Neither error changed the conclusion — the defaults still span two orders of
magnitude, which is the whole point — but both would have been published as page-verbatim
facts. **This is the same defect as the seven corrections above, arriving by a new route:**
previously from search-result summaries, now from an agent's research summary. The remedy is
identical and does not care which.

**One claim was checked and is NOT supported by the abstract.** The brief stated that MAST finds
step repetition "the single highest-frequency failure mode of all 14 categories". The *abstract*
does not rank the modes at all, so that sentence cannot be cited to it. It is supported by the
paper's §4 body text, where all fourteen per-mode percentages appear: FM-1.3 at 15.7% is the
largest, ahead of FM-2.6 (reasoning-action mismatch) at 13.2%. Cited to the body, with the
comparison named, rather than to the abstract where a reader would not find it.

---

## Quoted-from versus reported-by

A page with a commercial interest in the claim is still usable, but the interest has to be
visible.

### Our load-bearing citation has a commercial interest, and this was missed

**`omg.dev/tools/coding-agent-capacity`** — the source of the RSS-vs-PSS sentence quoted in
`ARTICLE.md` §2 and `README.md` — **sells per-session agent capacity** (its page advertises an
"Always On 24 sessions 36 GB" tier). "Use PSS, not RSS" is precisely the accounting choice under
which *more sessions fit per machine*, which is the thing being sold.

The quotation is verbatim and we verified it against raw page bytes. But this is our **only
load-bearing third-party citation**, and the disclosure rule stated below was applied to a
different source and not to this one — found by an independent reviewer. Stated now:

- the sentence is accurate, and
- the source has an interest in the conclusion it supports, and
- our own measurement is independent of it (`samples/measure_session_footprint.out` shows the
  RSS/PSS gap on our own hardware), so the argument does not rest on their number.

### A competing vendor's page, disclosed from the start

These two sentences are accurate and verbatim:

> "They are also deliberately task-scoped: idle sessions expire, the VM is reclaimed, and
> anything still running inside is gone." … "There is no way to keep a daemon alive."
>
> — [agent37.com/blog/claude-code-cloud](https://agent37.com/blog/claude-code-cloud)

**Disclosure:** that page advertises a competing always-on hosted product two sentences later.
The quotations describe a limitation of someone else's product in a post selling the alternative.
We consider them reliable because they are specific and checkable against vendor documentation,
but a reader should know the source's position. They are **not load-bearing** for anything in
this repository.

---

## Not verified at first hand

Stated so the gap is visible rather than implied absent. These were cited in the source study,
are **not** relied on anywhere here, and should be treated as reported-by until someone reads
the page: OpenHands' "one pod per active conversation"; the Agent Client Protocol adoption
figure "over 25 agents"; the durable-execution phrase "without holding a process open"; the
OpenCode mutual-restart issue; and the upstream Claude Code memory-growth issue bodies.

The source study's own honest denominator was **6 of ~40 verified**. This file brings the count
of *first-hand verified* quotations to 13 — but only across the quotations this repository
actually uses. (The five loop-flag readings above are **outside** that denominator: they are not
the study's quotations and were never part of the 13 re-checked, so they neither raise nor lower
the "7 of 13" figure. Counting them in would quietly improve a ratio by adding work that was
never in question.) The remaining ~27 from the study were not re-checked, because nothing here
depends on them.

### The "7 of 13" denominator, enumerated

A reviewer pointed out that "7 of 13" could not be reconstructed from the two tables above,
because one of the seven (the version clause) sits in the *verified* table rather than the
corrections table. Enumerated, so the count is checkable rather than asserted:

**The 13 quotations re-checked:** (1) RSS-vs-PSS, (2) median/p90 session tree, (3) "each
additional one costs about the same 167 MB", (4) the 32-session totals, (5) the `MCP_TIMEOUT`
wait, (6) `--resume` finds the session by id, (7) the cache-key description, (8) cache entry
readable after the first response, (9) the 20-block lookback, (10) TTL and price multipliers,
(11) model-scoped caches, (12) cloud sessions task-scoped, (13) "no way to keep a daemon alive".

**The 7 that needed correction:** (4) not a sentence at all, a table row — dropped. (5) the
governing clause `"When you pass --mcp-config with -p"` was dropped, and the following sentence's
version requirement was missed. (6) attributed to the wrong page. (7) not present on the page —
dropped. (8) "begins streaming" where the page says "begins". (9) *positions*, not content
blocks. (11) not present on the page — claim dropped.

**The 6 that stood as quoted:** (1), (2), (3), (10) (with the per-model exceptions added), (12),
(13).
