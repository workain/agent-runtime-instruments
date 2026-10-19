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

`--json` emits a compact digest (totals, the two breakdowns, the N costliest turns by
cache-read, a cache_read:output ratio per node) sized to hand to an LLM for qualitative
review. It is deliberately NOT the raw transcript, which runs to tens of MB.

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

    The positive control: `--self-test` builds a synthetic transcript with hand-computed
    totals (including a split turn, a sidechain, and a no-tool turn) and asserts the
    computed numbers match EXACTLY. TWO negative controls run in the same pass, because a
    control defeated by a one-field change is no control: (a) rename ALL usage fields and
    assert every turn reads as unmeasured and the run refuses; (b) rename a SINGLE field
    and assert the per-field diagnostic flags that one dimension as absent and the run
    still refuses -- the weaker all-or-nothing check passed (b) silently.
"""
import argparse
import hashlib
import json
import os
import sys

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
    unioned. Returns (turns, stats).
    """
    turns = {}            # message-id -> turn record (insertion order preserved)
    order = []
    stats = dict(bad_json=0, assistant_entries=0)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            stats["bad_json"] += 1
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
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

        names = set()
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                # structural match: block type AND the exact name field, never a substring
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    nm = b.get("name")
                    if isinstance(nm, str) and nm:
                        names.add(nm)

        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None

        t = turns.get(mid)
        if t is None:
            t = dict(tools=set(), usage=None, has_usage=False,
                     sidechain=False, agent=None)
            turns[mid] = t
            order.append(mid)
        t["tools"] |= names
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


def print_report(sources_read, sources_na, turns, args):
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


def build_digest(sources_read, sources_na, turns, rollup_rows, args):
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

    print()
    if ok:
        print("SELF-TEST PASSED -- positive control exact; both negative controls and "
              "both manifest guards fired.")
        return 0
    print("SELF-TEST FAILED -- do NOT believe this instrument's numbers.")
    return 3


def main():
    ap = argparse.ArgumentParser(
        description="Per-tool / per-subagent / per-subsession token breakdown from real "
                    "Claude Code transcripts (read-only).",
        epilog="Manifest format: a JSON list of "
               "{label, transcript_path, parent_label}. "
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
    for p in paths:
        try:
            with open(p, "rb") as fh:
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            sources_na.append(p)
            continue
        turns, _ = collect_turns(lines)
        per_path_turns[p] = turns
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

    cov = coverage(all_turns)
    reason = refusal_reason(cov)
    if reason:
        msg = f"REFUSING: {reason}. See --help-blind."
        if args.json:
            digest = build_digest(sources_read, sources_na, all_turns,
                                  (rollup(manifest_nodes, own_totals)
                                   if manifest_nodes else None), args)
            digest["measured"] = False
            print(json.dumps(digest, indent=2))
            print(msg, file=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        return 2

    rollup_rows = rollup(manifest_nodes, own_totals) if manifest_nodes else None

    if args.json:
        print(json.dumps(build_digest(sources_read, sources_na, all_turns,
                                      rollup_rows, args), indent=2))
        return 0

    print_report(sources_read, sources_na, all_turns, args)
    if rollup_rows is not None:
        print_rollup(rollup_rows, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
