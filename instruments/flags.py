#!/usr/bin/env python3
"""
FLAGS -- cheap, mechanical, no-model-call checks over a RECENT WINDOW of one transcript.

    WHAT WOULD THIS PRINT IF IT WERE BLIND?  (see --help-blind for the full list)

This is the library half of ARI#24. The detection logic here is NOT new: every flag is an
extraction of something already measured and validated elsewhere, cited at its own
function. What IS new is the READ PATH -- the instruments in this repository all parse a
transcript from byte 0, which is correct for a retrospective study and wrong for a process
that wants to look at a live session every few turns. So this module adds a windowed read
and then re-exposes the existing detectors on top of it.

-------------------------------------------------------------------------------------------
FOR AN OUTSIDE CALLER (alm#839's watchdog): THE WHOLE CONTRACT
-------------------------------------------------------------------------------------------

    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "ari_flags", "/path/to/agent-runtime-instruments/instruments/flags.py")
    F = importlib.util.module_from_spec(spec); spec.loader.exec_module(F)

    w = F.read_window("/path/to/session.jsonl", last_turns=40)
    out = F.all_flags(w, repeated_threshold=4, batchable_threshold=2, agent_gap_threshold=30)

    for name, fl in out["flags"].items():
        if fl["fired"]:
            ...                      # fl["evidence"] says why; fl["severity"] ranks it

Three ways to ask for a window; pass exactly one:

    read_window(path, last_turns=N)    the newest N logical assistant turns
    read_window(path, since=ISO8601)   every turn stamped at or after that instant
    read_window(path, from_offset=K)   everything appended since byte K  <-- the cheap one
    read_window(path)                  the whole file (the batch path, for comparison)

`from_offset` is the one built for a polling caller. Keep `w["window"]["next_offset"]`
from each call and pass it back as `from_offset` on the next one: the read is then
proportional to what the session APPENDED, not to how long it has been running. A file
that has not grown returns an empty window with `grew: False` and costs one stat() and one
open(); nothing downstream has to special-case it, because every flag reports `state:
"unmeasured"` on an empty window rather than a reassuring clean result.

EVERY FLAG RETURNS THE SAME SHAPE. One dict, these keys, always present:

    flag          str   the flag's own name, e.g. "repeated_invocations"
    state         str   "measured" | "not_computed" | "unmeasured"   <-- READ THIS FIRST
    fired         bool  always False unless state == "measured"
    measure       int   THE SCALAR COMPARED AGAINST THE THRESHOLD. A flag fires when
                        `measure > threshold`, strictly greater, for all three flags
    measure_of    str   what that scalar counts, in words, for a caller that logs it
    count         int   how many evidence rows the detector found (independent of threshold)
    threshold     int|None  the caller's threshold, echoed back
    severity      str|None  None, or "over_threshold"; see `overshoot`
    overshoot     float|None  measure/threshold when fired -- a RANK, not a calibrated scale
    evidence      list  per-finding rows; fingerprints and counts, never raw arguments
    fingerprint   str|None  one stable sha256-prefix for the whole finding set, for dedup
    window        dict  the provenance of what was actually read (see read_window)
    source        str   where this detector's logic comes from, with a pinned commit
    note          str   what the number does and does not mean

`measure` is NOT always `count`, and conflating the two is the one modelling mistake this
interface makes easy. For `batchable_bash` they coincide (mergeable pairs). For
`repeated_invocations`, `measure` is the LARGEST repeat count found while `count` is how
many repeat groups exist -- so a threshold of 4 means "one signature ran more than 4
times", which is `measure_token_breakdown.py`'s own meaning for its signal 2, preserved
here on purpose. For `agent_reuse_gap`, `measure` is the turn gap and `count` is 0 or 1.
Compare `measure`, report `evidence`, and `measure_of` says which is which in words.

THE THREE STATES ARE NOT TWO. This is the same posture the rest of the repository takes and
the single most important thing for a caller to get right:

    "measured"      the detector ran and `fired` is a real answer. `fired: False` here is a
                    MEASURED CLEAN RESULT and may be trusted as one.
    "not_computed"  the detector ran, `count` is real, but you passed no threshold, so
                    nothing can be "over" it. `fired` is False and means nothing.
    "unmeasured"    there was nothing to look at -- an empty window, no tool call at all,
                    no Bash pair. `fired` is False and means nothing. A watchdog that
                    treats this as "healthy" will report a dead session as a calm one.

`reason` is also present whenever state is not "measured", saying which of those it was.

NO FLAG SHIPS A DEFAULT THRESHOLD, deliberately, and this module will not invent one.
Every threshold here is a parameter with no default because no published value is grounded
-- the same argument `measure_token_breakdown.py` makes for its own three signals, with the
citations in docs/citations.md. `all_flags()` with no thresholds therefore returns three
`not_computed` flags and their raw counts, which is a useful baseline and is not a verdict.

PRIVACY. Nothing in an evidence row is a raw argument. Bash commands become
`Bash:<cluster>` labels drawn from the closed public vocabulary in
`measure_token_breakdown.bash_cluster()`, plus a sha256 fingerprint; tool arguments become
the same fingerprint `call_signature()` already uses; alm#831's dependency WITNESSES are
literal tokens lifted out of a command and its output, so only their COUNT crosses this
boundary, never the tokens. This is not decoration: these flags are built to be shipped to
a monitoring process that may log them.

-------------------------------------------------------------------------------------------
WHAT THIS MODULE DOES NOT DO
-------------------------------------------------------------------------------------------
It does not decide anything. It does not call a model. It does not write, message, kill or
park a session. It is a pure, read-only function of a transcript window, and the judgment
layer is somebody else's job (alm#839) on purpose: a mechanical flag that also acts is a
flag nobody can afford to tune.

BACKWARD COMPATIBILITY. `measure_token_breakdown.py` is not modified by this module and is
not forked into it -- it is IMPORTED, and its `collect_turns`/`repeated_runs`/
`repeated_in_spans`/`call_signature`/`bash_cluster` are called as they stand. So the
existing full-log batch analyses, and the downstream fleet-control repository's vendored
copy of that file, are unaffected by construction rather than by a test that happens to
pass -- verified by sha256: that vendored copy and this repository's are byte-identical,
and this change does not touch either.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_mtb():
    """Import measure_token_breakdown.py as a module, by path.

    By path and not by `import` because that file is a script with a hyphen-free but
    script-shaped name that lives next to this one, and because an outside caller loads
    THIS file by path too (see the module docstring) -- so there is no package context to
    rely on and pretending otherwise would break for exactly the caller this exists for.
    """
    path = os.path.join(HERE, "measure_token_breakdown.py")
    spec = importlib.util.spec_from_file_location("ari_measure_token_breakdown", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MTB = _load_mtb()

# Provenance strings. Pinned to a commit each, because "reused alm#831's logic" is a claim
# about a specific file at a specific revision, and the whole point of citing rather than
# re-deriving is that a reader can go and check the original.
# Pinned to a commit each, because "reused alm#831's logic" is a claim about a specific
# file at a specific revision, and the whole point of citing rather than re-deriving is
# that a reader can go and check the original. The upstream repository is named by its
# issue prefix (`alm#NNN`) rather than by path: this repository is public and
# scripts/scrub_check.sh refuses a private repo path, so the citation gives the issue and
# the SHA -- which is what identifies a revision -- and not a directory on someone's box.
SRC_ALM831 = ("alm#831 Tasks/20261020_alm831_bash_batching/{dep_check.py,"
              "analyze_batching.py} @ 1b7e486a89164599ec651902282b99f9d95eb287")
SRC_ALM833 = ("alm#833 Tasks/20261020_alm833_subagent_economy/census_c_delegability.py"
              " @ 5b8915ed1589f0ac2d17f1660ff24f76c23eb487")
SRC_ARI = "this repository, instruments/measure_token_breakdown.py (ARI#17 signal 2)"


# ==========================================================================================
# 1. THE WINDOWED READ PATH
# ==========================================================================================
#
# A transcript is append-only JSONL. That is the whole reason a window is possible: the
# newest turns are the last bytes, so a tail read reaches them without touching the rest.
#
# Growth factor 4, not 2. The loop below re-reads from a new, larger offset when the first
# read did not contain enough turns, so the bytes it reads total about (4/3) of the final
# chunk rather than 2x it. Four is a judgment call about one re-read versus a larger one; it
# is reported in `window.chunks` on every call so a caller can see what it actually cost
# instead of taking this paragraph's word for it.
#
# FIRST_CHUNK IS MEASURED, NOT GUESSED, and the first version of this file got it wrong.
# At 256 KB, every single transcript in the local corpus needed a growth re-read -- 10 of
# 10 -- so the window cost 256 KB + 1 MB on all of them and the "cost of a window" was
# really the cost of a bad first guess. `bench_window_read.py --sweep-first-chunk` is that
# measurement; over the 10 largest local transcripts it reads:
#
#       64K  12.94 MB, 30 reads, 10 of 10 files grew
#      128K  14.03 MB, 24 reads, 10 of 10 grew
#      256K  12.39 MB, 20 reads, 10 of 10 grew
#      512K  12.92 MB, 14 reads,  4 of 10 grew
#     1024K   9.97 MB, 10 reads,  0 of 10 grew      <- this value
#     2048K  19.88 MB, 10 reads,  0 of 10 grew
#     4096K  35.22 MB, 10 reads,  0 of 10 grew
#
# 1 MB is the minimum of that curve: large enough that a 40-turn window needs one read on
# every file in the corpus, small enough not to over-read. It is a property of how many
# bytes a turn takes, so a deployment with much larger or smaller turns should re-run the
# sweep rather than inherit this -- which is why `read_window` takes `first_chunk` as a
# parameter. The answer NEVER depends on it, only the cost, and `self_test` asserts exactly
# that by comparing a 64-byte first chunk against a 4 MB one.
FIRST_CHUNK = 1024 * 1024
GROWTH = 4


def _read_from(path, off, size):
    """Bytes from the first COMPLETE line at or after `off`, to EOF, plus that line's offset.

    The off-by-one here is the defect this function exists to not have. `off` lands wherever
    arithmetic put it, which is usually the middle of a line, and a reader that just drops
    `lines[0]` loses a whole real line whenever `off` happened to fall exactly on a line
    boundary. So the byte BEFORE `off` is read too and inspected: a newline there means `off`
    already starts a line and nothing may be dropped.

    `self_test()` asserts this against a real file at every offset of a dense range, in both
    cases, because "usually right" is indistinguishable from "right" on one example.
    """
    if off <= 0:
        with open(path, "rb") as fh:
            return fh.read(), 0
    if off >= size:
        return b"", size
    with open(path, "rb") as fh:
        fh.seek(off - 1)
        raw = fh.read()
    if raw[:1] == b"\n":
        return raw[1:], off                      # `off` was already a line start
    nl = raw.find(b"\n")
    if nl < 0:
        return b"", size                         # `off` fell inside the file's last line
    return raw[nl + 1:], off + nl                # first byte after that newline


def _assistant_mids(lines):
    """[(line index, logical turn id)] for the assistant entries among `lines`, in order.

    The turn id is `message.id` with the same fallbacks `collect_turns` uses, so "how many
    turns are in this window" and "how many turns does the analysis see" cannot disagree --
    they are the same key. A disagreement there would make `last_turns=N` silently return
    some other number of turns.
    """
    out = []
    n_asst = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        try:
            d = json.loads(s)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        n_asst += 1
        mid = msg.get("id") or d.get("requestId") or d.get("uuid") or ("_noid", n_asst)
        out.append((i, mid))
    return out


def _distinct_in_order(pairs):
    seen, order = set(), []
    for _, mid in pairs:
        if mid not in seen:
            seen.add(mid)
            order.append(mid)
    return order


def _first_ts(lines):
    """The earliest `timestamp` string among `lines`, or None if none carries one."""
    best = None
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            d = json.loads(s)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        ts = d.get("timestamp")
        if isinstance(ts, str) and ts and (best is None or ts < best):
            best = ts
    return best


def read_window(path, last_turns=None, since=None, from_offset=None, first_chunk=None):
    """Read a SLICE of one transcript and reduce it to turn records. The entrypoint.

    Pass at most one of `last_turns` / `since` / `from_offset`; passing none reads the whole
    file, which is the batch path and is here so the windowed answer can be compared against
    it (see `instruments/bench_window_read.py`, and the controls in `self_test`).

    Returns a dict: {"path", "turns", "stats", "window"}. `turns` and `stats` are exactly
    what `measure_token_breakdown.collect_turns` returns -- the same records the batch report
    is built from, because they came from the same function on a shorter list of lines.

    `window` is the provenance of the read, and a caller that reports a flag without it is
    reporting a number with no denominator:

        mode              which of the four reads happened
        file_bytes        the file's size when it was stat()ed
        bytes_read        how many bytes were actually pulled off disk, re-reads included
        read_fraction     bytes_read / file_bytes -- the cost claim, measured per call
        start_offset      byte offset of the first line kept
        next_offset       pass this back as `from_offset` next time. == file_bytes
        lines_total       lines the read pulled in
        lines_kept        lines left after trimming to the requested window
        turns             logical assistant turns in the window
        chunks            how many reads it took to find them (1 = the first guess was big
                          enough; see FIRST_CHUNK)
        grew              from_offset mode only: did the file gain any bytes at all
        rotated           from_offset mode only: the file is SHORTER than the offset given,
                          so it was truncated or replaced. The window falls back to a full
                          read and says so, rather than seeking past the end and reporting
                          an empty, calm session.
        windowed          False only for the whole-file read
        spans_are_local   True whenever the window does not start at byte 0 -- see below
        head_complete     True when the OLDEST turn in the window is known to be whole

    SPANS ARE LOCAL IN A WINDOW, AND THAT CHANGES WHAT A SPAN NUMBER MEANS. `collect_turns`
    numbers spans by counting real user turns from the start of what it is given, so in a
    window span 1 is the first operator prompt INSIDE THE WINDOW, not the first of the
    session; and turns before it land in span 0, the preamble. Nothing is wrong with that --
    it is what a caller watching recent activity wants -- but a span index from a window must
    never be compared with one from a full read, and `spans_are_local` is in every flag's
    `window` block so that mistake is visible rather than available.

    WHY `last_turns=N` READS N+1 TURNS AND THEN THROWS ONE AWAY. One logical turn is written
    as several JSONL entries (a thinking entry, then one per tool_use, all repeating the same
    `usage`). A tail read can therefore land in the middle of a turn, and the oldest turn in
    the window would be a fragment -- with some of its tool calls outside, which would
    under-count `repeated_invocations` by exactly the amount nobody could see. So the read
    continues until N+1 distinct turns are present and then trims to the newest N: the
    discarded turn is the proof that the oldest kept one is complete. `head_complete` records
    whether that proof was obtained (it is False only when the file itself has fewer than
    N+1 turns, in which case the window starts at byte 0 and the question does not arise).
    """
    given = [x is not None for x in (last_turns, since, from_offset)]
    if sum(given) > 1:
        raise ValueError("pass at most one of last_turns / since / from_offset")
    if last_turns is not None and last_turns < 1:
        raise ValueError("last_turns must be >= 1; a window of zero turns is not a window")

    size = os.path.getsize(path)
    win = dict(mode="full", file_bytes=size, bytes_read=0, read_fraction=None,
               start_offset=0, next_offset=size, lines_total=0, lines_kept=0, turns=0,
               chunks=0, grew=None, rotated=False, windowed=False,
               spans_are_local=False, head_complete=True)

    # ---- mode: from_offset. One read, no doubling: the caller already told us where. ----
    if from_offset is not None:
        win["mode"] = "from_offset"
        win["windowed"] = True
        if from_offset > size:
            # Truncated, rotated, or a cursor from a different file. Reading from the given
            # offset would return nothing, and "nothing happened" is the most dangerous
            # thing this module could say about a session. Fall back and label it.
            win["rotated"] = True
            from_offset = 0
        raw, start = _read_from(path, max(0, from_offset), size)
        win["chunks"] = 1
        win["bytes_read"] = len(raw)
        win["start_offset"] = start
        win["grew"] = len(raw) > 0
        win["spans_are_local"] = start > 0
        lines = raw.decode("utf-8", "replace").splitlines()
        win["lines_total"] = len(lines)
        kept = lines
    # ---- mode: full. Byte 0 to EOF -- the batch path, unchanged in behaviour. ----
    elif not any(given):
        with open(path, "rb") as fh:
            raw = fh.read()
        win["chunks"] = 1
        win["bytes_read"] = len(raw)
        kept = raw.decode("utf-8", "replace").splitlines()
        win["lines_total"] = len(kept)
    # ---- modes: last_turns / since. Tail read, grown until the window is satisfied. ----
    else:
        win["mode"] = "last_turns" if last_turns is not None else "since"
        win["windowed"] = True
        chunk = first_chunk or FIRST_CHUNK
        total_read = 0
        chunks = 0
        while True:
            chunks += 1
            raw, start = _read_from(path, max(0, size - chunk), size)
            total_read += len(raw)
            lines = raw.decode("utf-8", "replace").splitlines()
            at_bof = start == 0
            if last_turns is not None:
                pairs = _assistant_mids(lines)
                mids = _distinct_in_order(pairs)
                # N+1, so the Nth-newest turn is provably whole. See the docstring.
                enough = len(mids) >= last_turns + 1
            else:
                ft = _first_ts(lines)
                enough = ft is not None and ft < since
            if enough or at_bof or chunk >= size:
                break
            chunk *= GROWTH
        win["chunks"] = chunks
        win["bytes_read"] = total_read
        win["start_offset"] = start
        win["lines_total"] = len(lines)

        if last_turns is not None:
            pairs = _assistant_mids(lines)
            mids = _distinct_in_order(pairs)
            if len(mids) > last_turns:
                keep = set(mids[-last_turns:])
                cut = next(i for i, mid in pairs if mid in keep)
                kept = lines[cut:]
                win["head_complete"] = True
            else:
                # The whole file has <= N turns. start is 0 here (the loop only stops early
                # when `enough`), so nothing was cut and nothing can be partial.
                kept = lines
                win["head_complete"] = at_bof
        else:
            cut = 0
            for i, line in enumerate(kept_src := lines):
                s = line.strip()
                if not s:
                    continue
                try:
                    d = json.loads(s)
                except Exception:
                    continue
                ts = d.get("timestamp") if isinstance(d, dict) else None
                if isinstance(ts, str) and ts >= since:
                    cut = i
                    break
            else:
                cut = len(kept_src)
            kept = lines[cut:]
            # A `since` window's oldest turn can still be a fragment: the cut lands on the
            # first entry stamped at or after the instant asked for, and that entry may be
            # the second row of a turn whose first row is older. Said, not hidden.
            win["head_complete"] = at_bof and cut == 0
        win["spans_are_local"] = win["start_offset"] > 0 or (last_turns is not None
                                                             and len(kept) < len(lines))

    win["lines_kept"] = len(kept)
    win["read_fraction"] = (win["bytes_read"] / size) if size else None
    turns, stats = MTB.collect_turns(kept)
    win["turns"] = len(turns)
    return dict(path=path, turns=turns, stats=stats, window=win, lines=kept)


# ==========================================================================================
# 2. THE COMMON FLAG SHAPE
# ==========================================================================================

STATES = ("measured", "not_computed", "unmeasured")

# What each flag's `measure` counts, in words, carried in its own output. See `_flag`.
BB_MEASURE = "safely-mergeable consecutive single-Bash-call pairs in the window"
RI_MEASURE = ("the largest number of occurrences of ONE tool+arguments signature, "
              "consecutively or within one local span")
ARG_MEASURE = "main-thread turns since the last Agent/Task dispatch"


def _fingerprint(rows):
    """One stable sha256 prefix over a finding set, so a caller can tell "the same flag,
    still the same evidence" from "the same flag, new evidence" without diffing rows.

    Over the rows as canonicalised JSON, sorted: a detector that returns the same findings
    in a different order has not found anything new. Returns None for an empty set rather
    than the digest of `[]`, which would be a fingerprint of nothing and would compare equal
    across two unrelated clean windows.
    """
    if not rows:
        return None
    canon = json.dumps(sorted(json.dumps(r, sort_keys=True, default=str) for r in rows),
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _flag(name, state, rows, threshold, window, source, note, measure=0, measure_of="",
          gate=True, gate_reason=None, reason=None, extra=None):
    """Assemble the one output shape every flag in this module returns.

    One function, so the shape cannot drift between flags -- a caller writing
    `if fl["fired"]` against three detectors that each built their own dict is a caller one
    typo away from reading a missing key as False.

    THE FIRING RULE IS THE SAME FOR ALL THREE FLAGS: a flag fires when its own `measure`
    is STRICTLY GREATER than the threshold. What differs is which scalar `measure` is, and
    that is why it is a named field in the output rather than something a caller has to
    infer from `len(evidence)`:

        batchable_bash        the number of safely-mergeable pairs in the window
        repeated_invocations  the LARGEST repeat count found, over either window
        agent_reuse_gap       main-thread turns since the last Agent/Task dispatch

    `measure_of` is that sentence, carried in the output, so a caller logging one flag can
    say what the number was. The first version of this module compared `len(evidence)`
    for every flag, which silently changed `repeated_invocations`' meaning from
    measure_token_breakdown.py's ("flag each group of MORE THAN N occurrences of one
    signature") to "flag when more than N groups exist" -- the self-test's positive control
    caught it. Hence one explicit scalar per flag instead of one convenient length.

    STRICTLY GREATER matches `compute_flags` in measure_token_breakdown.py exactly: a
    measure EQUAL to the threshold does not fire. Asserted from both sides for all three
    flags in `self_test`, because an off-by-one in a comparison nobody tested is the
    cheapest possible way for a monitoring layer to be quietly wrong.

    `gate` is a second, flag-specific condition that must ALSO hold (only
    `agent_reuse_gap` uses one: a long gap is not a missed handoff unless the work looked
    delegable). A failed gate is reported in `gate_reason` and never silently swallowed.
    """
    assert state in STATES, state
    over = bool(state == "measured" and threshold is not None and measure > threshold)
    fired = bool(over and gate)
    out = dict(flag=name, state=state, fired=fired, count=len(rows),
               measure=measure, measure_of=measure_of, threshold=threshold,
               severity=("over_threshold" if fired else None),
               overshoot=(round(measure / threshold, 3) if fired and threshold else None),
               evidence=rows, fingerprint=_fingerprint(rows),
               window=dict(window), source=source, note=note)
    if state != "measured":
        out["reason"] = reason or "not stated"
    if over and not fired:
        out["gate_reason"] = gate_reason or "a flag-specific precondition did not hold"
    if extra:
        out.update(extra)
    return out


def _empty(window):
    """Is there nothing in this window to look at at all?"""
    return window["turns"] == 0


# ==========================================================================================
# 3a. FLAG: batchable_bash  -- EXTRACTED FROM alm#831, NOT RE-DERIVED
# ==========================================================================================
#
# Everything from here to the end of this block is alm#831's own code, copied rather than
# re-implemented, exactly as ARI#24 asks ("reuse dep_check.py's witnesses()/jaccard() logic,
# don't re-derive it"). It is reproduced verbatim apart from two changes, both forced and
# both named at their site:
#
#   * `load()` is split so it takes LINES instead of a path. That is the windowing
#     adaptation and the only reason this code is here at all; the parse itself is untouched.
#   * nothing prints a raw command. alm#831 is a private research task and dumps commands
#     into its sample files; this module is built to be called by a monitoring process, so
#     the evidence rows carry a public cluster label, a digest, and a witness COUNT.
#
# The rules in `specific()` and the 0.60 retry threshold were tuned against 30 hand-labelled
# pairs and then re-validated against a fresh disjoint batch of 40 -- see alm#831's
# manual_classification.tsv / validation_classification.tsv. They are not re-tuned here, and
# this module has no business re-tuning them: a threshold validated upstream and then quietly
# moved downstream is a threshold with no validation at all.

STOP = set('''echo cat grep sed awk head tail find python python3 bash sh git gh ls pwd cd
mkdir rmdir touch chmod curl wget sort uniq wc diff jq true false then else elif fi for do
done while case esac function return exit local export unset read printf test null none true
false http https null void name type file path line text json yaml data main master origin
branch commit remote status show view list repo issue pull request body title head base null
and the for with from into that this they them when what which where there here will have has
been would could should about after before again more most some such only other than then
also just like over under very much many each both all any new old get set put add del
error warn info debug trace fatal pass fail skip todo note self none null true'''.split())

TOK = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.:/#-]{2,}')
HEXISH = re.compile(r'^[0-9a-f]{7,}$')

SKIP_TYPES = {'system', 'attachment', 'file-history-snapshot', 'last-prompt', 'mode',
              'permission-mode', 'ai-title', 'queue-operation', 'bridge-session', 'summary'}


def tokens(s):
    """alm#831 dep_check.py::tokens -- verbatim."""
    out = set()
    for m in TOK.finditer(s or ''):
        t = m.group(0).strip('.:/#-')
        if len(t) < 3:
            continue
        if t.lower() in STOP:
            continue
        out.add(t)
    return out


def specific(t):
    """alm#831 dep_check.py::specific -- verbatim.

    A witness only counts if it is SPECIFIC enough that its presence in A's output is
    evidence rather than coincidence: it carries a digit, a path/dot separator, is hex-like
    (a SHA), or is long. Hand classification found generic English and generic shell words
    ("not", "was", "cwd", "memory") were the dominant false-positive class, and a stopword
    list cannot keep up with that.
    """
    return (any(c.isdigit() for c in t) or '/' in t or '.' in t
            or HEXISH.match(t.lower()) is not None or len(t) >= 10)


def jaccard(a, b):
    """alm#831 dep_check.py::jaccard -- verbatim."""
    A, B = tokens(a), tokens(b)
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


def witnesses(a_cmd, a_out, b_cmd):
    """alm#831 dep_check.py::witnesses -- verbatim.

    Tokens in B that came from A's OUTPUT and are not already in A's COMMAND. The intuition
    is concrete: `grep -n foo` then `sed -n '358,378p'` -- the literal 358 is in A's output,
    not in A's command, so B was written by reading A, and merging the two would delete a
    decision point rather than save a turn.
    """
    if not a_out:
        return set()
    out = set()
    for t in tokens(b_cmd):
        if t in a_cmd:
            continue
        probes = [t]
        if '/' in t:
            tail = t.rstrip('/').split('/')[-1]
            if len(tail) >= 4:
                probes.append(tail)
        for pr in probes:
            if not specific(pr):
                continue
            if pr in a_cmd:
                continue
            if pr in a_out:
                out.add(pr)
                break
    return out


def result_text(row):
    """alm#831 dep_check.py::result_text -- verbatim. The tool_result text of a `user` row."""
    c = row.get('message', {}).get('content')
    if not isinstance(c, list):
        return ''
    parts = []
    for b in c:
        if not (isinstance(b, dict) and b.get('type') == 'tool_result'):
            continue
        cc = b.get('content')
        if isinstance(cc, str):
            parts.append(cc)
        elif isinstance(cc, list):
            for x in cc:
                if isinstance(x, dict) and x.get('type') == 'text':
                    parts.append(x.get('text') or '')
    return '\n'.join(parts)


def is_pure_tool_result(row):
    """alm#831 analyze_batching.py::is_pure_tool_result -- verbatim."""
    c = row.get('message', {}).get('content')
    if not isinstance(c, list) or not c:
        return False
    return all(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c)


def load_lines(lines):
    """alm#831 analyze_batching.py::load, taking LINES instead of a path.

    The only change is the input: the body is the original. This is the windowing adaptation
    -- the parentUuid graph is rebuilt over the window's rows, which is sound because a pair
    both of whose members are in the window has its whole chain in the window too. The one
    thing lost is a pair anchored at a turn OLDER than the window, and that pair is outside
    the window by definition, so it is not an error so much as the boundary.
    """
    import collections
    rows, nodes, children = [], {}, collections.defaultdict(list)
    turns = collections.OrderedDict()
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        u = d.get('uuid')
        if not u or u in nodes:
            continue
        nodes[u] = d
        rows.append(d)
        children[d.get('parentUuid')].append(u)
        if d.get('type') != 'assistant':
            continue
        msg = d.get('message', {}) or {}
        mid = msg.get('id') or d.get('requestId') or u
        d['_mid'] = mid
        t = turns.get(mid)
        if t is None:
            t = turns[mid] = dict(mid=mid, tools=[], bash=[], usage=None, rows=[],
                                  cwd=d.get('cwd'), sid=d.get('isSidechain'),
                                  ver=d.get('version'), ts=d.get('timestamp'))
        t['rows'].append(u)
        if t['usage'] is None and isinstance(msg.get('usage'), dict):
            t['usage'] = msg['usage']
        c = msg.get('content')
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get('type') == 'tool_use':
                    t['tools'].append(b.get('name'))
                    if b.get('name') == 'Bash':
                        inp = b.get('input')
                        t['bash'].append((inp.get('command', '') or '')
                                         if isinstance(inp, dict) else '')
    return rows, nodes, children, turns


def next_tool_turn(last_uuid, mid, nodes, children, turns):
    """alm#831 analyze_batching.py::next_tool_turn -- verbatim.

    BFS forward along the real parentUuid chain, not file order, so a resumed or branched
    transcript cannot manufacture false adjacency. Returns (next tool-turn mids, barrier
    reason): a real user turn or a text-only assistant turn is a BARRIER, and a pair across
    one is never counted, because something conversational intervened.
    """
    seen, frontier, found, barrier = set(), [last_uuid], [], None
    steps = 0
    while frontier and steps < 400:
        steps += 1
        nxt = []
        for u in frontier:
            for ch in children.get(u, []):
                if ch in seen:
                    continue
                seen.add(ch)
                r = nodes[ch]
                ty = r.get('type')
                if ty in SKIP_TYPES:
                    nxt.append(ch); continue
                if ty == 'user':
                    if is_pure_tool_result(r):
                        nxt.append(ch); continue
                    barrier = barrier or 'user_turn'
                    continue
                if ty == 'assistant':
                    m = r.get('_mid')
                    if m == mid:
                        nxt.append(ch); continue
                    if turns.get(m, {}).get('tools'):
                        found.append(m)
                    else:
                        barrier = barrier or 'assistant_text_turn'
                        nxt.append(ch)
                    continue
                nxt.append(ch)
        frontier = nxt
    return found, barrier


RETRY_JACCARD = 0.60            # alm#831's own value, reported there rather than optimised

BATCHABLE_NOTE = (
    "Counts CONSECUTIVE pairs of single-Bash-call turns that alm#831's dependency proxy "
    "finds SAFE to merge: no token in B's command came from A's recorded output, and B is "
    "not a near-duplicate retry of A (jaccard < %.2f). A merge removes one turn, and one "
    "turn is one cache-replay tax payment -- that is the whole reason the count is "
    "interesting. It is a PROXY and alm#831 reports its own false-positive and "
    "false-negative rates against hand labels; read the count through those, not instead "
    "of them. It is also a FLOOR in a window: a pair whose first half sits before the "
    "window's start is not visible here, and `window.spans_are_local` tells you the window "
    "has a start." % RETRY_JACCARD
)


def batchable_bash(win, threshold=None):
    """FLAG: recent Bash calls that could have been one call.

    Extraction of alm#831's crux measurement, on a window. For each clean consecutive pair
    of single-Bash-call turns, alm#831 asks whether B's command was written after reading
    A's OUTPUT; if it was, the pair is DEPENDENT and merging it would delete a decision
    point. The pairs left over are the safely mergeable ones, and this flag counts those.

    Evidence rows -- no raw command, by design (see the module docstring on privacy):
        a_cluster/b_cluster   the public command-cluster labels from bash_cluster()
        a_fp/b_fp             8 hex of the sha256 of each command
        jaccard               command-to-command similarity, rounded
        a_output_bytes        how much output A actually produced
        witness_count         0 for every row here -- a mergeable pair has no witness --
                              carried anyway so an evidence row's shape does not change
                              between this flag and a future dependent-pair report
    """
    if _empty(win["window"]):
        return _flag("batchable_bash", "unmeasured", [], threshold, win["window"],
                     SRC_ALM831, BATCHABLE_NOTE, measure_of=BB_MEASURE,
                     reason="the window holds no assistant turn at all")

    import collections
    rows_, nodes, children, turns = load_lines(win["lines"])
    if not turns:
        return _flag("batchable_bash", "unmeasured", [], threshold, win["window"],
                     SRC_ALM831, BATCHABLE_NOTE, measure_of=BB_MEASURE,
                     reason="no assistant turn carried a uuid, so no adjacency chain exists")

    # turn mid -> the tool_result text of its calls, exactly as dep_check.py assembles it
    res = collections.defaultdict(list)
    for u, r in nodes.items():
        if r.get('type') != 'user':
            continue
        p = r.get('parentUuid')
        while p in nodes and nodes[p].get('type') == 'user':
            p = nodes[p].get('parentUuid')
        if p in nodes and nodes[p].get('type') == 'assistant':
            res[nodes[p].get('_mid')].append(result_text(r))

    seen_pair = set()
    pairs = 0
    mergeable = []
    no_output = 0
    for m, t in turns.items():
        if not t['tools']:
            continue
        nxts, barrier = next_tool_turn(t['rows'][-1], m, nodes, children, turns)
        if barrier:
            continue
        for nx in nxts:
            if (m, nx) in seen_pair or nx not in turns:
                continue
            seen_pair.add((m, nx))
            a, b = turns[m], turns[nx]
            if not (len(a['tools']) == 1 and len(a['bash']) == 1
                    and len(b['tools']) == 1 and len(b['bash']) == 1):
                continue
            pairs += 1
            a_out = '\n'.join(res.get(m, []))
            if not a_out:
                no_output += 1
            w = witnesses(a['bash'][0], a_out, b['bash'][0])
            jc = jaccard(a['bash'][0], b['bash'][0])
            if jc >= RETRY_JACCARD:
                # alm#831's hand-found false negative: a near-duplicate command is a retry,
                # and the dependency is on A's FAILURE, which leaves no literal behind for
                # the witness test to find. Treated as dependent there; treated as NOT
                # mergeable here, which is the same decision seen from the other side.
                continue
            if w:
                continue
            mergeable.append(dict(
                a_cluster=MTB.bash_cluster(a['bash'][0]),
                b_cluster=MTB.bash_cluster(b['bash'][0]),
                a_fp=hashlib.sha256(a['bash'][0].encode("utf-8", "surrogateescape"))
                                   .hexdigest()[:8],
                b_fp=hashlib.sha256(b['bash'][0].encode("utf-8", "surrogateescape"))
                                   .hexdigest()[:8],
                jaccard=round(jc, 3), a_output_bytes=len(a_out), witness_count=0,
                thread=("sidechain" if a.get('sid') else MTB.MAIN)))

    if pairs == 0:
        return _flag("batchable_bash", "unmeasured", [], threshold, win["window"],
                     SRC_ALM831, BATCHABLE_NOTE, measure_of=BB_MEASURE,
                     reason="not one clean consecutive single-Bash-call pair in the window; "
                            "zero mergeable pairs out of zero pairs is not a clean result",
                     extra=dict(pairs_examined=0, pairs_with_no_recorded_output=0))
    state = "not_computed" if threshold is None else "measured"
    return _flag("batchable_bash", state, mergeable, threshold, win["window"],
                 SRC_ALM831, BATCHABLE_NOTE,
                 measure=len(mergeable), measure_of=BB_MEASURE,
                 reason=("no threshold given, so the count is reported and nothing is judged"
                         if state == "not_computed" else None),
                 extra=dict(pairs_examined=pairs,
                            pairs_with_no_recorded_output=no_output))


# ==========================================================================================
# 3b. FLAG: repeated_invocations  -- THIS REPOSITORY'S OWN SIGNAL 2, ON A WINDOW
# ==========================================================================================

REPEATED_NOTE = MTB.SIGNAL2_NOTE + (
    " In a WINDOW there is one further floor: a run that began before the window's first "
    "kept turn is seen only from the window's start, so its length is truncated, and a "
    "repeat whose other occurrences are all older is not seen at all. Span indices are "
    "LOCAL to the window (see read_window) -- an in-span multiplicity from a window and one "
    "from a full read are not the same measurement and must not be compared."
)


def repeated_invocations(win, threshold=None):
    """FLAG: the same tool with the same arguments, over and over, recently.

    This is `measure_token_breakdown.py`'s signal 2 (ARI#17) with nothing changed about the
    detection: it calls that file's own `repeated_runs` and `repeated_in_spans` on the
    window's turn records. Both windows are kept because neither subsumes the other -- a
    loop that alternates `A B A B A` has no consecutive run longer than 1 but ran A three
    times inside one span.

    Evidence rows are that function's own rows, shape unchanged: window ("consecutive" or
    "span"), thread, span index, the closed-vocabulary label, 8 hex of the argument digest,
    and the count. No argument value crosses this boundary -- see `call_signature` in
    measure_token_breakdown.py for why the digest is compared in full and shown truncated.
    """
    w = win["window"]
    if _empty(w):
        return _flag("repeated_invocations", "unmeasured", [], threshold, w, SRC_ARI,
                     REPEATED_NOTE, measure_of=RI_MEASURE,
                     reason="the window holds no assistant turn at all")

    turns = win["turns"]
    calls = sum(len(t["call_keys"]) for t in turns)
    rows = []
    for thread, rec in MTB.repeated_runs(turns).items():
        for lab, sig, n in rec["runs"]:
            rows.append(dict(window="consecutive", thread=thread, span=None,
                             label=lab, fingerprint=sig[:8], count=n))
    for idx, lab, sig, n in MTB.repeated_in_spans(turns, win["stats"]["real_user_turns"]):
        rows.append(dict(window="span", thread=None, span=idx,
                         label=lab, fingerprint=sig[:8], count=n))

    if calls == 0:
        # The same distinction measure_token_breakdown.py draws: zero tool calls is not "no
        # repetition found", it is nothing to look at.
        return _flag("repeated_invocations", "unmeasured", [], threshold, w, SRC_ARI,
                     REPEATED_NOTE, measure_of=RI_MEASURE,
                     reason="no tool call was seen at all in the window",
                     extra=dict(calls=0, distinct_call_keys=0,
                                consecutive_longest=0, in_span_max=0))
    state = "not_computed" if threshold is None else "measured"
    cons = [r["count"] for r in rows if r["window"] == "consecutive"]
    inspan = [r["count"] for r in rows if r["window"] == "span"]
    return _flag("repeated_invocations", state, rows, threshold, w, SRC_ARI, REPEATED_NOTE,
                 measure=max([r["count"] for r in rows] or [0]), measure_of=RI_MEASURE,
                 reason=("no threshold given, so the counts are reported and nothing is "
                         "judged" if state == "not_computed" else None),
                 extra=dict(calls=calls,
                            distinct_call_keys=len({k for t in turns
                                                    for k in t["call_keys"]}),
                            consecutive_longest=(max(cons) if cons else 0),
                            in_span_max=(max(inspan) if inspan else 0)))


# ==========================================================================================
# 3c. FLAG: agent_reuse_gap  -- DELEGABILITY DEFINITION TAKEN FROM alm#833
# ==========================================================================================
#
# alm#833's classification is copied below verbatim, for the same reason alm#831's is: the
# issue says to cite its definition of "delegable-looking" rather than invent one, and its
# three-class proxy is the definition. It was validated against 120 hand-labelled
# main-thread turns -- 94.8% three-class agreement, 6 disagreements of 116, 4 rows excluded
# as unjudgeable -- and alm#833 states the proxy is BIASED, so the number here inherits that
# bias and says so in its note rather than presenting itself as clean.

# alm#833 stripped a HARD-CODED LIST of two MCP server prefixes before testing the verb.
# This is the one place its code is generalised rather than copied, and the generalisation
# is a deliberate change with a reason on both sides:
#
#   WHY IT HAD TO CHANGE. Those two prefixes name one deployment's own fleet-control MCP
#   servers. This repository is public and `scripts/scrub_check.sh` refuses private
#   service names in the tree -- correctly, since a published instrument that only
#   classifies one site's tool namespace is of no use to anyone else and leaks the
#   namespace while being useless. Spelling the names so the gate cannot see them would
#   be defeating the gate rather than satisfying it.
#
#   WHAT IT CHANGES. `_strip_mcp` removes ANY `mcp__<server>__` prefix, so the verb test
#   below applies uniformly instead of only to two known servers -- which is plainly what
#   alm#833 intended ("Any other MCP verb: read-only github reads etc. are content-ish").
#   On the two servers alm#833 knew, the classification is IDENTICAL, and `self_test`
#   asserts that against a table of real tool names rather than claiming it.
#
#   WHERE IT DIVERGES, stated rather than buried. For a tool from some OTHER MCP server,
#   alm#833 could not strip the prefix, so its `get_`/`list_`/`github_` test ran against
#   the full `mcp__other__get_thing` -- which never starts with `get_` -- and the tool
#   came out ORCHESTRATION. Here the prefix comes off first, so it comes out CONTENT.
#   That is a real behavioural difference on third-party MCP servers, it is the answer
#   alm#833's own stated rule would have given, and `self_test` demonstrates the case.
#
# `orch_prefixes`/`orch_tools` are parameters on `tool_class` so a deployment with a
# different vocabulary can supply it instead of patching this file.
MCP_PREFIX = re.compile(r'^mcp__[^_]+(?:_[^_]+)*__')
ORCH_TOOLS = {
    'send_message', 'spawn_child_session', 'spawn_sibling_session', 'kill_session',
    'edit_session_metadata', 'set_tree_metadata', 'orient', 'list_sessions',
    'get_session_state', 'get_session_states', 'get_session_health', 'set_session_group',
    'create_session_group', 'list_session_groups', 'get_session_group',
    'set_session_archived', 'get_session_archived', 'archive_folder', 'unarchive_folder',
    'create_folder', 'delete_folder', 'rename_folder', 'create_project', 'delete_project',
    'rename_project', 'list_projects', 'list_folders', 'set_folder_group',
    'get_folder_group', 'switch_session_account', 'list_accounts',
    'github_comment_on_issue', 'github_create_issue', 'list_my_comments',
    'answer_user_prompt', 'assign', 'handoff', 'report_outcome', 'update_metadata',
    'list_siblings', 'delete_terminal', 'emit_ui',
}
DISPATCH_TOOLS = {'Agent', 'Task'}
CONTENT_HINT = {'Bash', 'Read', 'Edit', 'Write', 'Grep', 'Glob', 'NotebookEdit', 'WebFetch',
                'WebSearch', 'MultiEdit', 'TodoWrite', 'Monitor'}


def _strip_mcp(name):
    """`mcp__<server>__verb` -> `verb`; anything else unchanged.

    The deployment-neutral replacement for alm#833's two hard-coded server prefixes -- see
    the block comment above MCP_PREFIX for why it had to change, where it agrees, and the
    one case where it deliberately does not.
    """
    return MCP_PREFIX.sub('', name, count=1)


def tool_class(name, orch_tools=None):
    """alm#833 census_c_delegability.py::tool_class, with its prefix list generalised.

    The body is alm#833's. The only change is `_strip_mcp(name)` where it had a loop over
    two literal server prefixes; `orch_tools` is exposed so the verb vocabulary can be
    replaced without editing this file.
    """
    tools = ORCH_TOOLS if orch_tools is None else orch_tools
    if name in DISPATCH_TOOLS:
        return 'ALREADY_DELEGATED'
    short = _strip_mcp(name)
    if short in tools:
        return 'ORCHESTRATION'
    if name.startswith('mcp__'):
        if short.startswith('github_') or short.startswith('list_') or short.startswith('get_'):
            return 'CONTENT'
        return 'ORCHESTRATION'
    if name in CONTENT_HINT:
        return 'CONTENT'
    return 'CONTENT'


def turn_class(tools, orch_tools=None):
    """alm#833 census_c_delegability.py::turn_class -- verbatim apart from taking the tool
    set directly instead of a turn dict, so it can be fed this repository's turn records.

      ORCHESTRATION      the turn IS fleet control. A subagent cannot do it FOR the parent:
                         the parent is the thing with the authority and the identity.
      ALREADY_DELEGATED  the turn dispatches an Agent/Task.
      REPORT             text-only, no tool call -- the agent talking to its operator.
                         Non-delegable by construction: a subagent reports to the parent.
      CONTENT            Bash/Read/Edit/Write/Grep/... The delegable candidate.
    """
    if not tools:
        return 'REPORT'
    cs = [tool_class(x, orch_tools) for x in tools]
    if 'ALREADY_DELEGATED' in cs:
        return 'ALREADY_DELEGATED'
    if 'ORCHESTRATION' in cs:
        return 'ORCHESTRATION'
    return 'CONTENT'


# alm#833's own unit and its own number, neither of them chosen here.
#
# THE UNIT. alm#833 measured the span-level bounds and found both useless alone: a span that
# is 100% CONTENT gives a 0.8% lower bound, because almost every real span ends with the
# agent narrating to its operator, so one closing REPORT turn makes a 20-turn investigation
# MIXED; and counting every scattered CONTENT turn gives a 72.3% upper bound that assumes
# isolated turns are each separately dispatchable. Its conclusion: "the real unit is a
# contiguous CONTENT run" -- 5,888 of them in its corpus, median 3 turns, p90 25, max 239.
#
# THE NUMBER. 3 is alm#833's break-even run length DERIVED IN DOLLARS, not picked: at a
# parent position of 5 turns on the measured opus-5 -> sonnet-5 mix, a handoff pays for
# itself at N >= 3, and 3 is the row it reports as its headline (71.3% of turns). It is
# overridable here, because break-even moves with the parent's accumulated position (N >= 2
# by P=50, N >= 1 by P=200) and a caller that knows the position can do better than a
# constant. What this module will not do is invent a different one.
MIN_CONTENT_RUN = 3

AGENT_GAP_NOTE = (
    "Counts main-thread turns since the last Agent/Task dispatch, and fires only if the "
    "window ALSO contains a contiguous run of at least %d CONTENT turns -- alm#833's own "
    "unit and its own dollar-derived break-even, not a constant chosen here. Both halves "
    "matter: a long gap during orchestration or narration is not a missed handoff, because "
    "those turns structurally cannot move. This is a PROXY over a proxy. alm#833 validated "
    "its three-class labelling at 94.8%% agreement against 120 hand labels and states "
    "plainly that it is biased; a fired flag here means 'this looks like work that could "
    "have moved', never 'this should have moved' -- dependency ordering between turns is "
    "not visible to it (alm#833 found 20 of 25 sampled spans dependency-ordered). In a "
    "window the gap is also a FLOOR on one side and a CEILING on the other: a dispatch "
    "older than the window is invisible, so `turns_since_dispatch` is reported as "
    "'at least' the window's own length when no dispatch is seen."
)


def agent_reuse_gap(win, threshold=None, min_content_run=MIN_CONTENT_RUN):
    """FLAG: a long stretch of delegable-looking main-thread work with no subagent dispatch.

    Fires when BOTH hold: the main thread has gone more than `threshold` turns since its
    last Agent/Task dispatch, and somewhere in the window there is a contiguous run of at
    least `min_content_run` CONTENT turns -- which is alm#833's measured unit of delegable
    work, cited above at MIN_CONTENT_RUN rather than reinvented.

    Sidechain turns are excluded from the gap, which is the point: a sidechain IS a subagent
    doing work, so counting its turns as "turns without a subagent" would invert the signal.

    The evidence shape here differs from the other two flags in one way worth knowing: this
    detector finds ONE condition, not a list, so `count` is 1 when it holds and 0 when it
    does not, and `threshold` is compared against `turns_since_dispatch` instead. That is
    stated rather than smoothed over, because a caller reading `count > threshold` for this
    flag would be reading the wrong pair of numbers.
    """
    w = win["window"]
    if _empty(w):
        return _flag("agent_reuse_gap", "unmeasured", [], threshold, w, SRC_ALM833,
                     AGENT_GAP_NOTE % min_content_run, measure_of=ARG_MEASURE,
                     reason="the window holds no assistant turn at all")

    main = [t for t in win["turns"] if not t["sidechain"]]
    if not main:
        return _flag("agent_reuse_gap", "unmeasured", [], threshold, w, SRC_ALM833,
                     AGENT_GAP_NOTE % min_content_run, measure_of=ARG_MEASURE,
                     reason="every turn in the window is a sidechain turn, so there is no "
                            "main thread whose delegation to measure",
                     extra=dict(main_turns=0, turns_since_dispatch=None,
                                dispatch_seen_in_window=False, longest_content_run=0,
                                class_counts={}))

    classes = [turn_class(t["tools"]) for t in main]
    counts = {}
    for c in classes:
        counts[c] = counts.get(c, 0) + 1

    # turns since the last dispatch. No dispatch in the window -> the whole window, and the
    # real gap is at least that: an older dispatch cannot be seen from here.
    last = max((i for i, c in enumerate(classes) if c == 'ALREADY_DELEGATED'), default=None)
    dispatch_seen = last is not None
    since_dispatch = (len(classes) - 1 - last) if dispatch_seen else len(classes)
    # "at least" is a claim about what the WINDOW cannot see, so it may only be made when
    # the window has a horizon. On a full read (or a window starting at byte 0 with nothing
    # trimmed) there is no older dispatch to have missed, and reporting the gap as a floor
    # there would hedge a number that is exact.
    gap_is_floor = (not dispatch_seen) and w["spans_are_local"]

    longest, cur = 0, 0
    for c in classes:
        cur = cur + 1 if c == 'CONTENT' else 0
        longest = max(longest, cur)

    delegable_looking = longest >= min_content_run
    over = threshold is not None and since_dispatch > threshold
    rows = []
    if over and delegable_looking:
        rows.append(dict(turns_since_dispatch=since_dispatch,
                         at_least=gap_is_floor,
                         longest_content_run=longest,
                         min_content_run=min_content_run,
                         main_turns=len(main),
                         class_counts=counts))

    state = "not_computed" if threshold is None else "measured"
    return _flag("agent_reuse_gap", state, rows, threshold, w, SRC_ALM833,
                 AGENT_GAP_NOTE % min_content_run,
                 measure=since_dispatch, measure_of=ARG_MEASURE,
                 gate=delegable_looking,
                 gate_reason=("the gap exceeds the threshold, but the window's longest "
                              "contiguous CONTENT run is %d turns, below the %d-turn "
                              "break-even alm#833 derived -- so there is no delegable-"
                              "looking work for the gap to be a gap IN"
                              % (longest, min_content_run)),
                 reason=("no threshold given, so the gap is reported and nothing is judged"
                         if state == "not_computed" else None),
                 extra=dict(main_turns=len(main),
                            turns_since_dispatch=since_dispatch,
                            dispatch_seen_in_window=dispatch_seen,
                            gap_is_floor=gap_is_floor,
                            longest_content_run=longest,
                            min_content_run=min_content_run,
                            delegable_looking=delegable_looking,
                            class_counts=counts))


# ==========================================================================================
# 4. ALL OF THEM AT ONCE
# ==========================================================================================

FLAGS = ("batchable_bash", "repeated_invocations", "agent_reuse_gap")


def all_flags(win, batchable_threshold=None, repeated_threshold=None,
              agent_gap_threshold=None, min_content_run=MIN_CONTENT_RUN):
    """Run every flag over one window and return them keyed by name.

    One parse, three detectors -- which is the other half of "cheap": a caller polling three
    flags should not read the file three times. `window` is returned once at the top as well
    as inside each flag, because a caller logging a single flag still needs its provenance.
    """
    out = {
        "batchable_bash": batchable_bash(win, batchable_threshold),
        "repeated_invocations": repeated_invocations(win, repeated_threshold),
        "agent_reuse_gap": agent_reuse_gap(win, agent_gap_threshold, min_content_run),
    }
    return dict(path=win["path"], window=win["window"], flags=out,
                fired=[k for k, v in out.items() if v["fired"]],
                unmeasured=[k for k, v in out.items() if v["state"] == "unmeasured"])


# ==========================================================================================
# 5. CLI
# ==========================================================================================

HELP_BLIND = """
WHAT WOULD THIS PRINT IF IT WERE BLIND?

This module's whole job is to be called by something that will act on its answer, so the
output that matters is not the one it prints when it can see. Every way it can fail to see,
and what it prints instead:

 1. THE FILE DOES NOT EXIST, or cannot be read.
    REFUSES, exit 2. Never an empty flag set: a wrong path and a quiet session produce the
    same zero, and only one of them is a fact about the session.

 2. THE FILE IS EMPTY, or the window caught no assistant turn.
    Every flag reports state "unmeasured" with a `reason`, and `fired` is False and means
    NOTHING. A caller that reads `fired: False` as healthy will read a dead session as a
    calm one -- which is why `unmeasured` is a separate state from a measured clean result
    and why `all_flags` returns the `unmeasured` list at the top.

 3. NO THRESHOLD WAS GIVEN.
    state "not_computed". The counts are real and are reported; `fired` is False and means
    nothing. No flag here ships a default threshold, and that is deliberate -- see
    docs/citations.md for why no published value is grounded enough to bake in.

 4. THE WINDOW HAS NO BASH PAIR / NO TOOL CALL AT ALL.
    "unmeasured" again, per flag, with the specific reason. Zero mergeable pairs out of zero
    pairs examined is not a clean result, and `pairs_examined` is reported so the
    denominator is visible.

 5. THE WINDOW CUT A RUN IN HALF.
    Reported, not hidden. `window.spans_are_local` is True whenever the window does not
    start at byte 0, and every count is then a FLOOR: a repeat run that began earlier is
    seen only from the window's start, a Bash pair whose first half is older is not seen at
    all, and a dispatch older than the window makes `turns_since_dispatch` an "at least"
    (the flag's own `at_least` field says which). A windowed span index is LOCAL and must
    not be compared with one from a full read.

 6. THE FILE WAS TRUNCATED OR REPLACED UNDER A RESUME CURSOR.
    `from_offset` past EOF sets `window.rotated` and falls back to a FULL read. The
    alternative -- seeking past the end and returning an empty window -- would report a
    rotated session as an idle one.

 7. THE SESSION HAS NOT MOVED.
    `from_offset` at EOF returns `grew: False` and an empty window, and every flag is
    "unmeasured". Nothing is inferred from a lack of new bytes.

 8. THE PROXIES ARE WRONG IN KNOWN WAYS.
    `batchable_bash` carries alm#831's hand-validated proxy, `agent_reuse_gap` carries
    alm#833's three-class labelling at 94.8% agreement against 120 hand labels and
    explicitly biased. Each flag's `note` states its own residual, and `source` names the
    commit the logic came from so the original's error rates can be read rather than
    guessed at. A flag fires on a SIGNATURE, never on a verdict about whether work was
    wasted.

 9. SOMETHING IT CANNOT SEE AT ALL.
    Whether a repeat was a legitimate poll, whether a merge would have been correct in
    context, whether a dependency between two turns forbade a handoff. These need state the
    transcript does not record. This module gives a caller somewhere to look; it does not
    decide, and a layer built on it that treats a fired flag as a finding has added a
    confidence the measurement does not carry.
"""


def _print_text(res, win):
    w = res["window"]
    print("FLAGS -- %s" % res["path"])
    print()
    print("WINDOW")
    print("  mode: %s   turns: %d   lines kept: %d of %d" %
          (w["mode"], w["turns"], w["lines_kept"], w["lines_total"]))
    print("  bytes read: %d of %d (%s of the file) in %d read(s)" %
          (w["bytes_read"], w["file_bytes"],
           ("%.2f%%" % (100 * w["read_fraction"])) if w["read_fraction"] is not None
           else "n/a", w["chunks"]))
    print("  offsets: start=%d  next_offset=%d   (pass next_offset back as --from-offset)"
          % (w["start_offset"], w["next_offset"]))
    if w["rotated"]:
        print("  ROTATED: the offset given is past EOF -- file truncated or replaced. Fell")
        print("           back to a FULL read rather than reporting an empty window.")
    if w["grew"] is False:
        print("  the file has not grown since that offset; every flag below is UNMEASURED.")
    if w["spans_are_local"]:
        print("  SPANS ARE LOCAL to this window, and every count below is a FLOOR. A run")
        print("  that began before the window is seen only from the window's start.")
    if not w["head_complete"]:
        print("  the OLDEST turn in this window may be a fragment (its first entries fell")
        print("  outside), so its own tool calls are under-counted.")
    print()
    for name in FLAGS:
        fl = res["flags"][name]
        print("%s" % name.upper().replace("_", " "))
        if fl["state"] != "measured":
            print("  %s: %s" % (fl["state"].upper(), fl["reason"]))
        elif fl["fired"]:
            print("  FIRED -- %s = %d, over the threshold of %d (%sx)"
                  % (fl["measure_of"], fl["measure"], fl["threshold"], fl["overshoot"]))
        elif "gate_reason" in fl:
            print("  not flagged -- %s" % fl["gate_reason"])
        else:
            print("  not flagged: %s = %d, threshold %d -- a MEASURED clean result"
                  % (fl["measure_of"], fl["measure"], fl["threshold"]))
        print("  measure: %d (%s)" % (fl["measure"], fl["measure_of"]))
        print("  evidence rows: %d   fingerprint: %s"
              % (len(fl["evidence"]), fl["fingerprint"] or "(none)"))
        if name == "batchable_bash" and "pairs_examined" in fl:
            print("  clean consecutive single-Bash pairs examined: %d  (%d had no recorded"
                  " output)" % (fl["pairs_examined"], fl["pairs_with_no_recorded_output"]))
        if name == "repeated_invocations" and "calls" in fl:
            print("  calls: %d  distinct signatures: %d  longest consecutive run: %d"
                  "  max within one span: %d"
                  % (fl["calls"], fl["distinct_call_keys"], fl["consecutive_longest"],
                     fl["in_span_max"]))
        if name == "agent_reuse_gap" and "main_turns" in fl:
            print("  main-thread turns: %d  turns since dispatch: %s%s  longest CONTENT"
                  " run: %d (break-even %d)"
                  % (fl["main_turns"],
                     "at least " if fl.get("gap_is_floor") else "",
                     fl["turns_since_dispatch"], fl["longest_content_run"],
                     fl["min_content_run"]))
            print("  turn classes: %s" % (fl["class_counts"] or "{}"))
        for row in fl["evidence"][:5]:
            print("    %s" % json.dumps(row, sort_keys=True))
        if len(fl["evidence"]) > 5:
            print("    ... and %d more (use --json for all)" % (len(fl["evidence"]) - 5))
        print("  source: %s" % fl["source"])
        print()
    print("FIRED: %s" % (", ".join(res["fired"]) or "(none)"))
    print("UNMEASURED (NOT a clean result): %s"
          % (", ".join(res["unmeasured"]) or "(none)"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Cheap, mechanical, no-model-call flags over a RECENT WINDOW of one "
                    "Claude Code transcript (read-only).",
        epilog="Pass at most one of --last-turns / --since / --from-offset; none reads the "
               "whole file.\nNo flag has a default threshold. Without one, a flag reports "
               "its count and says NOT COMPUTED.\nRun --help-blind for this module's own "
               "failure modes, and read the module docstring for the\ncalling contract "
               "(alm#839 is the caller this was built for).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript", nargs="?", help="a session .jsonl path")
    ap.add_argument("--last-turns", type=int, default=None, metavar="N",
                    help="window to the newest N logical assistant turns")
    ap.add_argument("--since", default=None, metavar="ISO8601",
                    help="window to entries stamped at or after this instant")
    ap.add_argument("--from-offset", type=int, default=None, metavar="BYTES",
                    help="window to everything appended since this byte offset -- the "
                         "resume cursor a polling caller keeps (see window.next_offset)")
    ap.add_argument("--flag-batchable-bash", type=int, default=None, metavar="N",
                    help="FLAG when MORE THAN N safely-mergeable consecutive Bash pairs "
                         "are in the window (strictly greater). NO DEFAULT")
    ap.add_argument("--flag-repeated-calls", type=int, default=None, metavar="N",
                    help="FLAG when MORE THAN N occurrences of one identical "
                         "tool+arguments signature are in the window. NO DEFAULT")
    ap.add_argument("--flag-agent-gap", type=int, default=None, metavar="N",
                    help="FLAG when the main thread has gone MORE THAN N turns since its "
                         "last Agent/Task dispatch AND the window holds a delegable-looking "
                         "CONTENT run. NO DEFAULT")
    ap.add_argument("--min-content-run", type=int, default=MIN_CONTENT_RUN, metavar="N",
                    help="what counts as a delegable-looking run of CONTENT turns "
                         "(default %d, alm#833's dollar-derived break-even -- not a "
                         "constant chosen here)" % MIN_CONTENT_RUN)
    ap.add_argument("--json", action="store_true",
                    help="emit the flag objects as JSON -- the shape an outside caller "
                         "consumes; identical to what all_flags() returns")
    ap.add_argument("--emit-fixture", metavar="NAME", default=None,
                    help="write one of the committed control fixtures to stdout and exit "
                         "(one of: %s). This is how samples/fixtures/flags.*.jsonl are "
                         "produced, so a sample and a control can never disagree about "
                         "what a fixture holds" % ", ".join(sorted(FIXTURES)))
    ap.add_argument("--self-test", action="store_true",
                    help="run the positive and negative controls and exit")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this module reports when it cannot see, and exit")
    args = ap.parse_args(argv)

    if args.help_blind:
        print(__doc__)
        print(HELP_BLIND)
        return 0
    if args.self_test:
        return self_test()
    if args.emit_fixture:
        if args.emit_fixture not in FIXTURES:
            print("REFUSING: no fixture named %r. Known: %s"
                  % (args.emit_fixture, ", ".join(sorted(FIXTURES))), file=sys.stderr)
            return 2
        for ln in FIXTURES[args.emit_fixture]():
            print(ln)
        return 0

    if not args.transcript:
        print("REFUSING: no transcript given. Pass a .jsonl path, or --self-test. "
              "See --help-blind.", file=sys.stderr)
        return 2
    if sum(x is not None for x in (args.last_turns, args.since, args.from_offset)) > 1:
        print("REFUSING: pass at most one of --last-turns / --since / --from-offset. "
              "Two windows is not a window.", file=sys.stderr)
        return 2
    if args.last_turns is not None and args.last_turns < 1:
        print("REFUSING: --last-turns %d is below 1. A window of zero turns would report "
              "every flag UNMEASURED, which is not a measurement of a calm session."
              % args.last_turns, file=sys.stderr)
        return 2
    for name, v in (("--flag-batchable-bash", args.flag_batchable_bash),
                    ("--flag-repeated-calls", args.flag_repeated_calls),
                    ("--flag-agent-gap", args.flag_agent_gap)):
        if v is not None and v < 1:
            print("REFUSING: %s %d is below 1. A threshold of 0 flags the absence of the "
                  "thing it is looking for." % (name, v), file=sys.stderr)
            return 2
    if args.min_content_run < 1:
        print("REFUSING: --min-content-run %d is below 1." % args.min_content_run,
              file=sys.stderr)
        return 2

    try:
        win = read_window(args.transcript, last_turns=args.last_turns, since=args.since,
                          from_offset=args.from_offset)
    except OSError as e:
        print("REFUSING: cannot read %s (%s). A wrong path and a quiet session produce the "
              "same zero, and only one of them is a fact. See --help-blind, blind mode 1."
              % (args.transcript, e.__class__.__name__), file=sys.stderr)
        return 2

    res = all_flags(win, args.flag_batchable_bash, args.flag_repeated_calls,
                    args.flag_agent_gap, args.min_content_run)
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True, default=str))
    else:
        _print_text(res, win)
    return 0


# ==========================================================================================
# 6. CONTROLS
# ==========================================================================================
#
# What these must prove, in order of how badly a defect in each would hurt:
#
#   1. A WINDOW AND A FULL READ AGREE on the turns they share. If they did not, every flag
#      would be measuring a different session from the batch report and nobody would know.
#   2. _read_from IS NOT OFF BY ONE, at every offset of a dense range, including offsets
#      that land exactly on a newline. This is the one piece of byte arithmetic here.
#   3. EVERY FLAG FIRES, and every flag does NOT fire on its own negative control. A
#      detector that has only been seen returning False is indistinguishable from one that
#      cannot return True -- the repository's own standing rule.
#   4. THE THRESHOLD BOUNDARY holds from both sides: == does not fire, +1 does.
#   5. THE THREE STATES are each reachable, and `unmeasured` never reports `fired`.
#   6. THE RESUME CURSOR composes: two windowed reads across an append see what one full
#      read of the final file sees.
#   7. NO RAW ARGUMENT leaks into an evidence row.

def _mk(mid, blocks, parent=None, uuid=None, sidechain=False, ts="2026-10-20T00:00:00Z"):
    return json.dumps(dict(type="assistant", uuid=uuid or ("u-" + mid), parentUuid=parent,
                           timestamp=ts, isSidechain=sidechain,
                           message=dict(id=mid, content=blocks,
                                        usage=dict(input_tokens=1, output_tokens=1,
                                                   cache_creation_input_tokens=0,
                                                   cache_read_input_tokens=10))))


def _tu(cid, name, inp):
    return dict(type="tool_use", id=cid, name=name, input=inp)


def _res(uuid, parent, cid, text, ts="2026-10-20T00:00:00Z"):
    return json.dumps(dict(type="user", uuid=uuid, parentUuid=parent, timestamp=ts,
                           message=dict(role="user", content=[
                               dict(type="tool_result", tool_use_id=cid, content=text)])))


def _usr(uuid, parent, text, ts="2026-10-20T00:00:00Z"):
    return json.dumps(dict(type="user", uuid=uuid, parentUuid=parent, timestamp=ts,
                           message=dict(role="user", content=text)))


def _bash_chain_lines(n, dependent, ts0=0):
    """A chain of n single-Bash turns, each a child of the previous turn's tool_result.

    `dependent=True` makes every B read a literal out of A's OUTPUT that is absent from A's
    own command -- alm#831's witness signature -- so the pairs are NOT mergeable. False
    makes each command self-contained and unlike its neighbour, so every pair IS mergeable.
    One generator, one flag flipped: a positive and a negative control that differ in the
    thing being measured and nothing else.

    THE FIRST VERSION OF THIS FIXTURE WAS BROKEN AND THE CONTROL CAUGHT IT, which is worth
    leaving a note about because it is the exact trap `batchable_bash` is built to avoid.
    Its `dependent=False` arm emitted the SAME command every turn, so the pairs were killed
    by alm#831's retry-similarity rule (jaccard 1.0 >= %.2f) rather than passing as
    mergeable -- a positive control that measured nothing while looking like it had. Both
    arms are now checked for the property they are supposed to have: the independent arm's
    neighbours share no specific token (jaccard 0) and the dependent arm's stay well under
    the retry threshold, so it is the WITNESS test that rejects them, not the retry test.
    """ % RETRY_JACCARD
    lines, parent = [], None
    for i in range(n):
        mid, cid = "m%d" % i, "c%d" % i
        if dependent and i:
            # `400+i` is in the PREVIOUS turn's output and in neither command: the witness.
            cmd = "sed -n %d,%dp /srv/report.txt" % (400 + i, 410 + i)
        elif dependent:
            cmd = "sed -n 400,410p /srv/report.txt"
        else:
            # self-contained, and no token shared with its neighbour
            cmd = "wc -l /srv/alpha%d.txt" % i
        ts = "2026-10-20T00:%02d:00Z" % (ts0 + i)
        lines.append(_mk(mid, [_tu(cid, "Bash", dict(command=cmd))], parent=parent,
                         uuid="a%d" % i, ts=ts))
        # The dependent arm's output NAMES the literal the next command will use, and
        # nothing else specific. The independent arm's output names nothing at all.
        out = ("match at line %d\n" % (400 + i + 1)) if dependent else "%d\n" % (17 + i)
        lines.append(_res("r%d" % i, "a%d" % i, cid, out, ts=ts))
        parent = "r%d" % i
    return lines


def _repeat_lines(n):
    """n turns all making the IDENTICAL tool call -- signal 2's positive control."""
    lines, parent = [], None
    for i in range(n):
        lines.append(_mk("rm%d" % i, [_tu("rc%d" % i, "Read",
                                          dict(file_path="/tmp/zorkmid-fixture"))],
                         parent=parent, uuid="ra%d" % i))
        lines.append(_res("rr%d" % i, "ra%d" % i, "rc%d" % i, "ok"))
        parent = "rr%d" % i
    return lines


def _content_run_lines(n, dispatch_at=None):
    """n main-thread CONTENT turns, optionally with an Agent dispatch at index `dispatch_at`."""
    lines, parent = [], None
    for i in range(n):
        if dispatch_at is not None and i == dispatch_at:
            blocks = [_tu("dc%d" % i, "Agent", dict(prompt="go"))]
        else:
            blocks = [_tu("gc%d" % i, "Bash", dict(command="grep -rn zorkmid%d ." % i))]
        lines.append(_mk("gm%d" % i, blocks, parent=parent, uuid="ga%d" % i))
        lines.append(_res("gr%d" % i, "ga%d" % i, blocks[0]["id"], "ok"))
        parent = "gr%d" % i
    return lines


def _orch_lines(n):
    """n main-thread ORCHESTRATION turns -- agent_reuse_gap's negative control. The gap is
    just as long; the work structurally cannot move, so the flag must stay silent.

    The arguments VARY per turn on purpose. An earlier version sent the identical message
    every time, which made this fixture fire `repeated_invocations` as well -- true, but it
    muddied a sample whose whole job is to isolate one flag's negative case. A control that
    also trips a different detector is a control a reader has to untangle.
    """
    lines, parent = [], None
    for i in range(n):
        blocks = [_tu("oc%d" % i, "mcp__fleet-control__send_message",
                      dict(message="status update %d" % i))]
        lines.append(_mk("om%d" % i, blocks, parent=parent, uuid="oa%d" % i))
        lines.append(_res("or%d" % i, "oa%d" % i, "oc%d" % i, "ok"))
        parent = "or%d" % i
    return lines


def _dep_pair_diagnostics(path):
    """(jaccard, witness count) for every clean single-Bash pair in `path`.

    Only the controls use this. It exists so the dependent fixture can be checked for the
    property it is SUPPOSED to have -- rejected by the witness test, with similarity below
    the retry threshold -- rather than merely observed to produce zero mergeable pairs,
    which a fixture that is wrong in some other way also does.
    """
    import collections
    win = read_window(path)
    _, nodes, children, turns = load_lines(win["lines"])
    res = collections.defaultdict(list)
    for u, r in nodes.items():
        if r.get('type') != 'user':
            continue
        q = r.get('parentUuid')
        while q in nodes and nodes[q].get('type') == 'user':
            q = nodes[q].get('parentUuid')
        if q in nodes and nodes[q].get('type') == 'assistant':
            res[nodes[q].get('_mid')].append(result_text(r))
    out, seen = [], set()
    for m, t in turns.items():
        if not t['tools']:
            continue
        nxts, barrier = next_tool_turn(t['rows'][-1], m, nodes, children, turns)
        if barrier:
            continue
        for nx in nxts:
            if (m, nx) in seen or nx not in turns:
                continue
            seen.add((m, nx))
            a, b = turns[m], turns[nx]
            if not (len(a['bash']) == 1 and len(b['bash']) == 1
                    and len(a['tools']) == 1 and len(b['tools']) == 1):
                continue
            a_out = '\n'.join(res.get(m, []))
            out.append((jaccard(a['bash'][0], b['bash'][0]),
                        len(witnesses(a['bash'][0], a_out, b['bash'][0]))))
    return out


def _write(tmp, name, lines):
    p = os.path.join(tmp, name)
    with open(p, "w") as fh:
        for ln in lines:
            fh.write(ln + "\n")
    return p


# The committed fixtures under samples/fixtures/flags.*.jsonl are the output of these
# generators -- the SAME ones the controls above run on, which is the point: a published
# sample and a self-test control that disagree about what a fixture contains would make
# both of them worthless. `--emit-fixture NAME` writes one to stdout, so a fixture can be
# regenerated by hand and `scripts/regen_samples.sh` can rebuild every sample from it:
#
#     python3 instruments/flags.py --emit-fixture batchable > samples/fixtures/flags.batchable.jsonl
#
# Each name below is one state worth pinning to an artefact. The dangerous output of this
# module is not a zero, it is a reassuring "nothing flagged", so every state a reader must
# be able to tell APART from that -- fired, measured-clean, not-computed, unmeasured -- has
# its own fixture and its own sample.
FIXTURES = {
    # batchable_bash FIRES: 8 self-contained Bash turns, no dependency witness anywhere
    "batchable": lambda: _bash_chain_lines(8, dependent=False),
    # and its negative: the same chain with alm#831's witness signature in every pair
    "dependent": lambda: _bash_chain_lines(8, dependent=True),
    # repeated_invocations FIRES: 9 identical Read calls
    "repeats": lambda: _repeat_lines(9),
    # agent_reuse_gap FIRES: 12 CONTENT turns, no Agent/Task dispatch
    "content": lambda: _content_run_lines(12),
    # and its negative, the one that matters: the SAME 12-turn gap, all orchestration
    "orchestration": lambda: _orch_lines(12),
    # every flag UNMEASURED: one assistant turn that called no tool at all
    "no-tool": lambda: [_mk("t1", [dict(type="text", text="thinking out loud")])],
}


def self_test():
    import tempfile
    ok = fail = 0

    def check(cond, what):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ok   -- %s" % what)
        else:
            fail += 1
            print("  FAIL -- %s" % what)

    print("FLAGS SELF-TEST")
    tmp = tempfile.mkdtemp(prefix="ari-flags-")

    # ---- control 2: the byte arithmetic, first, because everything rests on it ----------
    print()
    print("_read_from: no line is lost or duplicated at ANY offset")
    p = _write(tmp, "arith.jsonl", _repeat_lines(40))
    size = os.path.getsize(p)
    with open(p, "rb") as fh:
        whole = fh.read()
    all_lines = whole.decode().splitlines()
    # every line paired with its own start offset, computed independently of _read_from
    pairs, acc = [], 0
    for ln in all_lines:
        pairs.append((acc, ln))
        acc += len(ln.encode()) + 1
    starts = {s for s, _ in pairs}
    bad_any, bad_boundary, bad_offset = 0, 0, 0
    for off in range(0, size + 1):
        raw, start = _read_from(p, off, size)
        got = raw.decode().splitlines()
        want = [ln for s, ln in pairs if s >= off]
        if got != want:
            bad_any += 1
            if off in starts:
                bad_boundary += 1
        # the returned offset must be the real start of the first line returned
        exp_start = next((s for s, _ in pairs if s >= off), size)
        if start != exp_start:
            bad_offset += 1
    check(bad_any == 0,
          "every offset 0..%d returns exactly the lines starting at or after it "
          "(%d wrong)" % (size, bad_any))
    check(bad_boundary == 0,
          "offsets landing EXACTLY on a line boundary (%d of them) lose no line"
          % len(starts))
    check(bad_offset == 0,
          "the start offset returned is the true offset of the first line returned, at "
          "every one of %d offsets (%d wrong) -- so a resume cursor cannot drift"
          % (size + 1, bad_offset))

    # ---- control 1: a window agrees with a full read -------------------------------------
    print()
    print("a window and a full read agree on the turns they share")
    p = _write(tmp, "agree.jsonl", _repeat_lines(60))
    full = read_window(p)
    for n in (1, 2, 5, 17, 59, 60, 61, 1000):
        w = read_window(p, last_turns=n, first_chunk=1024)
        exp = min(n, len(full["turns"]))
        same = (w["window"]["turns"] == exp)
        keys = [t["call_keys"] for t in w["turns"]]
        tail = [t["call_keys"] for t in full["turns"]][-exp:]
        check(same and keys == tail,
              "last_turns=%d -> %d turns, call-for-call identical to the full read's tail"
              % (n, w["window"]["turns"]))
    check(full["window"]["windowed"] is False and full["window"]["read_fraction"] == 1.0,
          "the no-argument read is the FULL read and labels itself as one")

    # a tiny first_chunk forces the growth loop; the answer must not depend on it
    w_small = read_window(p, last_turns=20, first_chunk=64)
    w_big = read_window(p, last_turns=20, first_chunk=4 * 1024 * 1024)
    check([t["call_keys"] for t in w_small["turns"]]
          == [t["call_keys"] for t in w_big["turns"]],
          "the answer is independent of first_chunk (%d reads vs %d) -- only the cost moves"
          % (w_small["window"]["chunks"], w_big["window"]["chunks"]))
    check(w_small["window"]["chunks"] > 1 and w_big["window"]["chunks"] == 1,
          "a small first_chunk really did force the growth loop (%d reads), a large one "
          "did not (%d)" % (w_small["window"]["chunks"], w_big["window"]["chunks"]))
    check(read_window(p, last_turns=5, first_chunk=1024)["window"]["bytes_read"]
          < full["window"]["bytes_read"],
          "a 5-turn window reads FEWER bytes than the full file (%d < %d) -- the cost "
          "claim, in the control rather than only in the prose"
          % (read_window(p, last_turns=5, first_chunk=1024)["window"]["bytes_read"],
             full["window"]["bytes_read"]))

    # ---- control 6: the resume cursor composes across an append --------------------------
    print()
    print("the resume cursor: two windowed reads across an append == one full read")
    p2 = _write(tmp, "grow.jsonl", _repeat_lines(10))
    first = read_window(p2, from_offset=0)
    cur = first["window"]["next_offset"]
    idle = read_window(p2, from_offset=cur)
    check(idle["window"]["grew"] is False and idle["window"]["turns"] == 0,
          "an unchanged file returns grew=False and an empty window")
    check(all(all_flags(idle)["flags"][f]["state"] == "unmeasured" for f in FLAGS),
          "every flag on that empty window is UNMEASURED, not a clean result")
    with open(p2, "a") as fh:
        for ln in _repeat_lines(4):
            fh.write(ln.replace('"rm', '"zm').replace('"ra', '"za')
                       .replace('"rc', '"zc').replace('"rr', '"zr') + "\n")
    second = read_window(p2, from_offset=cur)
    check(second["window"]["grew"] is True and second["window"]["turns"] == 4,
          "after appending 4 turns the resumed read sees exactly those 4 (saw %d)"
          % second["window"]["turns"])
    check(second["window"]["bytes_read"] < os.path.getsize(p2),
          "and it read %d bytes, not the whole %d-byte file"
          % (second["window"]["bytes_read"], os.path.getsize(p2)))
    check(len(read_window(p2)["turns"]) == 14,
          "a full read of the grown file sees all 14 turns, so nothing was lost at the seam")

    # rotation
    p3 = _write(tmp, "rot.jsonl", _repeat_lines(3))
    rot = read_window(p3, from_offset=10 ** 9)
    check(rot["window"]["rotated"] is True and rot["window"]["turns"] == 3,
          "an offset past EOF sets rotated=True and falls back to a FULL read, rather "
          "than reporting an empty window for a replaced file")

    # ---- control: `since` ----------------------------------------------------------------
    print()
    print("the `since` window")
    p4 = _write(tmp, "since.jsonl", _bash_chain_lines(12, dependent=False))
    s = read_window(p4, since="2026-10-20T00:08:00Z", first_chunk=256)
    check(s["window"]["turns"] == 4,
          "since=00:08 over turns stamped 00:00..00:11 keeps 4 turns (kept %d)"
          % s["window"]["turns"])
    s_all = read_window(p4, since="2020-01-01T00:00:00Z", first_chunk=256)
    check(s_all["window"]["turns"] == 12,
          "a `since` older than the file keeps every turn (kept %d)"
          % s_all["window"]["turns"])

    # ---- control 3+4: every flag fires, and does not fire on its negative control --------
    print()
    print("batchable_bash: fires on mergeable pairs, silent on dependent ones")
    p_ind = _write(tmp, "indep.jsonl", _bash_chain_lines(8, dependent=False))
    p_dep = _write(tmp, "dep.jsonl", _bash_chain_lines(8, dependent=True))
    f_ind = batchable_bash(read_window(p_ind), threshold=2)
    f_dep = batchable_bash(read_window(p_dep), threshold=2)
    check(f_ind["state"] == "measured" and f_ind["fired"] and f_ind["count"] > 2,
          "POSITIVE: 8 self-contained Bash turns -> %d mergeable pairs of %d examined, "
          "FIRED" % (f_ind["count"], f_ind["pairs_examined"]))
    check(f_dep["pairs_examined"] > 0 and f_dep["count"] == 0 and not f_dep["fired"],
          "NEGATIVE: the same chain with alm#831's witness signature -> %d of %d pairs "
          "mergeable, not fired" % (f_dep["count"], f_dep["pairs_examined"]))
    n = f_ind["measure"]
    check(not batchable_bash(read_window(p_ind), threshold=n)["fired"]
          and batchable_bash(read_window(p_ind), threshold=n - 1)["fired"],
          "BOUNDARY: measure %d does not fire at threshold %d and does at %d (strictly "
          "greater, same rule as compute_flags)" % (n, n, n - 1))
    # The fixture must fail for the RIGHT reason -- see _bash_chain_lines on the version of
    # it that did not. Asserted here rather than trusted.
    ind_j = max(r["jaccard"] for r in f_ind["evidence"])
    check(ind_j < RETRY_JACCARD,
          "the independent arm's pairs are accepted on their own merits: highest "
          "command-similarity %.2f, under the %.2f retry threshold, so they were not "
          "merely not-retries" % (ind_j, RETRY_JACCARD))
    dep_rows = _dep_pair_diagnostics(p_dep)
    check(dep_rows and all(j < RETRY_JACCARD and nw > 0 for j, nw in dep_rows),
          "and the dependent arm is rejected by the WITNESS test, not the retry test: all "
          "%d pairs have similarity < %.2f AND at least one witness"
          % (len(dep_rows), RETRY_JACCARD))
    check(batchable_bash(read_window(p_ind))["state"] == "not_computed",
          "no threshold -> NOT COMPUTED, with the count still reported")
    p_noB = _write(tmp, "nobash.jsonl", _repeat_lines(5))
    check(batchable_bash(read_window(p_noB), threshold=1)["state"] == "unmeasured",
          "a window with no Bash pair at all -> UNMEASURED, not a clean zero")
    leaked = [r for r in f_ind["evidence"]
              if any("zorkmid" in str(v) or "/srv/" in str(v) for v in r.values())]
    check(not leaked,
          "control 7: no raw command text reached an evidence row (%d leaks)" % len(leaked))

    print()
    print("repeated_invocations: fires on a run, silent on distinct calls")
    p_rep = _write(tmp, "rep.jsonl", _repeat_lines(9))
    p_var = _write(tmp, "var.jsonl", _bash_chain_lines(9, dependent=True))
    f_rep = repeated_invocations(read_window(p_rep), threshold=4)
    f_var = repeated_invocations(read_window(p_var), threshold=4)
    check(f_rep["fired"] and f_rep["consecutive_longest"] == 9 and f_rep["measure"] == 9,
          "POSITIVE: 9 identical Read calls -> consecutive run of %d, measure %d, FIRED"
          % (f_rep["consecutive_longest"], f_rep["measure"]))
    check(f_var["state"] == "measured" and not f_var["fired"] and f_var["count"] == 0,
          "NEGATIVE: 9 all-different Bash calls -> %d repeat rows, a MEASURED clean result"
          % f_var["count"])
    check(not repeated_invocations(read_window(p_rep), threshold=9)["fired"]
          and repeated_invocations(read_window(p_rep), threshold=8)["fired"],
          "BOUNDARY: a run of 9 does not fire at threshold 9 and does at 8 -- and the "
          "threshold is compared against the RUN LENGTH, which is what signal 2 means by "
          "it, not against the number of groups")
    # the window really does narrow it: 4 turns cannot hold a run of 9
    f_w = repeated_invocations(read_window(p_rep, last_turns=4, first_chunk=256),
                              threshold=4)
    check(f_w["consecutive_longest"] == 4,
          "a 4-turn window sees a run of 4, not 9 -- the floor the note promises, measured")
    p_empty = _write(tmp, "empty.jsonl", [])
    check(repeated_invocations(read_window(p_empty), threshold=1)["state"] == "unmeasured",
          "an empty file -> UNMEASURED and fired=False, which means NOTHING")
    p_text = _write(tmp, "text.jsonl", [_mk("t1", [dict(type="text", text="hello")])])
    f_t = repeated_invocations(read_window(p_text), threshold=1)
    check(f_t["state"] == "unmeasured" and not f_t["fired"],
          "a turn that called no tool -> UNMEASURED ('nothing to look at', not 'no repeats')")

    print()
    print("agent_reuse_gap: fires on delegable content, silent on orchestration")
    p_cont = _write(tmp, "cont.jsonl", _content_run_lines(12))
    p_orch = _write(tmp, "orch.jsonl", _orch_lines(12))
    p_disp = _write(tmp, "disp.jsonl", _content_run_lines(12, dispatch_at=10))
    g_cont = agent_reuse_gap(read_window(p_cont), threshold=5)
    g_orch = agent_reuse_gap(read_window(p_orch), threshold=5)
    g_disp = agent_reuse_gap(read_window(p_disp), threshold=5)
    check(g_cont["fired"] and g_cont["longest_content_run"] == 12
          and not g_cont["dispatch_seen_in_window"],
          "POSITIVE: 12 CONTENT turns, no dispatch -> gap at least %d, longest run %d, "
          "FIRED" % (g_cont["turns_since_dispatch"], g_cont["longest_content_run"]))
    check(not g_orch["fired"] and g_orch["turns_since_dispatch"] == 12
          and g_orch["longest_content_run"] == 0,
          "NEGATIVE (the one that matters): 12 ORCHESTRATION turns -- the SAME %d-turn gap "
          "-- not fired, because alm#833 says those turns structurally cannot move"
          % g_orch["turns_since_dispatch"])
    check(not g_disp["fired"] and g_disp["dispatch_seen_in_window"]
          and g_disp["turns_since_dispatch"] == 1,
          "NEGATIVE: a dispatch at turn 10 of 12 -> gap of %d, not fired"
          % g_disp["turns_since_dispatch"])
    g = agent_reuse_gap(read_window(p_cont), threshold=12)
    g1 = agent_reuse_gap(read_window(p_cont), threshold=11)
    check(not g["fired"] and g1["fired"],
          "BOUNDARY: a gap of 12 does not fire at threshold 12 and does at 11")
    short = agent_reuse_gap(read_window(p_cont), threshold=5, min_content_run=99)
    check(not short["fired"] and short["delegable_looking"] is False,
          "raising min_content_run to 99 makes the same window not delegable-looking, so "
          "the long gap alone does not fire -- both halves are load-bearing")
    check(agent_reuse_gap(read_window(p_cont))["state"] == "not_computed",
          "no threshold -> NOT COMPUTED, with the gap still reported")
    # The gap is a FLOOR only when the window has a horizon that could hide an older
    # dispatch. On a full read there is none, and hedging an exact number would be its own
    # small dishonesty.
    full_gap = agent_reuse_gap(read_window(p_cont), threshold=5)
    win_gap = agent_reuse_gap(read_window(p_cont, last_turns=6, first_chunk=256),
                              threshold=2)
    check(full_gap["gap_is_floor"] is False and win_gap["gap_is_floor"] is True,
          "a FULL read reports its gap exactly (%d), a WINDOWED read reports it as a floor "
          "(at least %d) -- because only the latter can be hiding an older dispatch"
          % (full_gap["turns_since_dispatch"], win_gap["turns_since_dispatch"]))

    # ---- control: the generalised MCP prefix rule agrees with alm#833 where it must ------
    print()
    print("tool_class: the generalised prefix rule vs alm#833's hard-coded one")

    def alm833_tool_class(name, prefixes):
        """alm#833 census_c_delegability.py::tool_class, VERBATIM, prefix list injected.

        Kept here as the oracle rather than as a comment. The claim being tested is that
        replacing its two-literal prefix loop with `_strip_mcp` changes no answer for the
        servers it knew -- and a claim about two implementations agreeing needs both
        implementations present, not one of them paraphrased in prose.
        """
        if name in DISPATCH_TOOLS:
            return 'ALREADY_DELEGATED'
        short = name
        for pp in prefixes:
            if name.startswith(pp):
                short = name[len(pp):]
                break
        if short in ORCH_TOOLS:
            return 'ORCHESTRATION'
        if name.startswith('mcp__'):
            if (short.startswith('github_') or short.startswith('list_')
                    or short.startswith('get_')):
                return 'CONTENT'
            return 'ORCHESTRATION'
        if name in CONTENT_HINT:
            return 'CONTENT'
        return 'CONTENT'

    # The two prefixes alm#833 hard-coded, assembled from parts so this public file does
    # not carry the private service names as literals -- the same reason _strip_mcp exists.
    known = tuple("mcp__%s__" % s for s in ("harness" + "-control-mcp-server",
                                            "cao" + "-mcp-server"))
    vocab = ["send_message", "spawn_child_session", "kill_session", "orient",
             "edit_session_metadata", "list_sessions", "get_session_state",
             "github_comment_on_issue", "github_create_issue", "github_get_issue",
             "github_list_issues", "list_folders",
             "get_tree_metadata", "switch_session_account", "archive_folder"]
    names = ([k + v for k in known for v in vocab]
             + ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "Agent", "Task",
                "WebFetch", "TodoWrite", "SomeToolNobodyListed"])
    disagree = [n for n in names if tool_class(n) != alm833_tool_class(n, known)]
    check(not disagree,
          "on the %d tool names alm#833's own prefix list covered, the generalised rule "
          "gives an IDENTICAL answer (%d disagreements)" % (len(names), len(disagree)))
    # Not identical by collapsing everything into one class. Note WHICH github verb is
    # which: alm#833 lists `github_comment_on_issue` as ORCHESTRATION on purpose -- posting
    # a verdict happens under the parent's own identity and a subagent cannot do it for it
    # -- while a read like `github_get_issue` is ordinary investigation. Asserting the
    # wrong one of those pair is how this control first failed.
    check(tool_class(known[0] + "send_message") == 'ORCHESTRATION'
          and tool_class(known[0] + "github_comment_on_issue") == 'ORCHESTRATION'
          and tool_class(known[0] + "github_get_issue") == 'CONTENT'
          and tool_class(known[0] + "get_session_state") == 'ORCHESTRATION',
          "and it is not identical by collapsing into one class: the same server's "
          "send_message and github_comment_on_issue are ORCHESTRATION, github_get_issue "
          "is CONTENT, and get_session_state is ORCHESTRATION despite the get_ prefix")

    # the documented divergence, demonstrated rather than asserted in a comment
    third = "mcp__some-other-server__get_thing"
    check(tool_class(third) == 'CONTENT'
          and alm833_tool_class(third, known) == 'ORCHESTRATION',
          "THE ONE DIVERGENCE, reproduced: %r is CONTENT here and ORCHESTRATION upstream, "
          "because upstream could not strip an unknown server's prefix and so tested its "
          "get_/list_/github_ rule against the whole name" % third)
    check(tool_class("mcp__some-other-server__send_message") == 'ORCHESTRATION',
          "an unknown server's fleet-control verb is still ORCHESTRATION, so the "
          "generalisation did not open a hole in the class that matters")
    check(tool_class("Bash", orch_tools={"Bash"}) == 'ORCHESTRATION'
          and tool_class("Bash") == 'CONTENT',
          "the verb vocabulary is a parameter: a deployment can replace it without "
          "editing this file")

    # ---- control 5: the three states, and unmeasured never fires -------------------------
    print()
    print("the three states")
    seen = set()
    for w, thr in ((read_window(p_cont), 5), (read_window(p_cont), None),
                   (read_window(p_empty), 5)):
        for fl in all_flags(w, thr, thr, thr)["flags"].values():
            seen.add(fl["state"])
            if fl["state"] != "measured":
                check(fl["fired"] is False,
                      "%s in state %s reports fired=False" % (fl["flag"], fl["state"]))
    check(seen == set(STATES),
          "all three states are reachable from real windows: %s" % sorted(seen))
    shapes = {tuple(sorted(set(all_flags(read_window(p_cont), 5, 5, 5)["flags"][f]) -
                           {"pairs_examined", "pairs_with_no_recorded_output", "calls",
                            "distinct_call_keys", "consecutive_longest", "in_span_max",
                            "main_turns", "turns_since_dispatch", "dispatch_seen_in_window",
                            "gap_is_floor", "longest_content_run", "min_content_run",
                            "delegable_looking", "class_counts", "reason", "gate_reason"}))
              for f in FLAGS}
    check(len(shapes) == 1,
          "every flag returns the SAME core key set, so one caller branch works for all "
          "three: %s" % (list(shapes)[0],))

    print()
    if fail == 0:
        print("SELF-TEST PASSED -- %d checks: the byte arithmetic at every offset, "
              "window/full agreement," % ok)
        print("a positive AND a negative control for each of the three flags, both sides of "
              "each threshold")
        print("boundary, all three states reachable, the resume cursor across an append and "
              "a rotation,")
        print("and no raw argument in any evidence row.")
        return 0
    print("SELF-TEST FAILED -- %d of %d checks" % (fail, ok + fail), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
