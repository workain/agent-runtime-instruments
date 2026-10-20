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
# FIRST_CHUNK IS MEASURED, NOT GUESSED -- and the measurement moved once under it, which is
# the more useful thing to record. At 256 KB, every transcript in the local corpus needed a
# growth step, so the cost of a window was really the cost of a bad first guess; the sweep
# (`bench_window_read.py --sweep-first-chunk`) put the minimum at 1 MB, which is this value.
#
# Then the growth loop was changed to EXTEND its buffer backwards rather than re-read from a
# new offset (see read_window), and that flattened the curve the choice had been made on. On
# the current corpus 1 MB reads 10.00 MB in 10 reads -- one per file, no growth step -- while
# 128 KB reads 9.50 MB in 23 reads. So a smaller first chunk is now marginally cheaper in
# BYTES and markedly more expensive in SYSCALLS, and the 5% byte difference is inside the
# run-to-run movement of the corpus itself. 1 MB is kept for the property that is stable:
# one read per file.
#
# Re-run the sweep rather than inheriting either number. The optimum is a property of how
# many bytes a turn takes in a given corpus, which is why `first_chunk` is a per-call
# parameter -- and `self_test` asserts the ANSWER never depends on it, only the cost, by
# comparing a 64-byte first chunk against a 4 MB one.
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


def _split_raw(raw, base):
    """(lines, absolute byte offset of each line) from raw bytes.

    TWO DEFECTS THIS REPLACES, both found by review, both in the same mistake: deriving a
    byte position from DECODED text.

      1. The offset of the first kept line was computed as
         `sum(len(line.encode()) + 1 for line in dropped)`. `decode(..., "replace")` is not
         length-preserving -- one invalid byte becomes U+FFFD, which re-encodes to THREE
         bytes -- so a single 0xff in a dropped line pushed `start_offset` two bytes past
         the line it names, far enough that `_read_from` skipped it and the window's oldest
         turn was silently lost on resume. The `+ 1` was wrong in the other direction on
         CRLF, where `splitlines()` consumes two bytes: measured -197 over 200 short lines.
         Offsets are now READ OFF the raw bytes, so there is no arithmetic to be wrong.

      2. `str.splitlines()` splits on more than `\n` -- `\r`, `\v`, `\f`, U+2028, U+2029.
         JSONL is newline-delimited and those characters occur INSIDE JSON strings, so
         splitlines() could cut one valid line into two invalid halves. Splitting the raw
         bytes on b"\n" is both the correct reading of the format and the only split whose
         offsets are knowable. A trailing `\r` is left on the line and neither
         `json.loads` nor `collect_turns`' own `.strip()` minds it.
    """
    lines, offsets = [], []
    pos = 0
    parts = raw.split(b"\n")
    if parts and parts[-1] == b"" and raw.endswith(b"\n"):
        parts.pop()                      # the empty tail a trailing newline leaves behind
    for chunk in parts:
        lines.append(chunk.decode("utf-8", "replace"))
        offsets.append(base + pos)
        pos += len(chunk) + 1
    return lines, offsets


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


def _turn_occurrences(lines):
    """[(first line index, turn id)] -- one entry per maximal CONSECUTIVE run of assistant
    entries sharing a `message.id`.

    POSITIONAL, and that is the whole point. The previous version collected distinct ids in
    order of FIRST appearance and then cut at the first line carrying any kept id -- which
    on a resumed transcript, where the same `message.id` is replayed much later, jumped the
    cut back to the file's beginning. Measured consequences, both of them contract
    violations rather than slightly-off answers: `last_turns=400` returned 437 turns, and
    `read_fraction` came back as 1.659 -- a "window" that read 1.66x the whole file, in the
    very statistic the cost claim is made in -- while still reporting `head_complete: True`.

    Grouping by consecutive run instead makes the cut a position near the end of what was
    read, so `last_turns=N` can no longer return more than N turn-occurrences and
    `read_fraction` can no longer exceed 1. It does not make a replayed id vanish: if two
    occurrences of one id both land in the window, `collect_turns` still merges them and the
    window holds FEWER than N turns. So the contract is "at most N", which is what
    read_window's docstring now says, and the residual is bounded rather than unbounded.
    """
    occ = []
    for i, mid in _assistant_mids(lines):
        if occ and occ[-1][1] == mid:
            continue
        occ.append((i, mid))
    return occ


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
        start_offset      byte offset of the first line KEPT -- i.e. where this window's
                          CONTENT begins. Passing it back as `from_offset` reproduces this
                          same window. It is NOT where the read began; see read_offset
        read_offset       byte offset where the read actually began, which is earlier
                          whenever the trim discarded lines. This is the number the cost
                          figures are about, and it is the WRONG one to resume from
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

    DISCLOSED RESIDUAL: A RESUMED TRANSCRIPT CAN REPEAT A `message.id`, and then a window
    and a full read legitimately disagree about the turns they appear to share.
    `collect_turns` keys a logical turn by `message.id` and MERGES every entry carrying
    that id -- right for the split turn it was built for, where the entries are adjacent. A
    resumed or branched session can replay the same API response ids much later in the same
    file: measured 769 entries apart on a real local transcript. The full read then
    collapses both occurrences into ONE turn sitting at the earlier position, so its tail is
    not the window's content -- and in that comparison the full read is the one doing
    something strange.

    WHAT THE BOUND ACTUALLY IS, which matters more than the rate -- and the rate alone was
    how an earlier draft of this paragraph understated it. Two contract violations existed
    and are now gone, each verified over 151 windowing comparisons on 45 real transcripts at
    eight window sizes (1 to 400):

      * `last_turns=N` RETURNED MORE THAN N TURNS. The cut was chosen by turn IDENTITY, so a
        replayed id in the kept set also appeared near the file's start and dragged the cut
        back there: `last_turns=400` returned 437 turns. `_turn_occurrences` makes the cut
        POSITIONAL. Now 0 over-returns, from any window size, on any file tested.
      * `read_fraction` EXCEEDED 1 -- up to 1.934, a "window" charging for almost twice the
        file. Partly the same cause and partly the growth loop re-reading from each new
        offset. The loop now extends its buffer backwards instead. Now 0 cases above 1.0,
        maximum observed exactly 1.000.

    WHAT REMAINS is bounded, and is only this: when two occurrences of one replayed id both
    land inside the window, `collect_turns` merges them, so the window holds FEWER turns than
    asked for and its contents are not a suffix of the full read's turn list. 4 of 151
    comparisons, confined to the resumed files. That is a rate for "the two reads do not line
    up", NOT for "the window is wrong": the window's own counts are internally consistent,
    and they are what every flag is computed from. The resume cursor round-trips exactly --
    0 disagreements over 77 round-trips on the same corpus.

    Not worked around, deliberately: the key belongs to `collect_turns`, and changing it
    there would modify the file ARI#24's backward-compatibility criterion requires to stay
    untouched. `self_test` reproduces the disagreement from a deliberate fixture, so the
    behaviour is pinned rather than merely admitted.

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
        lines, offsets = _split_raw(raw, start)
        win["lines_total"] = len(lines)
        kept, cut = lines, 0
    # ---- mode: full. Byte 0 to EOF -- the batch path, unchanged in behaviour. ----
    elif not any(given):
        with open(path, "rb") as fh:
            raw = fh.read()
        win["chunks"] = 1
        win["bytes_read"] = len(raw)
        kept, offsets = _split_raw(raw, 0)
        lines, cut = kept, 0
        win["lines_total"] = len(kept)
    # ---- modes: last_turns / since. Tail read, grown until the window is satisfied. ----
    else:
        win["mode"] = "last_turns" if last_turns is not None else "since"
        win["windowed"] = True
        # THE BUFFER IS EXTENDED BACKWARDS, NOT RE-READ. The first version re-read from a
        # new, larger offset each time the window was not yet satisfied, so the bytes it
        # charged for could exceed the file: measured up to read_fraction 1.934 on a real
        # transcript (1 MB first read, then the whole 1.12 MB file). Every one of those
        # returned the right turns, so it was honest accounting of a real cost rather than a
        # wrong answer -- but a "window" that reads 1.9x the file undercuts the entire reason
        # this read path exists, and the fix is to keep what was already read and fetch only
        # the part in front of it. `bytes_read` can now exceed the file only by the one-byte
        # line-boundary probe per growth step, and `read_fraction <= 1` holds in practice.
        chunk = first_chunk or FIRST_CHUNK
        total_read = 0
        chunks = 0
        buf = b""                # bytes from `probe` to EOF, accumulated front-to-back
        probe = size             # where `buf` currently begins
        while True:
            chunks += 1
            want = max(0, size - chunk)
            # one byte before, so the line-boundary test below has something to test
            new_probe = max(0, want - 1) if want > 0 else 0
            if new_probe < probe:
                with open(path, "rb") as fh:
                    fh.seek(new_probe)
                    piece = fh.read(probe - new_probe)
                buf = piece + buf
                total_read += len(piece)
                probe = new_probe
            # first COMPLETE line at or after `want`, by the same rule as _read_from
            if probe == 0:
                start, body = 0, buf
            elif buf[:1] == b"\n":
                start, body = probe + 1, buf[1:]
            else:
                nl = buf.find(b"\n")
                start, body = ((size, b"") if nl < 0
                               else (probe + nl + 1, buf[nl + 1:]))
            lines, offsets = _split_raw(body, start)
            at_bof = start == 0
            if last_turns is not None:
                occ = _turn_occurrences(lines)
                # N+1, so the Nth-newest turn is provably whole. See the docstring.
                enough = len(occ) >= last_turns + 1
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
            occ = _turn_occurrences(lines)
            if len(occ) > last_turns:
                # The FIRST LINE of the Nth-from-last turn OCCURRENCE -- a position, not the
                # first line carrying a kept id. See _turn_occurrences for what the identity
                # form did on a resumed transcript.
                cut = occ[-last_turns][0]
                kept = lines[cut:]
                win["head_complete"] = True
            else:
                # The whole file has <= N turns. start is 0 here (the loop only stops early
                # when `enough`), so nothing was cut and nothing can be partial.
                kept, cut = lines, 0
                win["head_complete"] = at_bof
        else:
            cut = len(lines)
            for i, line in enumerate(lines):
                st = line.strip()
                if not st:
                    continue
                try:
                    d = json.loads(st)
                except Exception:
                    continue
                ts = d.get("timestamp") if isinstance(d, dict) else None
                if isinstance(ts, str) and ts >= since:
                    cut = i
                    break
            kept = lines[cut:]
            # A `since` window's oldest turn can still be a fragment: the cut lands on the
            # first entry stamped at or after the instant asked for, and that entry may be
            # the second row of a turn whose first row is older. Said, not hidden.
            win["head_complete"] = at_bof and cut == 0
        # EITHER condition makes the window have a horizon: it began past byte 0, or it
        # threw away lines it had read. Written as one rule for both modes deliberately --
        # the first version tested the trim only for `last_turns`, so a `since` window over
        # a file smaller than the first chunk trimmed turns and still reported
        # spans_are_local=False, suppressing the floor warning in exactly the case where it
        # was true. `self_test` now asserts it for both modes.
        win["spans_are_local"] = win["start_offset"] > 0 or len(kept) < len(lines)

    win["lines_kept"] = len(kept)
    # START_OFFSET IS THE FIRST KEPT LINE, NOT THE FIRST LINE READ. The two differ by
    # whatever the trim discarded, and conflating them was a real defect: a caller that
    # took `start_offset` as "where this window begins" and passed it back as `from_offset`
    # got back the trimmed turns as well -- it disagreed with its own window on 26 of 45
    # real transcripts. The raw read position is still reported, as `read_offset`, because
    # it is what the cost figures are about.
    #
    # LOOKED UP, NOT COMPUTED. The first version of this fix added up the re-encoded
    # lengths of the dropped lines, which is wrong on any byte that does not survive a
    # decode/encode round trip and wrong again on CRLF -- see _split_raw. `offsets` is read
    # off the raw bytes, so the right answer is an index rather than a sum.
    win["read_offset"] = win["start_offset"]
    win["start_offset"] = offsets[cut] if cut < len(offsets) else size
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


def public_label(label):
    """A tool label safe to PUBLISH, with any MCP server segment removed.

    `measure_token_breakdown.call_signature` returns the tool's own name as the label for
    everything except Bash, and justifies publishing it in a specific way: "which this file
    already publishes in BY TOOL, so no new surface is opened." That justification is sound
    THERE and does not transfer HERE -- this module prints no BY TOOL table, and its whole
    premise is that its output is shipped to a monitoring process that may log it.

    Review found the consequence on a real transcript, unprompted: a full
    `mcp__<fleet-control-server>__<verb>` name sitting in an evidence row -- and
    `scripts/scrub_check.sh` reports such a string as `FINDING [internal MCP servers]`,
    which was confirmed by planting one. So this module was publishing the private service
    names that the comment above MCP_PREFIX says it refuses to publish, two hundred lines
    apart. (The real name is not written out here either, for the same reason -- writing it
    into this docstring as the example tripped the scrubber on the very fix for it.)

    `mcp__<server>__<verb>` therefore becomes `mcp:<verb>`: the verb is what the signal is
    about ("which call repeated"), the server is the site-specific part, and nothing about
    the DETECTION changes -- matching still happens over the full digest of the untouched
    arguments, and `call_keys` are not rewritten. Only what crosses the boundary is
    narrowed. A non-MCP name is a closed public vocabulary (Bash:<cluster>, Read, Edit, ...)
    and is passed through.
    """
    if not isinstance(label, str):
        return label
    short = _strip_mcp(label)
    return ("mcp:" + short) if short != label else label


def public_thread(thread):
    """A thread label safe to PUBLISH: the main thread by name, a sidechain by TAG.

    The second half of F4, found while answering the reviewer's own follow-up question and
    very nearly missed. `repeated_invocations` takes its `thread` from
    `measure_token_breakdown.repeated_runs`, which keys sidechain threads by `agent_key()` --
    the transcript's `attributionAgent`, i.e. the SUBAGENT PROFILE NAME. Two profile names
    in this box's own corpus match `scripts/scrub_check.sh`'s `internal fleet-control
    product` pattern, 912 occurrences between them, so a sidechain repeat from such a thread
    published a string this repository refuses -- exactly the F4 defect, in a field the F4
    fix did not touch. A 2665-row sweep missed it because it needs a sidechain thread AND a
    repeat AND that profile; the leak is rare, not absent, and rare is worse because it
    survives a sweep.

    A sidechain therefore becomes `sidechain:<8 hex>` of its profile name -- the same scheme
    `anonymise()` and `bash_cluster()`'s `(other)` fingerprint already use here, and it keeps
    what the signal actually needs ("which thread was looping", stably, across calls and
    runs) while naming no profile. The main thread and the "type not recorded" placeholder
    are closed literals from measure_token_breakdown.py and pass through.

    Grouping is NOT affected: `repeated_runs` buckets by the real key internally and this
    renames only what crosses the boundary.
    """
    if not isinstance(thread, str) or thread in (MTB.MAIN, MTB.NO_AGENT_TYPE):
        return thread
    return "sidechain:" + hashlib.sha256(
        thread.encode("utf-8", "surrogateescape")).hexdigest()[:8]


def _empty(window):
    """Is there nothing in this window to look at at all?"""
    return window["turns"] == 0


def _check_threshold(name, v):
    """A threshold below 1 is refused HERE, in the library, not only in the CLI.

    The CLI already refuses it, but alm#839 calls these functions directly and would never
    pass through that check -- so the guard belonged on the function. A threshold of 0 is
    not a strict setting, it flags the ABSENCE of the thing being looked for: every window
    with any repeat at all fires, and the caller learns nothing. Raised rather than
    silently clamped, because a clamped threshold is a different measurement than the one
    asked for and nothing in the output would say so.
    """
    if v is None:
        return
    if not isinstance(v, int) or isinstance(v, bool):
        raise TypeError("%s threshold must be an int or None, got %r" % (name, v))
    if v < 1:
        raise ValueError("%s threshold %d is below 1; a threshold of 0 flags the absence "
                         "of the thing it is looking for" % (name, v))


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

    THREE changes, not one, and review was right that the previous docstring claimed only
    the first. (a) It takes LINES instead of a path -- the windowing adaptation. (b) An
    `isinstance(d, dict)` guard, because a window can legitimately contain a line holding a
    bare JSON scalar where the upstream census, reading whole files it had already filtered,
    never met one. (c) `b.get('input', {}).get('command','')` became the `isinstance(inp,
    dict)` form: upstream raises AttributeError when `input` is a bare string, which is
    correct for a census that wants to know, and wrong for a library a watchdog calls, so
    this returns `''` instead. Both (b) and (c) are HARDENINGS that change behaviour only on
    input upstream would have crashed on, and both are now named here rather than described
    as "no change". This is the windowing adaptation
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
    _check_threshold("batchable_bash", threshold)
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
            # `nx not in turns` was dead -- next_tool_turn only ever appends a mid it
            # looked up in `turns`. Removed rather than left as a guard that reads like a
            # real possibility.
            if (m, nx) in seen_pair:
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
    _check_threshold("repeated_invocations", threshold)
    w = win["window"]
    if _empty(w):
        return _flag("repeated_invocations", "unmeasured", [], threshold, w, SRC_ARI,
                     REPEATED_NOTE, measure_of=RI_MEASURE,
                     reason="the window holds no assistant turn at all")

    turns = win["turns"]
    calls = sum(len(t["call_keys"]) for t in turns)
    rows = []
    # `public_label` on the way OUT only: detection still compares the full digest of the
    # untouched arguments, and `call_keys` are not rewritten. See public_label.
    for thread, rec in MTB.repeated_runs(turns).items():
        for lab, sig, n in rec["runs"]:
            rows.append(dict(window="consecutive", thread=public_thread(thread),
                             span=None, label=public_label(lab),
                             fingerprint=sig[:8], count=n))
    for idx, lab, sig, n in MTB.repeated_in_spans(turns, win["stats"]["real_user_turns"]):
        rows.append(dict(window="span", thread=None, span=idx,
                         label=public_label(lab), fingerprint=sig[:8], count=n))

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


def agent_reuse_gap(win, threshold=None, min_content_run=MIN_CONTENT_RUN,
                    orch_tools=None):
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
    _check_threshold("agent_reuse_gap", threshold)
    if not isinstance(min_content_run, int) or min_content_run < 1:
        raise ValueError("min_content_run must be an int >= 1, got %r" % (min_content_run,))
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

    # `orch_tools` is threaded through rather than left on tool_class only: the override
    # is documented at MCP_PREFIX, and a documented override that the flag does not pass
    # down is not an override.
    classes = [turn_class(t["tools"], orch_tools) for t in main]
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

    # alm#833's run is "a CONTIGUOUS RUN OF CONTENT TURNS INSIDE A SPAN", and the "inside a
    # span" is load-bearing, not decoration: its census_c2_runs.py builds runs with
    # `for s in spans:` and resets the accumulator at every span boundary, because the unit
    # being priced is one brief handed off once -- and an operator prompt arriving mid-run
    # means the parent was addressed again, so what follows is a new brief rather than more
    # of the same one.
    #
    # This ran the accumulator over the whole window with no reset, which merged runs across
    # an operator prompt into one. The direction mattered: it made runs LONGER, so
    # `delegable_looking` was true more often and the gate opened more readily than the
    # definition being cited warranted. Found by review; the turn records already carried
    # `span`, so the correct unit was available the whole time.
    longest, cur, prev_span = 0, 0, None
    for t, c in zip(main, classes):
        if t["span"] != prev_span:
            cur = 0                     # a real user turn intervened: new brief, new run
            prev_span = t["span"]
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
              agent_gap_threshold=None, min_content_run=MIN_CONTENT_RUN,
              orch_tools=None):
    """Run every flag over one window and return them keyed by name.

    One parse, three detectors -- which is the other half of "cheap": a caller polling three
    flags should not read the file three times. `window` is returned once at the top as well
    as inside each flag, because a caller logging a single flag still needs its provenance.
    """
    out = {
        "batchable_bash": batchable_bash(win, batchable_threshold),
        "repeated_invocations": repeated_invocations(win, repeated_threshold),
        "agent_reuse_gap": agent_reuse_gap(win, agent_gap_threshold, min_content_run,
                                           orch_tools),
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

    # The disclosed residual, reproduced deliberately rather than only admitted in prose:
    # a transcript that REPEATS a message.id far from its first use. The full read merges
    # the two into one turn at the earlier position; a window holding only the later one
    # does not. Pinned so the behaviour cannot change unnoticed.
    rep_mid = (_repeat_lines(12)
               + _content_run_lines(6)
               + [_mk("rm0", [_tu("zz1", "Bash", dict(command="echo resumed"))],
                      parent="gr5", uuid="za9")])
    p_dup = _write(tmp, "dupmid.jsonl", rep_mid)
    f_dup = read_window(p_dup)
    w_dup = read_window(p_dup, last_turns=2, first_chunk=256)
    dup_mids = [m for m, c in __import__("collections").Counter(
        m for _i, m in _assistant_mids(open(p_dup).read().splitlines())).items() if c > 1]
    check(len(dup_mids) == 1,
          "the fixture really does repeat exactly one message.id far from its first use")
    check(w_dup["window"]["turns"] == 2
          and [t["call_keys"] for t in w_dup["turns"]]
          != [t["call_keys"] for t in f_dup["turns"]][-2:],
          "RESIDUAL PINNED: on a transcript that replays a message.id, the window and the "
          "full read disagree -- the full read merged the two occurrences into one turn at "
          "the earlier position. A property of collect_turns' key, not of the window; "
          "measured at 3 of 675 comparisons over 45 real transcripts")

    # start_offset must name the first KEPT line, so passing it back reproduces the window
    w_rt = read_window(p, last_turns=7, first_chunk=1024)
    r_rt = read_window(p, from_offset=w_rt["window"]["start_offset"])
    check(w_rt["window"]["read_offset"] < w_rt["window"]["start_offset"],
          "the read began at %d and the window's content begins at %d -- two different "
          "numbers, reported separately"
          % (w_rt["window"]["read_offset"], w_rt["window"]["start_offset"]))
    check([t["call_keys"] for t in r_rt["turns"]] == [t["call_keys"] for t in w_rt["turns"]],
          "and resuming from start_offset reproduces the SAME window, call for call -- the "
          "defect this replaced disagreed on 26 of 45 real transcripts")

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
    # read_fraction <= 1: the growth loop must EXTEND its buffer, not re-read from a new
    # offset. A tiny first_chunk forces many growth steps, which is the shape that used to
    # charge for the file several times over.
    fr = [read_window(p, last_turns=n, first_chunk=fc)["window"]["read_fraction"]
          for n in (1, 5, 20, 60) for fc in (64, 512, 8192)]
    check(max(fr) <= 1.0,
          "no window reads more than the file even with a 64-byte first chunk forcing the "
          "growth loop repeatedly (max read_fraction %.3f over %d combinations) -- measured "
          "1.934 before the buffer was extended rather than re-read" % (max(fr), len(fr)))
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
    check(s_all["window"]["spans_are_local"] is False,
          "and it is NOT reported as a local-span window, because it threw nothing away")
    s_fut = read_window(p4, since="2099-01-01T00:00:00Z", first_chunk=256)
    check(s_fut["window"]["turns"] == 0,
          "a `since` in the future keeps no turn (kept %d), rather than defaulting to all"
          % s_fut["window"]["turns"])
    # The bug this asserts against: BOTH windowing modes must report a horizon when they
    # TRIMMED, even on a file small enough that the read started at byte 0. The first
    # version tested the trim for last_turns only, so a `since` window silently suppressed
    # the "every count is a floor" warning in exactly the case where it applied.
    whole = read_window(p4)
    small_since = read_window(p4, since="2026-10-20T00:08:00Z", first_chunk=10 ** 7)
    small_last = read_window(p4, last_turns=4, first_chunk=10 ** 7)
    # read_offset, not start_offset: the READ began at byte 0 in all three cases, while a
    # trimmed window's CONTENT begins later. Asserting start_offset here was this control's
    # own bug, caught when the two fields were separated.
    check(whole["window"]["read_offset"] == 0
          and small_since["window"]["read_offset"] == 0
          and small_last["window"]["read_offset"] == 0,
          "all three of these READS start at byte 0 (the file is smaller than the chunk), "
          "so only the TRIM can set the floor flag")
    check(whole["window"]["spans_are_local"] is False
          and small_since["window"]["spans_are_local"] is True
          and small_last["window"]["spans_are_local"] is True,
          "and it is set for BOTH windowing modes and not for the full read -- the floor "
          "warning cannot be suppressed by a file being small")

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

    # ---- F2: the ten mutants that used to survive -----------------------------------------
    #
    # An independent review mutated this file 36 ways and found 10 defects that left this
    # suite GREEN. Each control below is the distinguishing input for one of them, and each
    # asserts the ORIGINAL behaviour -- so the mutant it was written for now goes red. This
    # block exists because a suite that cannot fail is decoration, and these ten were the
    # part of it that could not.
    print()
    print("F2: the ten mutants that used to survive this suite")

    # 1. alm#831's retry rule is `jc >= RETRY_JACCARD`, not `>`. Jaccard exactly 3/5 = 0.60.
    jc_a, jc_b = "zzz1 zzz2 zzz3 zzz4", "zzz1 zzz2 zzz3 zzz5"
    check(abs(jaccard(jc_a, jc_b) - RETRY_JACCARD) < 1e-9,
          "a pair of commands sitting EXACTLY on the %.2f retry threshold exists (jaccard "
          "%.3f) -- the boundary is reachable, so it can be tested"
          % (RETRY_JACCARD, jaccard(jc_a, jc_b)))
    p_eq = _write(tmp, "jc_eq.jsonl", [
        _mk("e0", [_tu("ec0", "Bash", dict(command=jc_a))], uuid="ea0"),
        _res("er0", "ea0", "ec0", "nothing specific"),
        _mk("e1", [_tu("ec1", "Bash", dict(command=jc_b))], parent="er0", uuid="ea1"),
        _res("er1", "ea1", "ec1", "nothing specific")])
    f_eq = batchable_bash(read_window(p_eq), threshold=1)
    check(f_eq["pairs_examined"] == 1 and f_eq["measure"] == 0,
          "and a pair AT the threshold is treated as a retry and is NOT mergeable "
          "(examined %d, mergeable %d) -- `>=` not `>`"
          % (f_eq["pairs_examined"], f_eq["measure"]))

    # 2. BOTH turns of a pair must be a single tool call AND a single Bash call.
    p_two = _write(tmp, "twotool.jsonl", [
        _mk("t0", [_tu("tc0", "Bash", dict(command="wc -l /srv/a.txt")),
                   _tu("tc0b", "Read", dict(file_path="/srv/b.txt"))], uuid="ta0"),
        _res("tr0", "ta0", "tc0", "7"),
        _mk("t1", [_tu("tc1", "Bash", dict(command="wc -l /srv/c.txt"))],
            parent="tr0", uuid="ta1"),
        _res("tr1", "ta1", "tc1", "9"),
        _mk("t2", [_tu("tc2", "Bash", dict(command="wc -l /srv/d.txt"))],
            parent="tr1", uuid="ta2"),
        _res("tr2", "ta2", "tc2", "11")])
    f_two = batchable_bash(read_window(p_two), threshold=1)
    check(f_two["pairs_examined"] == 1,
          "a two-tool turn next to a single-Bash turn contributes NO pair; only the "
          "single/single pair is examined (%d, not 2)" % f_two["pairs_examined"])

    # 3. alm#831's BARRIER: a text-only assistant turn between two Bash turns kills the pair.
    #    Review established this is the only barrier that is load-bearing -- the `user_turn`
    #    barrier cannot remove a pair in either implementation, because next_tool_turn stops
    #    descending at a real user turn and so returns an empty `found` anyway. The
    #    load-bearing one was the untested one.
    p_bar = _write(tmp, "barrier.jsonl", [
        _mk("b0", [_tu("bc0", "Bash", dict(command="wc -l /srv/a.txt"))], uuid="ba0"),
        _res("br0", "ba0", "bc0", "7"),
        _mk("b1", [dict(type="text", text="let me think about that")],
            parent="br0", uuid="ba1"),
        _mk("b2", [_tu("bc2", "Bash", dict(command="wc -l /srv/z.txt"))],
            parent="ba1", uuid="ba2"),
        _res("br2", "ba2", "bc2", "9")])
    f_bar = batchable_bash(read_window(p_bar), threshold=1)
    check(f_bar["state"] == "unmeasured" and f_bar["pairs_examined"] == 0,
          "a TEXT-ONLY assistant turn between two Bash turns is a barrier: 0 pairs "
          "examined, so nothing is mergeable across it (got %d)" % f_bar["pairs_examined"])

    # 4. signal 2's SECOND window. `A B A B A` has no consecutive run longer than 1 and
    #    still ran A three times inside one span -- the case repeated_invocations' own
    #    docstring cites and the suite had no positive control for. The 9-identical-Read
    #    fixture reports 9 by either window, so deleting repeated_in_spans left it green.
    ab, parent = [], None
    for i in range(5):
        nm = "Read" if i % 2 == 0 else "Grep"
        inp = dict(file_path="/srv/loop") if i % 2 == 0 else dict(pattern="loop")
        ab.append(_mk("ab%d" % i, [_tu("abc%d" % i, nm, inp)], parent=parent,
                      uuid="aba%d" % i))
        ab.append(_res("abr%d" % i, "aba%d" % i, "abc%d" % i, "ok"))
        parent = "abr%d" % i
    p_ab = _write(tmp, "alternating.jsonl", ab)
    f_ab = repeated_invocations(read_window(p_ab), threshold=2)
    check(f_ab["consecutive_longest"] <= 1 and f_ab["in_span_max"] >= 3,
          "an A B A B A loop: longest CONSECUTIVE run %d, but %d occurrences of one "
          "signature within the span -- the second window is what sees it"
          % (f_ab["consecutive_longest"], f_ab["in_span_max"]))
    check(f_ab["fired"] and f_ab["measure"] == f_ab["in_span_max"],
          "and it FIRES on that, with `measure` taken from the in-span window (%d) -- so "
          "deleting repeated_in_spans now goes red" % f_ab["measure"])

    # 5. alm#833's break-even is `>= min_content_run`, not `>`. A run of EXACTLY 3 counts.
    p_e3 = _write(tmp, "exactly3.jsonl", _content_run_lines(3))
    g_e3 = agent_reuse_gap(read_window(p_e3), threshold=1, min_content_run=3)
    check(g_e3["longest_content_run"] == 3 and g_e3["delegable_looking"] is True
          and g_e3["fired"] is True,
          "a CONTENT run of EXACTLY 3 is delegable-looking at the 3-turn break-even and "
          "fires -- `>=` not `>`")

    # 6. SIDECHAIN turns are excluded from the gap: a sidechain IS a subagent doing work,
    #    so counting its turns as turns-without-a-subagent inverts the signal.
    side, parent = list(_content_run_lines(5)), "gr4"
    for i in range(5):
        side.append(_mk("sm%d" % i, [_tu("sc%d" % i, "Bash",
                                         dict(command="grep -rn side%d ." % i))],
                        parent=parent, uuid="sa%d" % i, sidechain=True))
        side.append(_res("sr%d" % i, "sa%d" % i, "sc%d" % i, "ok"))
        parent = "sr%d" % i
    p_side = _write(tmp, "sidechain.jsonl", side)
    g_side = agent_reuse_gap(read_window(p_side), threshold=5)
    check(g_side["main_turns"] == 5 and g_side["turns_since_dispatch"] == 5
          and g_side["fired"] is False,
          "5 main + 5 sidechain turns: the gap counts the 5 MAIN turns (%d) and does not "
          "fire at a threshold of 5; counting the sidechain's own turns would make it 10 "
          "and fire" % g_side["turns_since_dispatch"])

    # 7. alm#833's REPORT class: a turn with no tool call is the agent talking to its
    #    operator, is non-delegable by construction, and BREAKS a content run.
    check(turn_class(set()) == 'REPORT' and turn_class({"Bash"}) == 'CONTENT',
          "a turn with no tool call classifies as REPORT, not CONTENT")
    rep = list(_content_run_lines(2))
    rep.append(_mk("rep1", [dict(type="text", text="here is what I found")],
                   parent="gr1", uuid="repa1"))
    rep += [ln.replace('"gm', '"hm').replace('"ga', '"ha').replace('"gc', '"hc')
              .replace('"gr', '"hr') for ln in _content_run_lines(2)]
    p_rep = _write(tmp, "report.jsonl", rep)
    g_rep = agent_reuse_gap(read_window(p_rep), threshold=1, min_content_run=3)
    check(g_rep["longest_content_run"] == 2 and g_rep["fired"] is False,
          "and a REPORT turn inside a run BREAKS it: longest run %d, not the 4 CONTENT "
          "turns either side of it, so the 3-turn gate stays shut"
          % g_rep["longest_content_run"])

    # 8. a `since` window whose cut lands past line 0 cannot promise a whole oldest turn.
    #    head_complete=False was asserted nowhere.
    s_hc = read_window(p4, since="2026-10-20T00:08:00Z", first_chunk=10 ** 7)
    check(s_hc["window"]["head_complete"] is False,
          "a `since` window that cut past line 0 reports head_complete=False -- its oldest "
          "turn may be a fragment, and saying True would be a promise it cannot keep")
    check(read_window(p4)["window"]["head_complete"] is True,
          "while a full read of the same file reports head_complete=True")

    # 9. two windowing arguments is not a window, and is refused rather than silently
    #    resolved in favour of whichever the code tests first.
    two_refused = 0
    for kw in (dict(last_turns=2, since="2026-10-20T00:00:00Z"),
               dict(last_turns=2, from_offset=0),
               dict(since="2026-10-20T00:00:00Z", from_offset=0)):
        try:
            read_window(p4, **kw)
        except ValueError:
            two_refused += 1
    check(two_refused == 3,
          "every pair of windowing arguments is refused (%d of 3)" % two_refused)

    # 10. _read_from's `nl < 0` branch: an offset landing inside an UNTERMINATED final line
    #     has no complete line at or after it, so the answer is "nothing", not "that
    #     fragment". The offset sweep above runs on a file ending in a newline, so this
    #     branch was never entered.
    p_unterm = _write(tmp, "unterm.jsonl", [])
    with open(p_unterm, "w") as fh:
        fh.write('{"type":"assistant","uuid":"u1"}\n{"type":"assistant","trunca')
    sz = os.path.getsize(p_unterm)
    raw_in, start_in = _read_from(p_unterm, sz - 3, sz)
    check(raw_in == b"" and start_in == sz,
          "an offset inside an unterminated final line returns NO bytes and an offset of "
          "EOF (got %r, %d) -- there is no complete line at or after it"
          % (raw_in[:12], start_in))
    raw_ok, start_ok = _read_from(p_unterm, 5, sz)
    check(raw_ok.startswith(b'{"type":"assistant","trunca'),
          "while an offset inside the FIRST line still returns the unterminated tail, "
          "because a complete line boundary does exist after it")

    # ---- F4, second half: a SUBAGENT PROFILE NAME must not be published either -----------
    print()
    print("a sidechain's thread label is a tag, not its profile name")
    side_lines, sparent = [], None
    PROFILE = "some_local_developer__model_opus"
    for i in range(4):
        side_lines.append(json.dumps(dict(
            type="assistant", uuid="pa%d" % i, parentUuid=sparent, isSidechain=True,
            attributionAgent=PROFILE, timestamp="2026-10-20T00:00:00Z",
            message=dict(id="pm%d" % i, usage=dict(input_tokens=1),
                         content=[_tu("pc%d" % i, "Read", dict(file_path="/x"))]))))
        side_lines.append(_res("pr%d" % i, "pa%d" % i, "pc%d" % i, "ok"))
        sparent = "pr%d" % i
    p_prof = _write(tmp, "profile.jsonl", side_lines)
    f_prof = repeated_invocations(read_window(p_prof), threshold=1)
    blob = json.dumps(f_prof["evidence"])
    check(f_prof["fired"] and PROFILE not in blob,
          "a sidechain repeat FIRES (so the row exists) and its evidence does not contain "
          "the profile name")
    threads = {r["thread"] for r in f_prof["evidence"] if r["thread"]}
    check(threads == {public_thread(PROFILE)} and public_thread(PROFILE).startswith(
              "sidechain:"),
          "it is published as %s -- stable, distinguishing, and naming no profile"
          % public_thread(PROFILE))
    check(public_thread(MTB.MAIN) == MTB.MAIN
          and public_thread(MTB.NO_AGENT_TYPE) == MTB.NO_AGENT_TYPE,
          "while the main thread and the 'type not recorded' placeholder pass through, "
          "being closed literals rather than site-specific names")
    check(public_thread("profile_a") != public_thread("profile_b"),
          "and two different profiles get two different tags, so the label still "
          "distinguishes threads")

    # ---- control: the library refuses what the CLI refuses --------------------------------
    print()
    print("a threshold below 1 is refused by the FUNCTION, not only by the CLI")
    wc = read_window(p_cont)
    for fn, nm in ((batchable_bash, "batchable_bash"),
                   (repeated_invocations, "repeated_invocations"),
                   (agent_reuse_gap, "agent_reuse_gap")):
        raised = 0
        for bad in (0, -1):
            try:
                fn(wc, threshold=bad)
            except ValueError:
                raised += 1
        try:
            fn(wc, threshold=2.5)
        except TypeError:
            raised += 1
        check(raised == 3,
              "%s refuses thresholds 0, -1 and 2.5 (%d of 3) -- alm#839 calls the "
              "function, so a guard living only in argparse would not protect it"
              % (nm, raised))
    try:
        agent_reuse_gap(wc, threshold=5, min_content_run=0)
        bad_mcr = False
    except ValueError:
        bad_mcr = True
    check(bad_mcr, "and min_content_run=0 is refused too, not silently clamped")
    check(agent_reuse_gap(wc, threshold=5, orch_tools={"Bash"})["fired"] is False
          and agent_reuse_gap(wc, threshold=5)["fired"] is True,
          "the orchestration vocabulary reaches the flag: declaring Bash orchestration "
          "turns the same window from FIRED to silent, so the override is not documentation")

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
