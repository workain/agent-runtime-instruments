#!/usr/bin/env python3
"""Where did one agent session's tokens actually go -- by tool, by subagent, by subsession?

An agent session's cost is dominated by the four token counts the engine records per
assistant turn (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`). This reads real Claude Code transcript `.jsonl` files and
splits that spend three ways:

  BY TOOL       -- group each usage-bearing turn by the SET of tool names that turn's own
                   assistant message invoked (`tool_use` blocks). This is TURN-GRAIN
                   attribution, not a per-call split: a turn that calls N tools has ONE
                   `usage` entry that cannot be divided exactly among them, and this
                   instrument does not pretend it can. A turn that calls Read and Edit is
                   reported under the pair `Edit+Read`, not half to each.
  BY AGENT      -- separate same-session Task-tool subagent turns (`isSidechain: true`)
                   from the main thread, grouped by subagent type (`attributionAgent`)
                   where the transcript records one. These are sidechains WITHIN one
                   session's transcript, NOT separate child sessions -- those are the
                   subsession rollup below.
  BY SUBSESSION -- an optional caller-supplied manifest (plain JSON) describing a tree of
                   SEPARATE sessions/transcripts, summed bottom-up to each parent. Cross-
                   session parent/child linkage is harness-specific, so this instrument
                   does not infer it; the caller states it, and this repo stays harness-
                   agnostic.

  LOOP FLAGS   -- two mechanical (non-LLM) signals correlated with excessive work cycles,
                   computed from transcript STRUCTURE alone -- turn boundaries and
                   `tool_use` block counts -- and never from judging what was SAID:
                   (1) tool calls in each span between consecutive REAL user turns, and
                   (3) runs of consecutive assistant turns that called no tool at all.
                   (2) is a deliberate gap -- see SIGNAL 2 below.

THESE ARE FLAGS, NOT VERDICTS. A flagged span says "an unusual amount of work happened
here by this threshold", which is a pointer for a human or a later semantic layer to look
at. It is NOT a finding that the work was wasted, redundant, or wrong: a 40-call span can
be a correct, necessary refactor and a 3-call span can be a pointless loop. This
instrument measures and surfaces; it does not judge a session, and it will not be made to
by adding a pass/fail line to its output.

NO THRESHOLD IS SHIPPED AS A DEFAULT, AND THAT IS THE MEASURED POSITION. The obvious
default -- "more than 20 tool calls after one user turn is abnormal" -- was checked
against published practice before being implemented, and nothing grounds it. The agent
frameworks' own loop ceilings, each read at first hand from its own reference or source
(docs/citations.md), are two orders of magnitude apart and none of them is derived from a
task:

    langchain_classic AgentExecutor   `max_iterations: int | None = 15`
    LangGraph recursion_limit         "Starting in version 1.0.6, the default recursion
                                       limit is set to 1000 steps."
    OpenAI Agents SDK                 `DEFAULT_MAX_TURNS = 10`
    Claude Agent SDK ClaudeAgentOptions  `max_turns` | `int | None` | default `None`

Three defaults spanning 10 to 1000 and one framework that declines to have one. They are
cost and runaway-loop backstops, not non-convergence detectors. So this instrument always
reports the DISTRIBUTION, which needs no constant, and flags only against a threshold the
caller sets explicitly (`--flag-span-tool-calls` / `ARI_FLAG_SPAN_TOOL_CALLS`,
`--flag-no-tool-streak` / `ARI_FLAG_NO_TOOL_STREAK`). Pick one from your own
distribution; whatever you pick is a starting point for recalibration, not a validated
constant, and this tool does not know a better one.

(Two of those four numbers arrived here wrong, from a research summary that had been
written by reading about the frameworks rather than reading them -- one default was
reported as a 5-10 range where the source assigns a single 10, another cited a superseded
value. Re-fetching each page is rule 4 of docs/method.md, and it changed the answer
again.)

WHAT A "REAL USER TURN" IS, AND THE TRAP IN NOT ASKING. Spans are delimited by operator
input, so everything turns on which `type: "user"` entries ARE operator input -- and in a
real Claude Code transcript, most are not. The engine hands every tool's output back as a
`user` entry carrying `tool_result` blocks, so counting those would cut a span at every
single tool call and this signal would report that nothing ever exceeds anything.
`isMeta: true` entries are synthetic prompts the engine wrote, including the relaunch stub
`"Continue from where you left off."`, which read as the operator asking for more work
when nothing asked for anything.

    One private corpus of 43 real transcripts, surveyed once with this file's own
    classifier while building it: of 4552 `user` entries, 4099 were tool_result carriers,
    26 were isMeta, 2 were engine-injected `<task-notification>` prompts, and 425 were
    actual operator input. A naive reader of `type: "user"` would therefore have
    overstated the operator's turns by roughly tenfold, and cut every span to about one
    tool call. That is a SNAPSHOT of a corpus that was still being written to as it was
    read -- an earlier pass of the same directory counted 4466 entries -- and it is a
    corpus nobody else can run, which is exactly why no number from it is used as a
    constant anywhere in this instrument. `--survey-user-entries` prints the same census
    for YOUR corpus; every normal run prints the per-reason exclusion counts for the
    transcripts it just read. Check that shape before trusting signal 1 on an unfamiliar
    format.

Every exclusion is structural -- a block type or an exact field, never a substring of
anyone's text -- and each is counted and printed, so an over-eager rule is visible rather
than silent. Where an entry cannot be classified it counts as REAL, which splits a span
and lowers every count: of the two directions to be wrong in, that is the one that cannot
manufacture a finding.

SIGNAL 2 IS NOT IMPLEMENTED HERE, deliberately and visibly. Repeated/near-duplicate tool
invocations are the signal published work actually names as dominant: MAST's failure
taxonomy, built from "1600+ annotated traces collected across 7 popular MAS frameworks",
rates "step repetitions (FM-1.3, 15.7%)" the most frequent of its 14 modes -- the next
highest is reasoning-action mismatch at 13.2% -- and that is identical-action-without-
state-change, which needs no LLM to detect. It is exactly what this instrument should
flag next. It is deferred rather
than approximated because a tool-NAME-only match ("Bash called 9 times") is noise in a
Bash-heavy session, and the command-level clustering that makes it meaningful is being
built separately. `--help-blind` says so, the run prints it as NOT IMPLEMENTED, and no
partial version of it is reported under its name.

`--json` emits a compact digest (totals, the two breakdowns, the loop flags, the N
costliest turns by cache-read, a cache_read:output ratio per node) sized to hand to an LLM
for qualitative review. It is deliberately NOT the raw transcript, which runs to tens of MB.

ONE MEASUREMENT TRAP THIS REPO WOULD OTHERWISE HIT. A single logical assistant turn is
written to the transcript as SEVERAL `.jsonl` entries that share one `message.id` -- a
`thinking` block, then one entry per `tool_use` block -- and EVERY one of those entries
repeats the SAME `usage` object. Summing `usage` over raw entries therefore multiplies a
turn's cost by its block count. This instrument keys every turn by `message.id` and counts
its `usage` ONCE, unioning the `tool_use` names across the turn's entries. The positive
control (`--self-test`) builds one turn as three entries repeating a single `usage` and
fails if the dedup regresses -- a reproducible stand-in for the production transcripts
where this was first seen.

Read-only: opens the transcript and manifest paths for READING only. No writes, no
network, no database. `--self-test` builds its fixture in memory and writes nothing.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    Its most dangerous blind output is `total=0`, which looks exactly like a free
    session. So a zero is never printed as a measurement: the run REFUSES (exit 2) in
    every case where it measured nothing, and prints an attribution-coverage line on
    every run so a partial blindness is visible rather than averaged away. Four ways to
    be blind, and the guard for each:

      1. NO TRANSCRIPT. A path that does not exist or cannot be read yields no turns. If
         NO source is readable the run REFUSES (exit 2): "I looked in the wrong place"
         must not share a shape with "this session was free".
      2. A TRANSCRIPT WHOSE USAGE FIELDS ARE WRONG -- wholly or in PART. If NO assistant
         turn carries any recognised `usage` field (an older format, every field renamed)
         the run REFUSES (exit 2) rather than printing `total=0`. The subtler case is a
         SINGLE renamed field: three fields still parse, so the turn is still "usage-
         bearing", but the renamed one reads as a silent 0 for a whole token dimension --
         `cache_read` quietly dropping to 0 across a real session is exactly the "looks
         free" trap. So presence is tracked PER FIELD: any canonical field absent from
         EVERY usage-bearing turn is treated as unmeasured and the run REFUSES (exit 2),
         and a field missing from SOME turns prints a WARNING that its total is a floor.
         A zero cost is a MEASURED zero only when that field was actually present.
      3. A MANIFEST NODE WHOSE PATH IS MISSING/UNREADABLE. It is reported as `n/a` in the
         rollup and contributes nothing to its parent's sum -- never silently dropped and
         never counted as 0, because a child that cost nothing and a child we could not
         read must not look the same. The count of `n/a` nodes is printed.
      4. SUBAGENT TYPE NOT RECORDED. Some sidechain turns carry no `attributionAgent`.
         They are still counted as sidechain spend, under "(subagent type not recorded)",
         never merged into the main thread -- the by-agent split would otherwise under-
         report subagent cost whenever the label happened to be absent.

    Three more ways to be blind belong to the LOOP FLAGS specifically, because the flags'
    dangerous output is the opposite of a zero -- it is a reassuring "nothing flagged":

      5. NO IDENTIFIABLE USER TURNS AT ALL. A transcript whose `user` entries are all
         `tool_result` carriers or all `isMeta` -- a sidechain-only export, a format whose
         user entries are shaped differently, a mid-session fragment -- has no span
         boundaries, so signal 1 cannot be computed. The whole file would collapse into
         ONE span, whose call count then exceeds any threshold and reads as a dramatic
         finding about a session nobody can see the turns of. So zero real user turns is
         reported as UNMEASURED and NO span is flagged -- the one case where this
         instrument declines to flag precisely because flagging would be easy.
      6. A THRESHOLD OF 0 OR LESS. `0` flags every span including the empty ones (a user
         turn answered with no tool call at all is a span of 0), and a negative threshold
         flags all of them unconditionally. A detector that fires on everything
         discriminates nothing, so a threshold below 1 is REFUSED (exit 2) rather than
         honoured -- in the CLI flag and in the env var alike, since an env var you
         believe you set is not an env var you got. An unparseable env value is refused
         the same way, never silently ignored back to "no threshold".
      7. NO THRESHOLD SET AT ALL -- the default. "0 spans flagged" would then be a
         statement about the absent threshold, not about the session, and it would look
         exactly like a clean run. So the unset case prints NOT COMPUTED, a THIRD state
         distinct from both "flagged" and "clean", and the distribution is printed anyway
         so the run is still worth something without one.

    The positive control: `--self-test` builds a synthetic transcript with hand-computed
    totals (including a split turn, a sidechain, and a no-tool turn) and asserts the
    computed numbers match EXACTLY. TWO negative controls run in the same pass, because a
    control defeated by a one-field change is no control: (a) rename ALL usage fields and
    assert every turn reads as unmeasured and the run refuses; (b) rename a SINGLE field
    and assert the per-field diagnostic flags that one dimension as absent and the run
    still refuses -- the weaker all-or-nothing check passed (b) silently.

    The flags carry their own controls in the same pass, built so each can fail on a
    change the others survive: a POSITIVE fixture that exceeds both thresholds and must be
    flagged; a NEGATIVE fixture under both that must report clean (not "not computed");
    a NEAR-MISS pair at exactly the threshold and exactly one over, asserting the boundary
    is strictly `>` in both directions rather than trivially always-true; a span fixture
    whose `user` entries are `tool_result` carriers and `isMeta` stubs, asserting it reads
    as ONE span rather than as one per tool call (delete either exclusion and this control
    goes red); and the refusals for a zero/negative/unparseable threshold and for a
    transcript with no real user turn. Nine more legs exist because a mutation sweep found
    each of them missing -- including one that caught a real leak: `--anonymise` was
    hashing the span rows and not the FLAGGED rows, which are separate copies of the same
    records, so the one output mode whose job is to omit the transcript path was printing
    it. That leg now asserts over the whole serialised digest.
"""
import argparse
import hashlib
import json
import os
import sys
import textwrap

# The four token fields every assistant turn's `usage` block carries. A turn is
# "usage-bearing" iff at least one of these is present AND numeric -- which is exactly the
# test the negative control trips by renaming them.
USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens")

NO_TOOL = "(no tool call)"
NO_AGENT_TYPE = "(subagent type not recorded)"
MAIN = "main thread"


def anonymise(tag):
    """Hash an identifier the same way the other instruments do: `sess-<8 hex>`.

    Applied only to transcript paths and caller-supplied manifest labels -- the things
    that can identify a session. Tool names, subagent TYPES, and every token count are
    left untouched: they are the evidence, not the identifier.
    """
    if not tag:
        return tag
    return "sess-" + hashlib.sha256(str(tag).encode()).hexdigest()[:8]


def num(v):
    """A usage field that is absent (None) or non-numeric contributes 0 to a sum, but the
    turn only counts as usage-bearing if SOME field was numeric -- see collect_turns."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def collect_turns(lines):
    """Reduce a transcript's lines to one record per logical assistant turn.

    Keyed by `message.id` so a turn split across a thinking entry and several tool_use
    entries -- all repeating the same `usage` -- is counted ONCE, with its tool names
    unioned and its tool CALLS counted by distinct `tool_use` id. Returns (turns, stats).

    Also walks the `user` entries, which the token breakdown has no use for but the loop
    flags are delimited by: each turn is stamped with the index of the span it falls in
    (`span`), advanced only by a REAL user turn -- see user_exclusion_reason. Turns before
    the first real user turn carry span 0.
    """
    turns = {}            # message-id -> turn record (insertion order preserved)
    order = []
    stats = dict(bad_json=0, assistant_entries=0, user_entries=0,
                 real_user_turns=0, user_excluded={})
    span = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            stats["bad_json"] += 1
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "user":
            stats["user_entries"] += 1
            why = user_exclusion_reason(d)
            if why:
                stats["user_excluded"][why] = stats["user_excluded"].get(why, 0) + 1
            else:
                stats["real_user_turns"] += 1
                span += 1
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        stats["assistant_entries"] += 1
        # message.id is the API response id and the right dedup key. Fall back to the
        # per-request/entry id so a turn missing it still counts as its own turn rather
        # than merging with another.
        mid = msg.get("id") or d.get("requestId") or d.get("uuid") \
            or ("_noid", stats["assistant_entries"])

        names, call_ids, idless = set(), set(), 0
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                # structural match: block type AND the exact name field, never a substring
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    nm = b.get("name")
                    if isinstance(nm, str) and nm:
                        names.add(nm)
                    # a call is counted even when its NAME is missing: it happened, and
                    # counting only named calls would let a missing label manufacture a
                    # "no tool call" streak (see no_tool_streaks).
                    bid = b.get("id")
                    if isinstance(bid, str) and bid:
                        call_ids.add(bid)
                    else:
                        idless += 1

        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None

        t = turns.get(mid)
        if t is None:
            t = dict(tools=set(), usage=None, has_usage=False,
                     sidechain=False, agent=None,
                     call_ids=set(), calls_idless=0, span=span)
            turns[mid] = t
            order.append(mid)
        t["tools"] |= names
        t["call_ids"] |= call_ids
        t["calls_idless"] += idless
        if d.get("isSidechain"):
            t["sidechain"] = True
        if not t["agent"] and isinstance(d.get("attributionAgent"), str):
            t["agent"] = d.get("attributionAgent")
        if usage is not None and not t["has_usage"]:
            vals = {k: usage.get(k) for k in USAGE_FIELDS}
            if any(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in vals.values()):
                t["usage"] = vals
                t["has_usage"] = True
    return [turns[m] for m in order], stats


def totals_of(turns):
    """Sum each usage field over the usage-bearing turns (None -> 0)."""
    acc = {k: 0 for k in USAGE_FIELDS}
    n = 0
    for t in turns:
        if not t["has_usage"]:
            continue
        n += 1
        for k in USAGE_FIELDS:
            acc[k] += num(t["usage"].get(k))
    acc["turns"] = n
    return acc


def ratio(acc):
    """cache_read : output. The overhead a session pays re-reading its own cached prefix
    per unit of new output. None when there is no output to divide by (not 0 -- an
    undefined ratio and a zero ratio are different facts)."""
    o = acc.get("output_tokens", 0)
    return (acc.get("cache_read_input_tokens", 0) / o) if o else None


def group(turns, keyfn):
    out = {}
    for t in turns:
        if not t["has_usage"]:
            continue
        out.setdefault(keyfn(t), []).append(t)
    return {k: totals_of(v) for k, v in out.items()}


def tool_key(t):
    return "+".join(sorted(t["tools"])) if t["tools"] else NO_TOOL


def agent_key(t):
    if not t["sidechain"]:
        return MAIN
    return t["agent"] or NO_AGENT_TYPE


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def coverage(turns):
    total = len(turns)
    ub = [t for t in turns if t["has_usage"]]
    # PER-FIELD presence, not just "has any field": a single renamed field leaves a turn
    # usage-bearing (the other three parse) while silently zeroing one whole dimension.
    present = {k: 0 for k in USAGE_FIELDS}
    full = 0
    for t in ub:
        got = sum(1 for k in USAGE_FIELDS if _is_num(t["usage"].get(k)))
        for k in USAGE_FIELDS:
            if _is_num(t["usage"].get(k)):
                present[k] += 1
        if got == len(USAGE_FIELDS):
            full += 1
    side = [t for t in turns if t["sidechain"]]
    side_no_type = sum(1 for t in side if not t["agent"])
    return dict(total_turns=total, usage_bearing=len(ub),
                without_usage=total - len(ub),
                fully_attributed=full, field_present=present,
                fields_entirely_absent=[k for k in USAGE_FIELDS if present[k] == 0],
                fields_partially_absent=[k for k in USAGE_FIELDS
                                         if 0 < present[k] < len(ub)],
                sidechain_turns=len(side), sidechain_without_type=side_no_type)


def refusal_reason(cov):
    """Return why this corpus cannot be measured, or None if it can.

    Two refusals, so a renamed field cannot pass as a measured zero: no usage AT ALL, or
    a whole canonical token dimension absent from every usage-bearing turn. A field absent
    from only SOME turns is a floor (a WARNING), not a refusal -- it was measured somewhere.
    """
    if cov["usage_bearing"] == 0:
        return (f"{cov['total_turns']} assistant turns found but NONE carried a recognised "
                f"usage field, so total=0 would be no measurement, not a free session")
    if cov["fields_entirely_absent"]:
        return (f"usage was found, but the field(s) "
                f"[{', '.join(cov['fields_entirely_absent'])}] are ABSENT from every "
                f"usage-bearing turn -- a renamed or unsupported field reads as a silent 0 "
                f"for that whole dimension, which is no measurement")
    return None


# ---------------------------------------------------------------------------
# LOOP FLAGS -- mechanical signals from transcript STRUCTURE. FLAGS, NOT VERDICTS.
# ---------------------------------------------------------------------------
# Why a `type: "user"` entry can fail to be a REAL user turn. Every reason here is
# structural -- a block type or an exact field, never a substring of the text -- and every
# one is COUNTED and PRINTED on each run, because an exclusion rule this load-bearing must
# be visible: get it wrong in the generous direction and every tool call ends a span, get
# it wrong in the strict direction and a relaunch artefact reads as the operator asking
# for more work.
USER_EXCLUSIONS = (
    ("isMeta",
     "isMeta:true -- a prompt the engine synthesised, e.g. the relaunch stub "
     "\"Continue from where you left off.\""),
    ("tool_result",
     "carries tool_result blocks only -- the engine handing a tool's output back, which "
     "is the overwhelming majority of user entries in a real transcript"),
    ("engine_injected",
     "promptSource names the engine itself (e.g. a <task-notification> injected when a "
     "background task ends), with no isMeta to mark it"),
)

# `promptSource` values that mean "the engine wrote this prompt, not a person". Engine-
# specific by nature: a transcript format without the field simply has no entry excluded
# this way, which is the floor direction (such an entry reads as a real user turn and
# splits a span) rather than the one that invents work.
ENGINE_PROMPT_SOURCES = ("system",)


def user_exclusion_reason(d):
    """Why this `user` entry is NOT operator input, or None if it is.

    First match wins and the order is the order of USER_EXCLUSIONS, so an isMeta entry
    that also carries tool_result blocks is counted once, under isMeta.
    """
    if d.get("isMeta") is True:
        return "isMeta"
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        # `kinds` empty (an empty content list) is deliberately NOT excluded: an entry we
        # cannot classify counts as a real user turn, which splits a span and LOWERS every
        # span's count. Erring toward under-flagging is the direction that cannot
        # manufacture a finding.
        if kinds and kinds <= {"tool_result"}:
            return "tool_result"
    if d.get("promptSource") in ENGINE_PROMPT_SOURCES:
        return "engine_injected"
    return None


def turn_calls(t):
    """How many tool calls one logical assistant turn made.

    NOT `len(t["tools"])`: that is the SET of tool names, so a turn that called Bash nine
    times would count as one. Counted by distinct `tool_use` block id, which is the
    engine's own per-call identity and survives the same split-turn trap `message.id`
    handles for usage (measured on 2319 real tool_use blocks: none missing an id, and no
    id repeated inside one turn). A block with no id cannot be deduped, so each such
    occurrence counts as its own call -- an idless block written twice would over-count,
    which is stated rather than silently assumed away.
    """
    return len(t["call_ids"]) + t["calls_idless"]


def quantile(xs, pct):
    """Nearest-rank (inverse-ECDF) quantile over an already-sorted list; None when empty.

    `pct` is an INTEGER percentile and the rank is computed with integer ceiling division,
    deliberately: the obvious float form `int(round(pct/100 * (n-1)))` picks its index by
    Python's banker's rounding at exact .5 and by whichever way `0.9 * n` last rounded in
    binary, so a p50 over two spans would depend on a tie-break convention no reader of
    the output could predict. This form has no floats in it.

    No interpolation either -- these are counts of tool calls, and a p90 of 20.5 calls is
    not a thing that happened. None for an empty list rather than 0: an undefined quantile
    and a measured zero are different facts, the same rule `ratio()` follows above.
    """
    if not xs:
        return None
    rank = -(-pct * len(xs) // 100)          # integer ceil(pct/100 * n)
    return xs[min(len(xs), max(1, rank)) - 1]


def spans_of(turns, real_user_turns):
    """One row per span between consecutive real user turns, in transcript order.

    EMPTY SPANS ARE INCLUDED. A user turn answered with no tool call at all is a span of
    0, and dropping those would quietly raise every quantile of the distribution and make
    a chatty session look busy. It is also exactly why a threshold of 0 is refused: it
    would flag them.

    Span 0 holds any assistant turns BEFORE the first real user turn (a transcript that
    opens mid-session, or a sidechain-only export). It is reported as the preamble and
    carried in the distribution, because the work in it is real work; it is simply not
    attributable to an operator request this file can see.
    """
    by = {}
    for t in turns:
        by.setdefault(t["span"], []).append(t)
    idxs = ([0] if 0 in by else []) + list(range(1, real_user_turns + 1))
    rows = []
    for i in idxs:
        ts = by.get(i, [])
        rows.append(dict(index=i, preamble=(i == 0),
                         turns=len(ts),
                         calls=sum(turn_calls(t) for t in ts)))
    return rows


def no_tool_streaks(turns):
    """Runs of consecutive assistant turns that called NO tool, per thread.

    Split main thread from sidechains rather than scanning the file in raw order: a
    subagent's turns are interleaved into the same transcript, so a main-thread text turn
    sitting next to a subagent's text turns would read as one long "talking" streak that
    no single thread ever had.

    A turn counts as no-tool when it made zero CALLS -- not when its tool-name set is
    empty. A `tool_use` block with no `name` therefore breaks a streak instead of
    extending it; it is a call that happened, and the alternative would invent a streak
    out of a missing label.

    Disclosed residual: two DIFFERENT subagents' turns landing adjacently inside the
    sidechain stream can merge into one apparent streak, so the sidechain figure is a
    ceiling. The main-thread figure, which is the one that matters for "a lot of talking
    without doing", has no such join.
    """
    out = {}
    for label, seq in ((MAIN, [t for t in turns if not t["sidechain"]]),
                       ("sidechains", [t for t in turns if t["sidechain"]])):
        runs, cur = [], 0
        for t in seq:
            if turn_calls(t) == 0:
                cur += 1
            else:
                if cur:
                    runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
        out[label] = dict(turns=len(seq), runs=runs)
    return out


def compute_flags(per_source, span_threshold, streak_threshold):
    """Aggregate both signals over one or more sources and decide what is flagged.

    PER SOURCE, then concatenated -- never over a merged turn list. Span indices restart
    at every transcript, so pooling turns from several files first would glue the tail of
    one session's span onto the head of another's and report a span neither one had.

    Each signal ends in exactly one of THREE states, never two:
      measured + a threshold set  -> flagged list (possibly empty == a real clean result)
      measured + no threshold     -> "not_computed" (the distribution is still reported)
      not measurable              -> "unmeasured", and nothing is flagged
    """
    spans, streaks, real_users, excluded, asst = [], {}, 0, {}, 0
    for label, turns, stats in per_source:
        real_users += stats["real_user_turns"]
        asst += len(turns)
        for k, v in stats["user_excluded"].items():
            excluded[k] = excluded.get(k, 0) + v
        for row in spans_of(turns, stats["real_user_turns"]):
            spans.append(dict(source=label, **row))
        for thread, rec in no_tool_streaks(turns).items():
            acc = streaks.setdefault(thread, dict(turns=0, runs=[]))
            acc["turns"] += rec["turns"]
            acc["runs"] += rec["runs"]

    calls = sorted(s["calls"] for s in spans)
    turns_per_span = sorted(s["turns"] for s in spans)
    # Signal 1 needs a boundary to measure between. With no real user turn there is none:
    # the file collapses into ONE span whose count beats any threshold, which is the most
    # convincing wrong answer this instrument could produce.
    s1_state = ("unmeasured" if real_users == 0
                else "not_computed" if span_threshold is None else "measured")
    s1 = dict(state=s1_state,
              real_user_turns=real_users,
              user_entries_excluded=excluded,
              spans=len(spans),
              threshold=span_threshold,
              calls_p50=quantile(calls, 50), calls_p90=quantile(calls, 90),
              calls_max=(calls[-1] if calls else None),
              turns_p50=quantile(turns_per_span, 50),
              turns_max=(turns_per_span[-1] if turns_per_span else None),
              rows=spans,
              # STRICTLY greater. A span of exactly the threshold is not flagged, in both
              # signals, and the near-miss control asserts both sides of that boundary.
              flagged=([s for s in spans if s["calls"] > span_threshold]
                       if s1_state == "measured" else []))
    longest = max([max(v["runs"]) for v in streaks.values() if v["runs"]] or [0])
    s3_state = ("unmeasured" if asst == 0
                else "not_computed" if streak_threshold is None else "measured")
    s3 = dict(state=s3_state, threshold=streak_threshold,
              assistant_turns=asst, longest=longest,
              per_thread={k: dict(turns=v["turns"],
                                  no_tool_turns=sum(v["runs"]),
                                  streaks=len(v["runs"]),
                                  longest=(max(v["runs"]) if v["runs"] else 0),
                                  flagged=([r for r in v["runs"] if r > streak_threshold]
                                           if s3_state == "measured" else []))
                         for k, v in streaks.items()})
    return dict(signal1_span_tool_calls=s1,
                # named in the digest, not left to be inferred from a missing key: an LLM
                # handed two signals out of three will reason about the two it can see
                signal2_repeated_invocations=dict(state="not_implemented",
                                                  note=SIGNAL2_DEFERRED),
                signal3_no_tool_streaks=s3)


# TODO(#4 signal 2, deferred pending #3): repeated / near-duplicate tool invocations --
# runs of the same tool+arguments with no intervening state change. Blocked on nothing
# technical; blocked on MEANING. The only duplicate key available in this file today is
# the tool NAME, and `turn_calls` above already shows why that is not enough: a real
# session calls Bash dozens of times with different commands, so name-matching would flag
# every Bash-heavy session as one long repetition and discriminate nothing -- the
# uniformity trap docs/method.md names. The command-level clustering that makes the key
# meaningful is #3's work (Bash-command clustering / agent cross-tabs); once it lands, the
# key becomes (cluster, argument-string) and this signal can be built on the span rows
# compute_flags already produces. Until then it is reported as NOT IMPLEMENTED, in the
# text report and in the --json digest, rather than shipped as a name-only approximation
# under the same name.
#
# The deferred signal is named in the output rather than left to be inferred from its
# absence. Two signals present and one missing with no explanation reads as two being all
# there is.
SIGNAL2_DEFERRED = (
    "NOT IMPLEMENTED -- repeated/near-duplicate invocations. Published work rates this "
    "the most frequent of 14 multi-agent failure modes (MAST FM-1.3, step repetition, "
    "15.7% over 1600+ annotated traces), so it is the next "
    "signal to add, not one that was judged unimportant. It is deferred rather than "
    "approximated: matching on tool NAME alone would report a Bash-heavy session as one "
    "long repetition, and the command-level clustering that makes it meaningful is being "
    "built separately. Nothing partial is reported under its name."
)


def _wrap(text, indent="    ", width=88):
    """Fold one of the prose explanations to a readable width.

    The explanations are long on purpose -- a flag nobody understands gets read as a
    verdict, which is the one thing this section must not become -- so they are wrapped
    rather than shortened.
    """
    return textwrap.fill(text, width=width,
                         initial_indent=indent, subsequent_indent=indent)


def print_flags(flags):
    s1 = flags["signal1_span_tool_calls"]
    s3 = flags["signal3_no_tool_streaks"]
    print("\nLOOP FLAGS")
    print(_wrap("Mechanical signals, computed from transcript STRUCTURE only -- turn "
                "boundaries and tool_use block counts -- never from judging what was "
                "said. THESE ARE FLAGS, NOT VERDICTS: a flagged span means an unusual "
                "amount of work by the threshold YOU set, which is a pointer to look. It "
                "is not a finding that the work was wasted, and no threshold here is a "
                "validated constant.", indent="  "))

    print(f"\n  real user turns (span boundaries): {s1['real_user_turns']}")
    if s1["user_entries_excluded"]:
        print("  user entries NOT counted as operator input, by reason:")
        for key, why in USER_EXCLUSIONS:
            n = s1["user_entries_excluded"].get(key, 0)
            if n:
                print(f"    {key}: {n}")
                print(_wrap(why, indent="      "))
    else:
        print("  user entries NOT counted as operator input: none")
        print(_wrap("No isMeta, tool_result-only or engine-injected user entry was seen "
                    "at all. For a real Claude Code transcript that is itself unusual -- "
                    "tool results come back as user entries -- so check that this is the "
                    "format you think it is.", indent="    "))

    print("\n  SIGNAL 1 -- tool calls per span between consecutive real user turns")
    if s1["state"] == "unmeasured":
        print(_wrap("UNMEASURED: not one real user turn was found, so this transcript has "
                    "no span boundaries at all. The whole file would read as ONE span and "
                    "would exceed any threshold, so NOTHING is flagged here: a dramatic "
                    "number about a session whose turns cannot be seen is worse than no "
                    "number. See --help-blind, blind mode 5."))
    else:
        print(f"    spans: {s1['spans']}  tool calls per span: "
              f"p50={s1['calls_p50']} p90={s1['calls_p90']} max={s1['calls_max']}"
              f"  (assistant turns per span: p50={s1['turns_p50']} "
              f"max={s1['turns_max']})")
        if s1["state"] == "not_computed":
            print("    threshold: NOT SET -> NOT COMPUTED")
            print(_wrap("Not a clean result and not a dirty one: no span was tested. No "
                        "default ships because no published threshold is grounded -- the "
                        "frameworks' own ceilings span 10 to 1000 (each read at first "
                        "hand; see docs/citations.md) and exist as cost backstops, not as "
                        "non-convergence detectors. Pick one from the distribution above "
                        "via --flag-span-tool-calls N or ARI_FLAG_SPAN_TOOL_CALLS, and "
                        "treat it as a starting point for recalibration.",
                        indent="      "))
        else:
            print(f"    threshold: > {s1['threshold']} tool calls -- caller-set, strictly "
                  f"greater, so a span of exactly {s1['threshold']} is NOT flagged")
            if s1["flagged"]:
                print(f"    FLAGGED spans: {len(s1['flagged'])} of {s1['spans']}")
                for s in s1["flagged"]:
                    where = ("preamble, before any user turn" if s["preamble"]
                             else f"span {s['index']}")
                    print(f"      {where}: calls={s['calls']} turns={s['turns']}")
            else:
                print(f"    FLAGGED spans: 0 of {s1['spans']} -- a MEASURED clean result "
                      f"against this threshold")

    print("\n  SIGNAL 2 -- repeated / near-duplicate invocations: NOT IMPLEMENTED")
    print(_wrap(SIGNAL2_DEFERRED, indent="      "))

    print("\n  SIGNAL 3 -- consecutive assistant turns that called no tool")
    if s3["state"] == "unmeasured":
        print("    UNMEASURED: no assistant turn was found at all.")
    else:
        print(f"    longest streak anywhere: {s3['longest']} turn(s)")
        for thread, rec in sorted(s3["per_thread"].items()):
            print(f"    {thread}: {rec['turns']} turns, {rec['no_tool_turns']} with no "
                  f"tool call, in {rec['streaks']} streak(s), longest={rec['longest']}")
        if s3["state"] == "not_computed":
            print("    threshold: NOT SET -> NOT COMPUTED")
            print(_wrap("No streak was tested. Same reason as signal 1 for shipping no "
                        "default; set one with --flag-no-tool-streak N or "
                        "ARI_FLAG_NO_TOOL_STREAK.", indent="      "))
        else:
            print(f"    threshold: > {s3['threshold']} consecutive turns -- caller-set, "
                  f"strictly greater")
            hits = [(th, r) for th, rec in sorted(s3["per_thread"].items())
                    for r in rec["flagged"]]
            if hits:
                print(f"    FLAGGED streaks: {len(hits)}")
                for th, r in hits:
                    print(f"      {th}: {r} consecutive turns with no tool call")
            else:
                print("    FLAGGED streaks: 0 -- a MEASURED clean result against this "
                      "threshold")


def print_user_entry_survey(sources_read, sources_na, per_source, args):
    """The census signal 1's span boundaries rest on, run on whatever corpus you have.

    Exists because the exclusion rule is the one place this feature can be quietly,
    catastrophically wrong in either direction, and no one should have to take the
    docstring's word for the shape of their own transcripts. Counts only -- no entry text
    is read, printed, or inspected beyond its block types and two exact fields.
    """
    print("USER-ENTRY CENSUS -- which `user` entries count as operator input, and why not "
          "if not.\nThis is what signal 1's span boundaries are built from; the exclusion "
          "rules are listed\nunder --help-blind. Counts only: no entry's text is read.")
    print("\nsources read:")
    for label in sources_read:
        print(f"  {anonymise(label) if args.anonymise else label}")
    if sources_na:
        print("sources UNREADABLE (reported, not counted as zero):")
        for label in sources_na:
            print(f"  n/a  {anonymise(label) if args.anonymise else label}")

    tot_entries = tot_real = 0
    excluded = {}
    for _, st in per_source:
        tot_entries += st["user_entries"]
        tot_real += st["real_user_turns"]
        for k, v in st["user_excluded"].items():
            excluded[k] = excluded.get(k, 0) + v
    print(f"\nuser entries: {tot_entries}")
    print(f"  counted as REAL user turns (span boundaries): {tot_real}")
    for key, why in USER_EXCLUSIONS:
        print(f"  excluded as {key}: {excluded.get(key, 0)}")
        print(_wrap(why, indent="    "))
    other = tot_entries - tot_real - sum(excluded.values())
    # Must be 0 by construction. Printed anyway: a classifier whose own arithmetic is not
    # checked is a classifier whose buckets can silently stop summing to its input.
    print(f"  unaccounted for (must be 0): {other}")

    print("\nper source (real user turns / user entries):")
    for label, st in per_source:
        print(f"  {st['real_user_turns']:>5} / {st['user_entries']:<6} "
              f"{anonymise(label) if args.anonymise else label}")

    if tot_real == 0:
        print("\nNOT ONE REAL USER TURN in this corpus. Signal 1 is UNMEASURABLE here -- "
              "see\n--help-blind, blind mode 5. This is the case where the whole "
              "transcript would\ncollapse into one span and exceed any threshold, so "
              "nothing is flagged instead.")

# ---------------------------------------------------------------------------
# threshold resolution: CLI flag, else env var, else NOT SET
# ---------------------------------------------------------------------------
def resolve_threshold(cli_value, env_name):
    """Return (threshold_or_None, error_or_None).

    An env var that is set but unparseable is an ERROR, not a fallback to "no threshold":
    the environment you believe you set is not the environment you got until something
    says so, and silently ignoring `ARI_FLAG_SPAN_TOOL_CALLS=twenty` would print NOT
    COMPUTED to someone who thinks they asked for a flag.

    A threshold below 1 is an ERROR too, from either source. 0 flags every span including
    the empty ones; a negative flags all of them unconditionally. A detector that fires on
    everything discriminates nothing, so it is refused rather than honoured.
    """
    src, raw = ("flag", cli_value)
    if cli_value is None:
        raw = os.environ.get(env_name)
        src = "env"
        if raw is None or raw == "":
            return None, None
        try:
            raw = int(str(raw).strip())
        except ValueError:
            return None, (f"{env_name}={os.environ.get(env_name)!r} is not an integer. An "
                          f"env var that cannot be parsed is refused, not quietly ignored "
                          f"back to 'no threshold'")
    if raw < 1:
        where = (f"{env_name}={raw}" if src == "env" else f"threshold {raw}")
        return None, (f"{where} is below 1. A threshold of 0 flags EVERY span and EVERY "
                      f"streak, including the empty ones -- a user turn answered with no "
                      f"tool call is a span of 0 -- and a negative threshold flags all of "
                      f"them unconditionally. A detector that fires on everything "
                      f"discriminates nothing")
    return raw, None


# ---------------------------------------------------------------------------
# subsession manifest: a caller-supplied tree of SEPARATE transcripts
# ---------------------------------------------------------------------------
def load_manifest(path):
    """Parse the manifest JSON: a list of {label, transcript_path, parent_label}.

    Returns (nodes, error). `error` is a human string when the file is unusable, in which
    case the caller refuses -- a malformed manifest is "cannot measure", not "measured
    nothing".
    """
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
    except OSError as e:
        return None, f"cannot read manifest {path!r}: {e}"
    except Exception as e:
        return None, f"manifest {path!r} is not valid JSON: {e}"
    return validate_manifest(data)


def validate_manifest(data):
    """Validate parsed manifest JSON (split out so the self-test exercises it without
    writing a file -- this instrument stays read-only)."""
    if not isinstance(data, list):
        return None, "manifest must be a JSON list of {label, transcript_path, parent_label}"
    nodes, seen = [], set()
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "label" not in item \
                or "transcript_path" not in item:
            return None, (f"manifest entry {i} must have at least 'label' and "
                          f"'transcript_path'")
        label = str(item["label"])
        # Duplicate labels are REFUSED, not merged: the rollup keys own-totals and the
        # parent/child map by label, so two nodes sharing one would silently collapse into
        # one -- a wrong number that still looks like a measurement.
        if label in seen:
            return None, (f"duplicate manifest label {label!r}: labels must be unique or "
                          f"the rollup silently merges those nodes into one")
        seen.add(label)
        parent = item.get("parent_label")
        nodes.append(dict(label=label,
                          path=str(item["transcript_path"]),
                          parent=(str(parent) if parent is not None else None)))
    # A parent_label naming no node would silently reparent to root; refuse instead.
    labels = {nd["label"] for nd in nodes}
    for nd in nodes:
        if nd["parent"] is not None and nd["parent"] not in labels:
            return None, (f"manifest node {nd['label']!r} names parent_label "
                          f"{nd['parent']!r}, which is not itself a node")
    return nodes, None


def rollup(nodes, own_totals):
    """Sum each node's own totals bottom-up to its parent.

    own_totals[label] is a totals dict, or None when that node's transcript was
    unreadable (reported n/a, contributing nothing -- never 0). Returns a list of
    (node, own, subtree, na) in input order; cycles are broken and reported via `na`.
    """
    children = {}
    for nd in nodes:
        children.setdefault(nd["parent"], []).append(nd["label"])
    by_label = {nd["label"]: nd for nd in nodes}

    def subtree(label, seen):
        if label in seen:                       # cycle guard
            return {k: 0 for k in USAGE_FIELDS}
        seen = seen | {label}
        acc = {k: 0 for k in USAGE_FIELDS}
        own = own_totals.get(label)
        if own is not None:
            for k in USAGE_FIELDS:
                acc[k] += own[k]
        for ch in children.get(label, []):
            sub = subtree(ch, seen)
            for k in USAGE_FIELDS:
                acc[k] += sub[k]
        return acc

    rows = []
    for nd in nodes:
        own = own_totals.get(nd["label"])
        rows.append((nd, own, subtree(nd["label"], set()), own is None))
    return rows


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def fmt(acc):
    return (f"in={acc['input_tokens']} out={acc['output_tokens']} "
            f"cache_create={acc['cache_creation_input_tokens']} "
            f"cache_read={acc['cache_read_input_tokens']}")


def print_report(sources_read, sources_na, turns, args, flags=None):
    tot = totals_of(turns)
    cov = coverage(turns)

    print("sources read:")
    for label in sources_read:
        print(f"  {anonymise(label) if args.anonymise else label}")
    if sources_na:
        print("sources UNREADABLE (reported, not counted as zero):")
        for label in sources_na:
            print(f"  n/a  {anonymise(label) if args.anonymise else label}")

    print(f"\nattribution coverage: {cov['usage_bearing']} of {cov['total_turns']} "
          f"assistant turns were usage-bearing; "
          f"{cov['without_usage']} carried no recognised usage field")
    if cov["without_usage"]:
        print(f"  WARNING: {cov['without_usage']} of {cov['total_turns']} turns had no "
              f"usage field and are ABSENT from every total below -- not counted as 0.")
    print("usage-field presence across usage-bearing turns (a field missing here is a "
          "silent 0, not a measured one):")
    for k in USAGE_FIELDS:
        flag = "" if cov["field_present"][k] == cov["usage_bearing"] else "  <-- INCOMPLETE"
        print(f"  {k}: {cov['field_present'][k]}/{cov['usage_bearing']}{flag}")
    if cov["fields_partially_absent"]:
        print(f"  WARNING: field(s) [{', '.join(cov['fields_partially_absent'])}] are "
              f"missing from some turns; their totals below are FLOORS, not measured 0s.")
    print(f"sidechain (subagent) turns: {cov['sidechain_turns']}"
          + (f", of which {cov['sidechain_without_type']} with no recorded subagent type"
             if cov["sidechain_without_type"] else ""))

    print(f"\nTOTAL over {tot['turns']} usage-bearing turns: {fmt(tot)}")
    r = ratio(tot)
    print(f"cache_read : output overhead = "
          + (f"{r:.2f}" if r is not None else "n/a (no output tokens)"))

    print("\nBY TOOL (turn-grain: a turn's usage is attributed to the SET of tools it "
          "called, not split per call):")
    bytool = group(turns, tool_key)
    print(f"  {'turns':>5} {'out':>8} {'cache_read':>11} {'cache_create':>12}  tools")
    for key, acc in sorted(bytool.items(), key=lambda kv: -kv[1]["cache_read_input_tokens"]):
        print(f"  {acc['turns']:>5} {acc['output_tokens']:>8} "
              f"{acc['cache_read_input_tokens']:>11} "
              f"{acc['cache_creation_input_tokens']:>12}  {key}")

    print("\nBY AGENT (same-session Task subagents separated from the main thread):")
    byagent = group(turns, agent_key)
    print(f"  {'turns':>5} {'out':>8} {'cache_read':>11} {'cache_create':>12}  agent")
    for key, acc in sorted(byagent.items(), key=lambda kv: -kv[1]["cache_read_input_tokens"]):
        print(f"  {acc['turns']:>5} {acc['output_tokens']:>8} "
              f"{acc['cache_read_input_tokens']:>11} "
              f"{acc['cache_creation_input_tokens']:>12}  {key}")

    print(f"\nTOP {args.top} TURNS BY cache_read (the turns re-reading the most cached "
          f"context):")
    ranked = sorted((t for t in turns if t["has_usage"]),
                    key=lambda t: -num(t["usage"].get("cache_read_input_tokens")))[:args.top]
    print(f"  {'cache_read':>11} {'out':>8}  agent / tools")
    for t in ranked:
        print(f"  {num(t['usage'].get('cache_read_input_tokens')):>11} "
              f"{num(t['usage'].get('output_tokens')):>8}  "
              f"{agent_key(t)} / {tool_key(t)}")

    if flags is not None:
        print_flags(flags)


def print_rollup(rows, args):
    print("\nBY SUBSESSION (caller's manifest tree, summed bottom-up; own = this node's "
          "transcript, subtree = own + all descendants):")
    na = sum(1 for *_, is_na in rows if is_na)
    for nd, own, sub, is_na in rows:
        label = anonymise(nd["label"]) if args.anonymise else nd["label"]
        parent = nd["parent"]
        if parent and args.anonymise:
            parent = anonymise(parent)
        ptxt = f" (parent {parent})" if parent else " (root)"
        if is_na:
            print(f"  {label}{ptxt}: own=n/a (transcript unreadable), "
                  f"subtree={fmt(sub)}")
        else:
            print(f"  {label}{ptxt}: own[{fmt(own)}]  subtree[{fmt(sub)}]")
    if na:
        print(f"  {na} node(s) reported n/a -- their transcripts were unreadable and "
              f"contribute nothing to any parent sum (not 0).")


def _anonymise_sources(obj):
    """Hash every `source` field anywhere under the flags digest, recursively.

    Written as a walk rather than as a loop over the one list it is obvious to loop over,
    because that version shipped and leaked: the span rows and the FLAGGED rows are
    SEPARATE copies of the same records after the digest's round-trip through JSON, so
    anonymising `rows` left every flagged row carrying the raw transcript path -- in the
    one output mode whose entire purpose is that the path is not there. Found by grepping
    an `--anonymise` run for the string it had just been told to hide, which is now a
    self-test leg. A walk cannot be defeated by someone adding a third list.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "source" and isinstance(v, str):
                obj[k] = anonymise(v)
            else:
                _anonymise_sources(v)
    elif isinstance(obj, list):
        for v in obj:
            _anonymise_sources(v)


def build_digest(sources_read, sources_na, turns, rollup_rows, args,
                 flags=None):
    tot = totals_of(turns)
    cov = coverage(turns)

    def anon_list(xs):
        return [anonymise(x) if args.anonymise else x for x in xs]

    def grp(gfn):
        return [dict(key=k, **{f: acc[f] for f in USAGE_FIELDS}, turns=acc["turns"])
                for k, acc in sorted(group(turns, gfn).items(),
                                     key=lambda kv: -kv[1]["cache_read_input_tokens"])]

    ranked = sorted((t for t in turns if t["has_usage"]),
                    key=lambda t: -num(t["usage"].get("cache_read_input_tokens")))[:args.top]
    digest = dict(
        measured=bool(tot["turns"]),
        sources_read=anon_list(sources_read),
        sources_unreadable=anon_list(sources_na),
        coverage=cov,
        totals={**{f: tot[f] for f in USAGE_FIELDS}, "turns": tot["turns"]},
        cache_read_to_output=ratio(tot),
        by_tool=grp(tool_key),
        by_agent=grp(agent_key),
        top_by_cache_read=[dict(cache_read=num(t["usage"].get("cache_read_input_tokens")),
                                output=num(t["usage"].get("output_tokens")),
                                agent=agent_key(t), tools=sorted(t["tools"]))
                           for t in ranked],
    )
    if flags is not None:
        # The span rows name their source, so they are anonymised with the same rule the
        # source list follows -- a span row is as identifying as the path it came from.
        f = json.loads(json.dumps(flags))
        if args.anonymise:
            _anonymise_sources(f)
        digest["loop_flags"] = f
        digest["loop_flags_are_not_verdicts"] = (
            "A flagged span or streak is a pointer to look, computed from transcript "
            "structure against a caller-set threshold. It is not a finding that the work "
            "was wasted, and no threshold here is a validated constant.")
    if rollup_rows is not None:
        digest["subsessions"] = [
            dict(label=(anonymise(nd["label"]) if args.anonymise else nd["label"]),
                 parent=((anonymise(nd["parent"]) if (nd["parent"] and args.anonymise)
                          else nd["parent"])),
                 own=(None if is_na else {f: own[f] for f in USAGE_FIELDS}),
                 subtree={f: sub[f] for f in USAGE_FIELDS})
            for nd, own, sub, is_na in rollup_rows]
    return digest


# ---------------------------------------------------------------------------
# self-test: positive control (exact) + negative control (must be able to fail)
# ---------------------------------------------------------------------------
def _fixture_lines():
    """A synthetic transcript with hand-computed totals. Returned as JSONL strings so the
    self-test runs the SAME parser the file path would, with no file written.

    Deliberately includes: a turn split across thinking + two tool_use entries that all
    repeat one usage (the dedup trap); a multi-tool turn; a no-tool turn; and a two-turn
    sidechain carrying a subagent type.
    """
    def asst(mid, blocks, usage, side=False, agent=None):
        d = dict(type="assistant", isSidechain=side,
                 message=dict(id=mid, role="assistant", content=blocks, usage=usage))
        if agent:
            d["attributionAgent"] = agent
        return json.dumps(d)

    def u(i, o, cc, cr):
        return {"input_tokens": i, "output_tokens": o,
                "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}

    tu = lambda name: {"type": "tool_use", "name": name, "input": {}}
    th = {"type": "thinking", "thinking": "..."}
    tx = {"type": "text", "text": "ok"}

    return [
        json.dumps(dict(type="user", message=dict(role="user", content="hi"))),
        # M1: ONE turn, THREE entries, same message.id + same usage -> must count once
        asst("msg-main-0001", [th], u(10, 100, 1000, 5000)),
        asst("msg-main-0001", [tu("Read")], u(10, 100, 1000, 5000)),
        asst("msg-main-0001", [tu("Read")], u(10, 100, 1000, 5000)),
        # M2: multi-tool turn
        asst("msg-main-0002", [tx, tu("Bash"), tu("Grep")], u(20, 200, 2000, 8000)),
        # M3: no tool call
        asst("msg-main-0003", [tx], u(5, 50, 500, 1000)),
        # M4: launches a subagent
        asst("msg-main-0004", [tu("Task")], u(8, 80, 800, 2000)),
        json.dumps(dict(type="queue-operation", operation="noop")),
        # sidechain: two turns, subagent type recorded
        asst("msg-side-0001", [tu("Grep")], u(4, 40, 400, 3000),
             side=True, agent="explore"),
        asst("msg-side-0002", [tx], u(2, 20, 200, 1500), side=True, agent="explore"),
    ]


# hand-computed from _fixture_lines (usage counted ONCE per message.id)
_EXPECT = dict(input_tokens=49, output_tokens=490,
               cache_creation_input_tokens=4900, cache_read_input_tokens=20500, turns=6)


def _flag_fixture_lines(span_calls, streak_len, tool_results_as_user_text=False):
    """A synthetic transcript built to a SPECIFIED span size and streak length.

    One builder for every flag control, so the positive, the negative and the near-miss
    differ only in the two numbers under test -- a control that has its own bespoke
    fixture can pass for reasons that have nothing to do with the number it claims to
    test.

    Shape, with span_calls=N and streak_len=S:
      real user turn      -> opens span 1
      N assistant turns, one tool call each, EACH followed by the engine's `tool_result`
        user entry, exactly as a real transcript writes it
      an `isMeta` relaunch stub ("Continue from where you left off.")
      S assistant turns with no tool call at all
      real user turn      -> opens span 2
      1 assistant turn with one tool call
    So: 2 spans, span 1 = N calls, span 2 = 1 call, longest main-thread no-tool streak = S.

    `tool_results_as_user_text` rewrites the engine's tool_result entries as plain text
    user entries -- the counterfactual that shows the exclusion rule is load-bearing: read
    them as operator input and span 1 shatters into N spans of one call each, so the
    positive control goes clean. A control that cannot be made to fail is decoration.
    """
    out = []
    n = [0]

    def usage():
        return {"input_tokens": 1, "output_tokens": 10,
                "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}

    def asst(blocks):
        n[0] += 1
        return json.dumps(dict(type="assistant", isSidechain=False,
                               message=dict(id=f"msg-flag-{n[0]:04d}", role="assistant",
                                            content=blocks, usage=usage())))

    def real_user(text):
        return json.dumps(dict(type="user", userType="external",
                               message=dict(role="user", content=text)))

    def tool_result(i):
        if tool_results_as_user_text:
            return json.dumps(dict(type="user", userType="external",
                                   message=dict(role="user", content=f"result {i}")))
        return json.dumps(dict(type="user", userType="external",
                               message=dict(role="user",
                                            content=[dict(type="tool_result",
                                                          tool_use_id=f"call-{i}",
                                                          content="ok")])))

    def call(i):
        return dict(type="tool_use", id=f"call-{i}", name="Bash", input={})

    text = dict(type="text", text="thinking out loud")

    out.append(real_user("do the thing"))
    for i in range(span_calls):
        out.append(asst([text, call(i)]))
        out.append(tool_result(i))
    out.append(json.dumps(dict(type="user", userType="external", isMeta=True,
                               message=dict(role="user",
                                            content=[dict(type="text",
                                                          text="Continue from where you "
                                                               "left off.")]))))
    for _ in range(streak_len):
        out.append(asst([text]))
    out.append(real_user("now do the other thing"))
    out.append(asst([text, call(9000)]))
    out.append(tool_result(9000))
    return out


def _flags_of(lines, span_threshold=None, streak_threshold=None):
    """Run the real collector and the real flag computation over fixture lines."""
    turns, stats = collect_turns(lines)
    return compute_flags([("fixture", turns, stats)], span_threshold, streak_threshold)

def _corrupt(lines):
    """The negative control: rename every usage field so no turn is usage-bearing.

    A correct instrument must flag this (coverage: 0 of M usage-bearing) and refuse. An
    instrument that treats a missing field as 0 would print total=0 and pass silently --
    which is the failure this control is built to catch.
    """
    out = []
    for l in lines:
        for f in USAGE_FIELDS:
            l = l.replace(f'"{f}"', f'"{f}_RENAMED"')
        out.append(l)
    return out


def _corrupt_one(lines, field):
    """Rename ONE usage field. The turns stay usage-bearing on the other three fields, so
    the all-or-nothing check misses it -- this is the case finding (a) was defeated by."""
    return [l.replace(f'"{field}"', f'"{field}_RENAMED"') for l in lines]


def self_test():
    print("SELF-TEST [positive control: exact totals over a synthetic fixture]")
    turns, _ = collect_turns(_fixture_lines())
    tot = totals_of(turns)
    ok = True

    # the dedup is the load-bearing claim: M1's 3 entries must be one turn
    for f in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens", "turns"):
        got, exp = tot.get(f, tot.get("turns")), _EXPECT[f]
        got = tot[f] if f in tot else tot["turns"]
        mark = "OK" if got == exp else "MISMATCH"
        if got != exp:
            ok = False
        print(f"  {f:<32} got={got:<7} expected={exp:<7} {mark}")

    cov = coverage(turns)
    side = group(turns, agent_key)
    # main vs sidechain must be split, not merged
    main_out = side.get(MAIN, {}).get("output_tokens")
    exp_main = 430
    if main_out != exp_main:
        ok = False
    print(f"  {'main-thread output':<32} got={main_out} expected={exp_main} "
          f"{'OK' if main_out == exp_main else 'MISMATCH'}")
    sub_out = side.get("explore", {}).get("output_tokens")
    if sub_out != 60:
        ok = False
    print(f"  {'subagent explore output':<32} got={sub_out} expected=60 "
          f"{'OK' if sub_out == 60 else 'MISMATCH'}")
    bytool = group(turns, tool_key)
    # the no-tool bucket must merge M3 and the sidechain's text turn (2 turns)
    nt = bytool.get(NO_TOOL, {}).get("turns")
    if nt != 2:
        ok = False
    print(f"  {'(no tool call) turns':<32} got={nt} expected=2 "
          f"{'OK' if nt == 2 else 'MISMATCH'}")
    full_ok = (cov["fully_attributed"] == 6
               and all(cov["field_present"][k] == 6 for k in USAGE_FIELDS))
    if not full_ok:
        ok = False
    print(f"  coverage: {cov['usage_bearing']} of {cov['total_turns']} usage-bearing, "
          f"fully-attributed={cov['fully_attributed']} "
          f"{'OK' if full_ok else 'MISMATCH'}")

    print("\nSELF-TEST [negative control A: rename ALL usage fields -> no turn measurable]")
    ca = coverage(collect_turns(_corrupt(_fixture_lines()))[0])
    a_ok = ca["usage_bearing"] == 0 and refusal_reason(ca) is not None
    print(f"  usage-bearing now {ca['usage_bearing']} of {ca['total_turns']}; "
          f"refusal={'FIRED' if a_ok else 'DID NOT FIRE'}")
    if not a_ok:
        ok = False

    print("\nSELF-TEST [negative control B: rename ONE field (output_tokens) -- the case "
          "the all-or-nothing check passed silently]")
    cb = coverage(collect_turns(_corrupt_one(_fixture_lines(), "output_tokens"))[0])
    # still usage-bearing on the other three fields, but output_tokens absent everywhere,
    # and the run must refuse rather than report output=0
    b_ok = (cb["usage_bearing"] == 6 and cb["field_present"]["output_tokens"] == 0
            and "output_tokens" in cb["fields_entirely_absent"]
            and refusal_reason(cb) is not None)
    print(f"  usage-bearing still {cb['usage_bearing']}, but output_tokens present on "
          f"{cb['field_present']['output_tokens']}/{cb['usage_bearing']}; "
          f"refusal={'FIRED' if b_ok else 'DID NOT FIRE'}")
    if not b_ok:
        ok = False

    print("\nSELF-TEST [manifest guards: duplicate label and dangling parent must REFUSE]")
    dup_ok = validate_manifest(
        [{"label": "x", "transcript_path": "a"},
         {"label": "x", "transcript_path": "b"}])[1] is not None
    unknown_ok = validate_manifest(
        [{"label": "x", "transcript_path": "a", "parent_label": "ghost"}])[1] is not None
    print(f"  duplicate-label refused={dup_ok}; dangling-parent refused={unknown_ok}")
    if not (dup_ok and unknown_ok):
        ok = False

    # -----------------------------------------------------------------------
    # LOOP FLAGS: positive, negative, near-miss, and the exclusion counterfactual
    # -----------------------------------------------------------------------
    SPAN_T, STREAK_T = 5, 3
    print(f"\nSELF-TEST [loop flags, POSITIVE control: a fixture built to exceed both "
          f"thresholds (span > {SPAN_T} calls, streak > {STREAK_T} turns)]")
    pos = _flags_of(_flag_fixture_lines(7, 4), SPAN_T, STREAK_T)
    p1, p3 = pos["signal1_span_tool_calls"], pos["signal3_no_tool_streaks"]
    # hand-computed from the builder: 2 real user turns, 2 spans of 7 and 1 calls, one
    # main-thread streak of 4, and 8 tool_result + 1 isMeta entries excluded
    checks = [
        ("real user turns", p1["real_user_turns"], 2),
        ("spans", p1["spans"], 2),
        ("span 1 calls", p1["rows"][0]["calls"], 7),
        ("span 2 calls", p1["rows"][1]["calls"], 1),
        ("calls p50 / p90", (p1["calls_p50"], p1["calls_p90"]), (1, 7)),
        ("excluded tool_result", p1["user_entries_excluded"].get("tool_result"), 8),
        ("excluded isMeta", p1["user_entries_excluded"].get("isMeta"), 1),
        ("signal 1 state", p1["state"], "measured"),
        ("spans FLAGGED", [r["calls"] for r in p1["flagged"]], [7]),
        ("signal 3 longest streak", p3["longest"], 4),
        ("streaks FLAGGED (main)", p3["per_thread"][MAIN]["flagged"], [4]),
        ("signal 2 state", pos["signal2_repeated_invocations"]["state"],
         "not_implemented"),
    ]
    for label, got, exp in checks:
        if got != exp:
            ok = False
        print(f"  {label:<32} got={str(got):<18} expected={str(exp):<18} "
              f"{'OK' if got == exp else 'MISMATCH'}")

    print(f"\nSELF-TEST [loop flags, NEGATIVE control: a fixture under BOTH thresholds "
          f"must report a MEASURED clean result, not 'not computed']")
    neg = _flags_of(_flag_fixture_lines(3, 1), SPAN_T, STREAK_T)
    n1, n3 = neg["signal1_span_tool_calls"], neg["signal3_no_tool_streaks"]
    neg_ok = (n1["state"] == "measured" and n1["flagged"] == []
              and n1["calls_max"] == 3 and n3["state"] == "measured"
              and n3["longest"] == 1 and n3["per_thread"][MAIN]["flagged"] == [])
    print(f"  max span={n1['calls_max']} (threshold {SPAN_T}), longest streak="
          f"{n3['longest']} (threshold {STREAK_T}); flagged spans="
          f"{len(n1['flagged'])} streaks={len(n3['per_thread'][MAIN]['flagged'])}; "
          f"both states measured={n1['state'] == n3['state'] == 'measured'} "
          f"{'OK' if neg_ok else 'MISMATCH'}")
    if not neg_ok:
        ok = False

    print("\nSELF-TEST [loop flags, NEAR-MISS boundary: EXACTLY the threshold must not "
          "flag, and exactly one more must -- both signals, both directions]")
    for name, exact, over in (("span calls", _flag_fixture_lines(SPAN_T, 0),
                               _flag_fixture_lines(SPAN_T + 1, 0)),
                              ("no-tool streak", _flag_fixture_lines(1, STREAK_T),
                               _flag_fixture_lines(1, STREAK_T + 1))):
        fe = _flags_of(exact, SPAN_T, STREAK_T)
        fo = _flags_of(over, SPAN_T, STREAK_T)
        if name == "span calls":
            at, above = len(fe["signal1_span_tool_calls"]["flagged"]), \
                len(fo["signal1_span_tool_calls"]["flagged"])
            n_at = fe["signal1_span_tool_calls"]["calls_max"]
            n_over = fo["signal1_span_tool_calls"]["calls_max"]
        else:
            at = len(fe["signal3_no_tool_streaks"]["per_thread"][MAIN]["flagged"])
            above = len(fo["signal3_no_tool_streaks"]["per_thread"][MAIN]["flagged"])
            n_at = fe["signal3_no_tool_streaks"]["longest"]
            n_over = fo["signal3_no_tool_streaks"]["longest"]
        leg_ok = (at == 0 and above == 1)
        if not leg_ok:
            ok = False
        print(f"  {name:<16} at={n_at} -> flagged {at} (must be 0);  "
              f"at={n_over} -> flagged {above} (must be 1)  "
              f"{'OK' if leg_ok else 'MISMATCH'}")

    print("\nSELF-TEST [loop flags, the exclusion rule is LOAD-BEARING: read the engine's "
          "own tool_result entries as operator input and the positive control goes clean]")
    cf = _flags_of(_flag_fixture_lines(7, 4, tool_results_as_user_text=True),
                   SPAN_T, STREAK_T)["signal1_span_tool_calls"]
    # Not a style point: with tool results counted as user turns, span 1 shatters into one
    # span per call, the max drops to 1, and the fixture built to exceed the threshold is
    # reported as clean. This is the control demonstrating it can fail.
    cf_ok = (cf["spans"] == 10 and cf["calls_max"] == 1 and cf["flagged"] == []
             and p1["spans"] == 2 and p1["calls_max"] == 7)
    print(f"  with the exclusion: spans={p1['spans']} max={p1['calls_max']} "
          f"flagged={len(p1['flagged'])}  |  without it: spans={cf['spans']} "
          f"max={cf['calls_max']} flagged={len(cf['flagged'])}  "
          f"{'OK -- the control can fail' if cf_ok else 'MISMATCH'}")
    if not cf_ok:
        ok = False

    print("\nSELF-TEST [loop flags: a transcript with NO real user turn must read "
          "UNMEASURED, and must flag NOTHING]")
    # every real user turn removed: only the engine's tool_result entries and the isMeta
    # relaunch stub remain, which is the sidechain-only / mid-session-fragment case
    headless = [l for l in _flag_fixture_lines(7, 4)
                if not (json.loads(l).get("type") == "user"
                        and user_exclusion_reason(json.loads(l)) is None)]
    hl = _flags_of(headless, SPAN_T, STREAK_T)["signal1_span_tool_calls"]
    hl_ok = (hl["real_user_turns"] == 0 and hl["state"] == "unmeasured"
             and hl["flagged"] == [])
    print(f"  real user turns={hl['real_user_turns']}, state={hl['state']}, "
          f"flagged={len(hl['flagged'])} (the single implicit span holds "
          f"{hl['rows'][0]['calls']} calls, which would have exceeded the threshold)  "
          f"{'OK' if hl_ok else 'MISMATCH'}")
    if not hl_ok:
        ok = False

    print("\nSELF-TEST [loop flags: a turn that calls ONE tool SEVERAL times is that many "
          "calls, and a split turn's repeated block ids are counted once]")
    # Found by mutating turn_calls to `len(t["tools"])`: the span fixture's turns each
    # call one tool once, so name-counting and call-counting agreed everywhere and the
    # whole point of counting calls went untested. These two turns disagree under that
    # mutation by construction.
    def _tu(i):
        return dict(type="tool_use", id=f"b-{i}", name="Bash", input={})
    multi = [json.dumps(dict(type="user", message=dict(role="user", content="go"))),
             # one turn, one tool name, THREE calls
             json.dumps(dict(type="assistant", message=dict(
                 id="msg-multi-1", role="assistant",
                 content=[_tu(1), _tu(2), _tu(3)],
                 usage=dict(input_tokens=1, output_tokens=1,
                            cache_creation_input_tokens=1,
                            cache_read_input_tokens=1)))),
             # ONE turn written as two entries that REPEAT the same block id: the split-
             # turn trap applied to call counting rather than to usage
             json.dumps(dict(type="assistant", message=dict(
                 id="msg-multi-2", role="assistant", content=[_tu(9)],
                 usage=dict(input_tokens=1, output_tokens=1,
                            cache_creation_input_tokens=1,
                            cache_read_input_tokens=1)))),
             json.dumps(dict(type="assistant", message=dict(
                 id="msg-multi-2", role="assistant", content=[_tu(9)],
                 usage=dict(input_tokens=1, output_tokens=1,
                            cache_creation_input_tokens=1,
                            cache_read_input_tokens=1))))]
    mt, _ = collect_turns(multi)
    mt_ok = (len(mt) == 2 and turn_calls(mt[0]) == 3 and mt[0]["tools"] == {"Bash"}
             and turn_calls(mt[1]) == 1)
    print(f"  turn 1: tool names={sorted(mt[0]['tools'])} calls={turn_calls(mt[0])} "
          f"(expected 3 -- name-counting would say 1); turn 2: one block id across two "
          f"entries -> calls={turn_calls(mt[1])} (expected 1)  "
          f"{'OK' if mt_ok else 'MISMATCH'}")
    if not mt_ok:
        ok = False

    print("\nSELF-TEST [loop flags: a no-tool streak must NOT run across the main thread "
          "and a sidechain, which are interleaved in one file]")
    # Found by mutating no_tool_streaks to scan raw transcript order: the span fixture has
    # no sidechain at all, so the split was unexercised. Here four no-tool turns alternate
    # main/sidechain/sidechain/main -- a raw-order scan reports ONE streak of 4, which no
    # thread ever had.
    def _txt(mid, side):
        return json.dumps(dict(type="assistant", isSidechain=side, message=dict(
            id=mid, role="assistant", content=[dict(type="text", text="talking")],
            usage=dict(input_tokens=1, output_tokens=1,
                       cache_creation_input_tokens=1, cache_read_input_tokens=1))))
    woven = [json.dumps(dict(type="user", message=dict(role="user", content="go"))),
             _txt("w1", False), _txt("w2", True), _txt("w3", True), _txt("w4", False)]
    wf = _flags_of(woven, None, 3)["signal3_no_tool_streaks"]
    w_ok = (wf["longest"] == 2
            and wf["per_thread"][MAIN]["longest"] == 2
            and wf["per_thread"]["sidechains"]["longest"] == 2
            and wf["per_thread"][MAIN]["flagged"] == []
            and wf["per_thread"]["sidechains"]["flagged"] == [])
    print(f"  main={wf['per_thread'][MAIN]['longest']} "
          f"sidechains={wf['per_thread']['sidechains']['longest']} "
          f"longest anywhere={wf['longest']} (expected 2/2/2; a raw-order scan would say "
          f"4 and flag it against a threshold of 3)  {'OK' if w_ok else 'MISMATCH'}")
    if not w_ok:
        ok = False

    print("\nSELF-TEST [loop flags: a user turn answered with NO tool call is a span of 0 "
          "and must stay in the distribution]")
    # Found by mutating spans_of to drop empty spans: nothing asserted their presence
    # directly. They are also the whole reason a threshold of 0 is refused, so dropping
    # them would quietly lift every quantile AND weaken that refusal's stated rationale.
    empty = _flags_of(_flag_fixture_lines(0, 0), SPAN_T, STREAK_T)[
        "signal1_span_tool_calls"]
    e_ok = (empty["spans"] == 2 and [r["calls"] for r in empty["rows"]] == [0, 1]
            and empty["calls_p50"] == 0 and empty["flagged"] == [])
    print(f"  spans={empty['spans']} calls per span="
          f"{[r['calls'] for r in empty['rows']]} p50={empty['calls_p50']} "
          f"flagged={len(empty['flagged'])} (expected 2 spans, [0, 1], p50=0, none "
          f"flagged)  {'OK' if e_ok else 'MISMATCH'}")
    if not e_ok:
        ok = False

    print("\nSELF-TEST [loop flags: the user-entry classifier, entry by entry, including "
          "the engine-injected case no fixture above contains]")
    # Found by mutating the engine_injected rule away with no leg going red: the span
    # fixtures carry isMeta and tool_result entries but no promptSource one. Each row is a
    # shape measured in real transcripts on this box.
    cls_cases = [
        ("typed prompt, string content",
         dict(type="user", userType="external", promptSource="typed",
              message=dict(role="user", content="do the thing")), None),
        ("SDK prompt, list of text blocks",
         dict(type="user", userType="external", promptSource="sdk",
              message=dict(role="user", content=[dict(type="text", text="do it")])),
         None),
        ("engine's tool output coming back",
         dict(type="user", userType="external",
              message=dict(role="user",
                           content=[dict(type="tool_result", content="ok")])),
         "tool_result"),
        ("relaunch stub",
         dict(type="user", isMeta=True,
              message=dict(role="user", content=[dict(type="text",
                                                      text="Continue from where you "
                                                           "left off.")])), "isMeta"),
        ("engine-injected task notification",
         dict(type="user", userType="external", promptSource="system",
              message=dict(role="user", content="<task-notification>...")),
         "engine_injected"),
        ("isMeta AND tool_result -> counted once, under isMeta",
         dict(type="user", isMeta=True,
              message=dict(role="user",
                           content=[dict(type="tool_result", content="ok")])), "isMeta"),
        ("unclassifiable (empty content) -> treated as REAL, which SPLITS a span and so "
         "lowers every count rather than inventing work",
         dict(type="user", message=dict(role="user", content=[])), None),
    ]
    for label, entry, exp in cls_cases:
        got = user_exclusion_reason(entry)
        if got != exp:
            ok = False
        print(f"  {'real user turn' if got is None else got:<16} "
              f"expected={'real user turn' if exp is None else exp:<16} "
              f"{'OK' if got == exp else 'MISMATCH'}  {label}")

    print("\nSELF-TEST [loop flags: spans are computed PER SOURCE -- pooling two "
          "transcripts' turns first would report a span neither session had]")
    # Span indices restart at every transcript, so a merged turn list glues source A's
    # span 1 onto source B's. Demonstrated both ways below rather than asserted.
    ta, sa = collect_turns(_flag_fixture_lines(7, 0))
    tb, sb = collect_turns(_flag_fixture_lines(7, 0))
    per = compute_flags([("A", ta, sa), ("B", tb, sb)], SPAN_T, STREAK_T)[
        "signal1_span_tool_calls"]
    pooled = spans_of(ta + tb, sa["real_user_turns"] + sb["real_user_turns"])
    two_ok = ([r["calls"] for r in per["rows"]] == [7, 1, 7, 1]
              and per["spans"] == 4 and per["calls_max"] == 7
              and [r["calls"] for r in pooled] == [14, 2, 0, 0])
    print(f"  per source: spans={per['spans']} calls="
          f"{[r['calls'] for r in per['rows']]} max={per['calls_max']}  |  pooled "
          f"first: calls={[r['calls'] for r in pooled]} -- a 14-call span that never "
          f"happened  {'OK' if two_ok else 'MISMATCH'}")
    if not two_ok:
        ok = False

    print("\nSELF-TEST [loop flags: --anonymise must leave NO raw source path anywhere "
          "in the digest -- the mode whose only job is that the path is not there]")
    # This shipped broken: `flagged` holds separate copies of the span rows after the
    # digest's JSON round-trip, so anonymising `rows` alone left every flagged row's raw
    # path in the output. Asserted over the WHOLE serialised digest, not per field, so a
    # future list cannot reintroduce it.
    anon_args = argparse.Namespace(anonymise=True, top=2)
    anon_turns, anon_stats = collect_turns(_flag_fixture_lines(7, 4))
    leaky = "transcript-path-that-must-not-survive.jsonl"
    anon_digest = json.dumps(build_digest(
        [leaky], [], anon_turns, None, anon_args,
        compute_flags([(leaky, anon_turns, anon_stats)], SPAN_T, STREAK_T)))
    a_ok = (leaky not in anon_digest and anonymise(leaky) in anon_digest)
    print(f"  raw path present={leaky in anon_digest} (must be False), hashed form "
          f"present={anonymise(leaky) in anon_digest} (must be True)  "
          f"{'OK' if a_ok else 'MISMATCH'}")
    if not a_ok:
        ok = False

    print("\nSELF-TEST [loop flags: an unusable threshold must REFUSE -- 0, negative, and "
          "an env var that is set but unparseable]")
    refusals = [("--flag 0", resolve_threshold(0, "ARI_UNSET_FOR_SELFTEST")),
                ("--flag -1", resolve_threshold(-1, "ARI_UNSET_FOR_SELFTEST")),
                ("flag 1 (valid)", resolve_threshold(1, "ARI_UNSET_FOR_SELFTEST")),
                ("nothing set", resolve_threshold(None, "ARI_UNSET_FOR_SELFTEST"))]
    # the env legs set and restore ONE variable of their own in this process; no file is
    # written, and the name is this test's own, never one a caller might be relying on
    for raw in ("twenty", "0", "-4", "7"):
        os.environ["ARI_SELFTEST_THRESHOLD"] = raw
        refusals.append((f"env={raw!r}",
                         resolve_threshold(None, "ARI_SELFTEST_THRESHOLD")))
    os.environ.pop("ARI_SELFTEST_THRESHOLD", None)
    expected = [None, None, 1, None, None, None, None, 7]   # value, or None if refused
    must_err = [True, True, False, False, True, True, True, False]
    for (label, (val, err)), exp_val, exp_err in zip(refusals, expected, must_err):
        leg_ok = (val == exp_val) and (bool(err) == exp_err)
        if not leg_ok:
            ok = False
        print(f"  {label:<16} -> threshold={val} refused={bool(err)} "
              f"(expected threshold={exp_val} refused={exp_err}) "
              f"{'OK' if leg_ok else 'MISMATCH'}")

    print("\nSELF-TEST [loop flags: with NO threshold set, both signals must say NOT "
          "COMPUTED -- never 'clean' -- while still reporting the distribution]")
    nt = _flags_of(_flag_fixture_lines(7, 4))
    nt_ok = (nt["signal1_span_tool_calls"]["state"] == "not_computed"
             and nt["signal3_no_tool_streaks"]["state"] == "not_computed"
             and nt["signal1_span_tool_calls"]["flagged"] == []
             and nt["signal1_span_tool_calls"]["calls_max"] == 7
             and nt["signal3_no_tool_streaks"]["longest"] == 4)
    print(f"  states={nt['signal1_span_tool_calls']['state']}/"
          f"{nt['signal3_no_tool_streaks']['state']}, distribution still reported "
          f"(max={nt['signal1_span_tool_calls']['calls_max']}, longest streak="
          f"{nt['signal3_no_tool_streaks']['longest']}) "
          f"{'OK' if nt_ok else 'MISMATCH'}")
    if not nt_ok:
        ok = False

    print()
    if ok:
        print("SELF-TEST PASSED -- positive controls exact; both usage negative controls, "
              "both manifest guards, and\nevery loop-flag control fired (including the "
              "counterfactual that makes the flags' exclusion\nrule falsifiable).")
        return 0
    print("SELF-TEST FAILED -- do NOT believe this instrument's numbers.")
    return 3


def main():
    ap = argparse.ArgumentParser(
        description="Per-tool / per-subagent / per-subsession token breakdown from real "
                    "Claude Code transcripts (read-only).",
        epilog="Manifest format: a JSON list of "
               "{label, transcript_path, parent_label}.\n"
               "Loop-flag thresholds have NO DEFAULT and may be set by env var instead of "
               "flag:\n"
               "  ARI_FLAG_SPAN_TOOL_CALLS, ARI_FLAG_NO_TOOL_STREAK (both refuse a value "
               "below 1).\n"
               "Run --help-blind for this instrument's own failure modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcripts", nargs="*",
                    help="one or more transcript .jsonl paths")
    ap.add_argument("--manifest", metavar="PATH",
                    help="JSON manifest describing a tree of SEPARATE sessions to roll up")
    ap.add_argument("--json", action="store_true",
                    help="emit a compact digest for LLM review instead of the text report")
    ap.add_argument("--top", type=int, default=5, metavar="N",
                    help="how many costliest-by-cache-read turns to list (default 5)")
    ap.add_argument("--flag-span-tool-calls", type=int, default=None, metavar="N",
                    help="FLAG each span between consecutive real user turns that made "
                         "MORE THAN N tool calls (strictly greater). Env: "
                         "ARI_FLAG_SPAN_TOOL_CALLS. NO DEFAULT -- unset reports the "
                         "distribution and says NOT COMPUTED, because no published "
                         "threshold is grounded (framework ceilings span 10 to 1000; "
                         "sources in docs/citations.md). Whatever you pass is a starting "
                         "point, not a validated constant")
    ap.add_argument("--flag-no-tool-streak", type=int, default=None, metavar="N",
                    help="FLAG each run of MORE THAN N consecutive assistant turns that "
                         "called no tool at all. Env: ARI_FLAG_NO_TOOL_STREAK. Same "
                         "no-default posture as above (docs/citations.md)")
    ap.add_argument("--survey-user-entries", action="store_true",
                    help="print the census this instrument's span boundaries rest on -- "
                         "every `user` entry by whether it is operator input, and why not "
                         "if not -- then exit. Run it on your own corpus before trusting "
                         "signal 1 on it. Text only: --json is ignored, and says so")
    ap.add_argument("--anonymise", action="store_true",
                    help="hash transcript paths and manifest labels before printing "
                         "(for published output); never alters a token count")
    ap.add_argument("--self-test", action="store_true",
                    help="run the positive and negative controls and exit")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args()

    if args.help_blind:
        print(__doc__)
        return 0
    if args.self_test:
        return self_test()

    # Resolved BEFORE any file is opened, so an unusable threshold refuses on its own
    # terms rather than after a run that looks like it worked.
    span_threshold, err = resolve_threshold(args.flag_span_tool_calls,
                                            "ARI_FLAG_SPAN_TOOL_CALLS")
    if err:
        print(f"REFUSING: {err}. See --help-blind, blind mode 6.", file=sys.stderr)
        return 2
    streak_threshold, err = resolve_threshold(args.flag_no_tool_streak,
                                              "ARI_FLAG_NO_TOOL_STREAK")
    if err:
        print(f"REFUSING: {err}. See --help-blind, blind mode 6.", file=sys.stderr)
        return 2

    # assemble the set of sources: positional paths + manifest transcripts
    manifest_nodes = None
    if args.manifest:
        manifest_nodes, err = load_manifest(args.manifest)
        if err:
            print(f"REFUSING: {err}. A manifest that cannot be parsed is 'cannot "
                  f"measure', not 'measured nothing'. See --help-blind.", file=sys.stderr)
            return 2

    paths = list(args.transcripts)
    if manifest_nodes:
        for nd in manifest_nodes:
            if nd["path"] not in paths:
                paths.append(nd["path"])

    if not paths:
        print("REFUSING: no transcript given. Pass one or more .jsonl paths and/or "
              "--manifest. See --help-blind.", file=sys.stderr)
        return 2

    # read every source; an unreadable one is tracked as n/a, never counted as 0
    all_turns = []
    sources_read, sources_na = [], []
    own_totals = {}                       # per manifest label, for the rollup
    per_path_turns = {}
    per_path_stats = {}
    for p in paths:
        try:
            with open(p, "rb") as fh:
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            sources_na.append(p)
            continue
        turns, stats = collect_turns(lines)
        per_path_turns[p] = turns
        # Kept PER PATH, not merged: span indices restart at every transcript, so the
        # flags must be computed per source and concatenated (see compute_flags).
        per_path_stats[p] = stats
        all_turns.extend(turns)
        sources_read.append(p)

    if manifest_nodes:
        for nd in manifest_nodes:
            t = per_path_turns.get(nd["path"])
            own_totals[nd["label"]] = totals_of(t) if t is not None else None

    if not sources_read:
        print("REFUSING: not one source was readable, so a zero here would mean 'wrong "
              "path', not 'free session'. See --help-blind.", file=sys.stderr)
        return 2

    if args.survey_user_entries:
        if args.json:
            # Said rather than silently dropped: a caller who asked for machine output and
            # got prose should be told which flag won, not left to diff it.
            print("NOTE: --survey-user-entries is text only; --json is ignored for it.")
        print_user_entry_survey(sources_read, sources_na,
                               [(p, per_path_stats[p]) for p in sources_read], args)
        return 0

    flags = compute_flags([(p, per_path_turns[p], per_path_stats[p])
                           for p in sources_read], span_threshold, streak_threshold)

    cov = coverage(all_turns)
    reason = refusal_reason(cov)
    if reason:
        msg = f"REFUSING: {reason}. See --help-blind."
        if args.json:
            digest = build_digest(sources_read, sources_na, all_turns,
                                  (rollup(manifest_nodes, own_totals)
                                   if manifest_nodes else None), args, flags)
            digest["measured"] = False
            print(json.dumps(digest, indent=2))
            print(msg, file=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        return 2

    rollup_rows = rollup(manifest_nodes, own_totals) if manifest_nodes else None

    if args.json:
        print(json.dumps(build_digest(sources_read, sources_na, all_turns,
                                      rollup_rows, args, flags), indent=2))
        return 0

    print_report(sources_read, sources_na, all_turns, args, flags)
    if rollup_rows is not None:
        print_rollup(rollup_rows, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
