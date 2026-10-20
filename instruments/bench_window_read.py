#!/usr/bin/env python3
"""Is the windowed read path in `flags.py` actually CHEAPER than parsing the whole file?

ARI#24 asks for a number rather than an assumption, so this is the number. It runs both
read paths over real local transcripts and reports bytes read, lines parsed, and wall-clock
for each, plus the ratio -- the claim "cheap" rests on this file's output and on nothing
else.

WHAT IS COMPARED. Three reads of the same file, in the shape a caller would actually use:

  FULL        `read_window(path)` -- byte 0 to EOF, every line JSON-parsed. This is what
              every existing instrument in this repository does and what alm#839's
              watchdog would otherwise have had to do on every poll.
  WINDOW      `read_window(path, last_turns=N)` -- the newest N logical turns, found by a
              tail seek that grows until it has them.
  RESUME      `read_window(path, from_offset=K)` -- everything appended since byte K. This
              is the one the watchdog is built on: K is the cursor it kept from its last
              poll. K is taken from the WINDOW read's own `start_offset` -- the first line
              that window KEPT, not where its read began -- so "the session appended N
              turns since I last looked" is simulated with the real byte position of those
              real turns rather than a guessed offset.

THREE COSTS, NOT ONE, because they do not move together and a caller is paying all three:
bytes off disk, lines fed to `json.loads`, and wall-clock. Wall-clock is the one that
varies between runs and between machines (page cache, other load), so it is reported as
the MEDIAN of several reps with the min and max alongside -- a single timing presented as
a ratio would be the most quotable and least reliable thing here.

THE RESULT DEPENDS ON THE SESSION, AND THE PER-FILE TABLE IS THE POINT. A window is cheap
in proportion to how much of the session it is NOT reading: 40 turns out of 3000 is a
rounding error, 40 turns out of 48 is the whole file. So a session SHORTER than the window
shows no saving at all, correctly, and the per-file rows are printed rather than collapsed
into one headline. The headline this prints is a MEDIAN over files, and the spread is
printed next to it, because a mean over a corpus containing both shapes would describe
neither.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    Ratios of 1.00 across the board -- which is also what a genuinely useless windowing
    implementation prints, so that reading has to be distinguishable from a broken harness.
    Four ways to be blind here, and the guard for each:

      1. ROOTS points at nothing, or no file is large enough to be worth windowing. Then
         `transcripts measured: 0` and the run REFUSES (exit 2) rather than printing a
         ratio over an empty corpus. A speedup averaged over no files is 1.00, and that
         number would look like a finding.
      2. THE PAGE CACHE DOES THE WORK. A first read pulls the file off disk and every
         later read of it is served from RAM, so whichever path runs first pays for both.
         Guarded two ways: a warm-up read of each file happens BEFORE any timing, so every
         timed read is equally cache-warm, and the byte/line counts -- which no cache can
         change -- are reported next to the timings. If the timing ratio and the byte ratio
         disagree, believe the bytes and distrust the clock. That is stated here rather
         than discovered later.
      3. THE WINDOW SILENTLY READ EVERYTHING. A reader that fell back to a full read on
         every file would report honest-looking 1.00 ratios. So every row asserts the
         window and the full read AGREE on the turns they share, and a row where they do
         not is printed as MISMATCH and excluded from the medians -- a wrong answer is not
         allowed to contribute a fast time.
      3b. THE FILE WAS A LIVE SESSION AND GREW WHILE BEING MEASURED. Then the window really
         did see a turn the full read never did, and that is correct behaviour rather than
         a mismatch. The first real run of this instrument hit exactly this and reported it
         as MISMATCH, which would have made a growth artefact and a genuine read-path
         defect look identical from then on. The size is now taken before AND after, such a
         row is marked GREW, and its agreement is reported as having NO ANSWER -- a third
         outcome, excluded from the figures, never folded into either of the other two.
      3c. THE TRANSCRIPT WAS RESUMED AND REPLAYS A `message.id`. Then flags.py's own
         documented residual applies: collect_turns merges the two occurrences, so the full
         read's tail is not the window's content by design. Marked RESIDUAL, reported with
         its own explanation, and excluded -- so MISMATCH keeps meaning "unexplained".
         Without this split the largest transcript in the real corpus reads as a defect.
      4. THE CORPUS IS ALL ONE SHAPE. A ratio measured only on huge files overstates what
         a caller gets, so `turns in file` is printed per row and the summary splits the
         corpus at the window size: files with more turns than the window (where a saving
         is possible at all) and files with fewer (where it is not, and where this reports
         no saving rather than hiding them).

    Paths are never printed. A row is named by `sess-<8 hex>`, the same tag the other
    instruments' --anonymise emits, and there is no flag to turn it off: the only reason
    this instrument prints a file name is to flag a row, and a published sample carrying a
    real transcript path is what scripts/scrub_check.sh exists to refuse.

    What it cannot tell you: anything about a machine other than this one. These are
    wall-clock figures from one box under whatever load it had. The byte and line counts
    are properties of the files and the code, and travel; the timings do not.

Read-only: opens transcript files for reading only. Never writes, never deletes.
"""
import argparse
import importlib.util
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import engines
except ImportError:                                  # pragma: no cover
    engines = None

_spec = importlib.util.spec_from_file_location("ari_flags", os.path.join(HERE, "flags.py"))
FL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FL)

# Default roots come from the engine adapter table, so this instrument learns about a new
# engine when engines.py does. `~` is left UNEXPANDED here and expanded at the point of
# use, which is both the convention measure_active_concurrency.py already follows and the
# reason this file carries no absolute home path for scripts/scrub_check.sh to find.
DEFAULT_ROOTS = (":".join(engines.transcript_roots()) if engines
                 else "~/.claude/projects:~/.codex/sessions")


def discover(roots, min_bytes):
    """Candidate transcripts, largest first, DEDUPED by session identity.

    The dedup is not tidiness. Review re-ran this instrument and found 4 distinct
    transcripts listed TWICE each -- the same session visible under two account roots --
    which means the corpus median was weighted by how many account directories happen to
    hold a copy of a session rather than by how many sessions there are. That is a real way
    for the headline ratio to move without anything about the code or the sessions
    changing.

    Deduped on (basename, size): a Claude Code transcript's basename is its session uuid,
    so two paths sharing one are the same session, and the size guards against the
    pathological case of two genuinely different sessions colliding on a name. The count
    dropped is reported by the caller rather than silently absorbed.
    """
    out = []
    missing = []
    seen = {}
    dupes = 0
    for r in roots:
        if not os.path.isdir(r):
            missing.append(r)
            continue
        for dirpath, _dirnames, files in os.walk(r):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                if sz < min_bytes:
                    continue
                key = (fn, sz)
                if key in seen:
                    dupes += 1
                    continue
                seen[key] = p
                out.append((sz, p))
    out.sort(reverse=True)
    return out, missing, dupes


def time_read(fn, reps):
    """Median / min / max wall-clock of `fn` over `reps` runs, plus one result.

    `perf_counter`, and the result of the LAST rep is returned so the caller compares the
    thing it timed rather than a separate call. The warm-up is the caller's job (see blind
    mode 2) and is done once per file, not once per path, so neither path gets a cold read
    the other one paid for.
    """
    ts = []
    res = None
    for _ in range(reps):
        t0 = time.perf_counter()
        res = fn()
        ts.append(time.perf_counter() - t0)
    ts.sort()
    return statistics.median(ts), ts[0], ts[-1], res


def tag(path):
    """`sess-<8 hex>` for a transcript path -- the same scheme the other instruments'
    --anonymise uses (measure_token_breakdown.anonymise).

    NOT optional here, unlike there. This instrument prints a path only to NAME a file
    whose row needs attention, and a published sample carrying a real transcript path is
    exactly what scripts/scrub_check.sh refuses -- which is how the first run of this
    instrument failed that gate. There is no mode in which the raw path is wanted, so
    there is no flag to forget to pass.
    """
    import hashlib
    return "sess-" + hashlib.sha256(str(path).encode()).hexdigest()[:8]


def turn_keys(win):
    """The window's turns as comparable keys, for the agreement check (blind mode 3)."""
    return [tuple(t["call_keys"]) for t in win["turns"]]


def _streams(lines):
    """`lines` partitioned into (main thread, sidechain) -- the two streams whose entries
    interleave in one transcript file.

    Split on the same `isSidechain` field `measure_token_breakdown` keys its own thread
    attribution on; an entry without it is main-thread. A line that is not JSON belongs to
    neither and is dropped, exactly as `_assistant_mids` drops it.
    """
    main, side = [], []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            d = json.loads(s)
        except Exception:
            continue
        (side if isinstance(d, dict) and d.get("isSidechain") else main).append(line)
    return main, side


def _replays_an_id(lines):
    """True if one turn id appears in more than one maximal consecutive run within `lines`."""
    occ = FL._turn_occurrences(lines)
    return len(occ) != len({mid for _i, mid in occ})


def measure_file(path, last_turns, reps):
    """One row: the three reads, their costs, and whether they agree.

    THE FILE MAY BE A LIVE SESSION AND MAY GROW WHILE THIS RUNS. The first real run of
    this instrument reported MISMATCH on a transcript whose session was mid-turn: the full
    read and the window read happened seconds apart, the session appended in between, and
    the window correctly saw a newer turn the full read had never seen. That is not a
    disagreement between the two code paths, which is what MISMATCH means and what blind
    mode 3 exists to catch -- so reporting it as one would have put a live-growth artefact
    in the place reserved for a real defect, and the two would have been indistinguishable
    thereafter.

    So the size is taken BEFORE and AFTER, and a file that grew gets `agree: None` --
    "not measurable on this file", a third answer, reported separately from both pass and
    fail. The same distinction every flag in `flags.py` draws between a measured clean
    result and an unmeasured one.
    """
    size = os.path.getsize(path)
    # WARM-UP, before any timing: every timed read below is then equally cache-warm.
    with open(path, "rb") as fh:
        while fh.read(1 << 20):
            pass

    t_full, lo_full, hi_full, full = time_read(lambda: FL.read_window(path), reps)
    t_win, lo_win, hi_win, win = time_read(
        lambda: FL.read_window(path, last_turns=last_turns), reps)
    # start_offset, which is the first line the window KEPT -- not read_offset, where the
    # read began. Resuming from read_offset would re-read the turns the window trimmed and
    # so would measure a LARGER slice than the window it claims to be the cursor for.
    cursor = win["window"]["start_offset"]
    t_res, lo_res, hi_res, res = time_read(
        lambda: FL.read_window(path, from_offset=cursor), reps)

    size_after = os.path.getsize(path)
    grew_during = size_after != size

    # Is this a RESUMED transcript -- one that replays a `message.id`? If so, flags.py's own
    # disclosed residual applies and the two reads are EXPECTED not to line up, so calling
    # it MISMATCH would put a documented, bounded residual in the slot reserved for an
    # unexplained read-path disagreement. That is the same mistake as the GREW case one
    # level up, and the first run after the residual was documented made it: the largest
    # transcript in the corpus came back MISMATCH purely because it had been resumed.
    #
    # Detected the way flags.py defines it: a turn id that occurs in more than one maximal
    # consecutive run of assistant entries -- PER STREAM. The per-stream part is review's
    # (R3): one main turn whose entries are SPLIT by a sidechain turn's entries yields the
    # runs [main, side, main], and counting those together called the main id replayed when
    # nothing was. That direction is bounded -- this is consulted only where the two reads
    # already disagree, so it can never excuse an agreeing file, and it misclassified
    # nothing on the real corpus (4 of 45 fired, all 4 genuine far-apart replays). It is
    # fixed anyway because RESIDUAL means "the ONE disagreement we have accounted for": a
    # real read-path defect that happened to surface on a sidechain-interleaved transcript
    # would have been filed as the known residual and stopped being visible. That is the
    # error this file warns about two comments up, pointing the other way.
    resumed = any(_replays_an_id(ls) for ls in _streams(full["lines"]))

    # blind mode 3: the window must AGREE with the full read on the turns they share --
    # a question that only has an answer if the file held still while both ran.
    fk, wk = turn_keys(full), turn_keys(win)
    n = win["window"]["turns"]
    if grew_during:
        agree = None
    elif (wk == fk[-n:]) if n else (len(fk) == 0):
        agree = True
    else:
        agree = "residual" if resumed else False

    return dict(
        path=path, size=size, size_after=size_after, grew_during=grew_during,
        turns_in_file=full["window"]["turns"],
        lines_in_file=full["window"]["lines_total"],
        window_turns=n,
        window_lines=win["window"]["lines_kept"],
        resume_turns=res["window"]["turns"],
        resumed=resumed,
        full_bytes=full["window"]["bytes_read"],
        win_bytes=win["window"]["bytes_read"],
        res_bytes=res["window"]["bytes_read"],
        full_lines_parsed=full["window"]["lines_total"],
        win_lines_parsed=win["window"]["lines_total"],
        res_lines_parsed=res["window"]["lines_total"],
        win_chunks=win["window"]["chunks"],
        t_full=t_full, t_win=t_win, t_res=t_res,
        lo_full=lo_full, hi_full=hi_full, lo_win=lo_win, hi_win=hi_win,
        lo_res=lo_res, hi_res=hi_res,
        byte_ratio=(win["window"]["bytes_read"] / full["window"]["bytes_read"]
                    if full["window"]["bytes_read"] else None),
        res_byte_ratio=(res["window"]["bytes_read"] / full["window"]["bytes_read"]
                        if full["window"]["bytes_read"] else None),
        line_ratio=(win["window"]["lines_total"] / full["window"]["lines_total"]
                    if full["window"]["lines_total"] else None),
        time_ratio=(t_win / t_full) if t_full else None,
        res_time_ratio=(t_res / t_full) if t_full else None,
        agree=agree,
    )


def _mb(n):
    return n / 1048576.0


def human(rows, last_turns, reps, roots, missing, min_bytes, skipped, dupes=0):
    print("WINDOWED vs FULL TRANSCRIPT READ -- the cost of flags.py's read path")
    print()
    print("corpus: %d transcripts >= %d bytes, largest first (%d smaller skipped, "
          "%d duplicate copies of a session under another account root dropped)"
          % (len(rows), min_bytes, skipped, dupes))
    print("window: the newest %d logical assistant turns      reps: %d (median reported)"
          % (last_turns, reps))
    if missing:
        print("ROOTS/paths NOT FOUND (skipped): %d -- shown as tags, since a root is a "
              "path: %s" % (len(missing), ", ".join(tag(m) for m in missing)))
    print()
    print("Each row: one real transcript. FULL reads byte 0 to EOF and parses every line.")
    print("WINDOW tail-seeks to the newest %d turns. RESUME reads from the cursor a polling"
          % last_turns)
    print("caller would have kept -- i.e. what it costs to notice those %d turns arrived."
          % last_turns)
    print()
    hdr = ("%-7s %7s %6s | %9s %8s %8s | %7s %7s | %6s %6s"
           % ("size", "turns", "lines", "full MB", "win MB", "res MB",
              "full ms", "win ms", "byte", "time"))
    print(hdr)
    print("%-7s %7s %6s | %9s %8s %8s | %7s %7s | %6s %6s"
          % ("", "in file", "", "read", "read", "read", "median", "median",
             "ratio", "ratio"))
    print("-" * len(hdr))
    for r in rows:
        print("%-7s %7d %6d | %9.2f %8.2f %8.2f | %7.1f %7.1f | %6s %6s%s"
              % ("%.0fMB" % _mb(r["size"]), r["turns_in_file"], r["lines_in_file"],
                 _mb(r["full_bytes"]), _mb(r["win_bytes"]), _mb(r["res_bytes"]),
                 1000 * r["t_full"], 1000 * r["t_win"],
                 ("%.3f" % r["byte_ratio"]) if r["byte_ratio"] is not None else "n/a",
                 ("%.3f" % r["time_ratio"]) if r["time_ratio"] is not None else "n/a",
                 "" if r["agree"] is True
                 else "  GREW" if r["agree"] is None
                 else "  RESIDUAL" if r["agree"] == "residual" else "  MISMATCH"))

    bad = [r for r in rows if r["agree"] is False]
    grew = [r for r in rows if r["agree"] is None]
    resid = [r for r in rows if r["agree"] == "residual"]
    good = [r for r in rows if r["agree"] is True]
    if grew:
        print()
        print("GREW DURING MEASUREMENT -- %d file(s): the session appended while the three"
              % len(grew))
        print("reads were running, so the window legitimately saw a turn the full read")
        print("never did. This is NOT a mismatch between the two code paths -- it is a")
        print("live session, and the agreement question has no answer on it. Reported")
        print("separately and excluded from the figures below, so a growth artefact can")
        print("never be mistaken for the defect blind mode 3 looks for.")
        for r in grew:
            print("   %s  (%d -> %d bytes while measuring)"
                  % (tag(r["path"]), r["size"], r["size_after"]))
    if resid:
        print()
        print("RESUMED TRANSCRIPT, DISCLOSED RESIDUAL -- %d file(s): these replay a"
              % len(resid))
        print("`message.id`, so collect_turns merges two occurrences into one turn and the")
        print("full read's tail is NOT the window's content. Expected, bounded, and")
        print("documented in read_window's own contract -- NOT the unexplained read-path")
        print("disagreement MISMATCH is reserved for. Excluded from the figures below so")
        print("neither label can absorb the other.")
        for r in resid:
            print("   %s  (%d turns, %d lines)"
                  % (tag(r["path"]), r["turns_in_file"], r["lines_in_file"]))
    if bad:
        print()
        print("MISMATCH on %d file(s): the window and the full read disagreed about the "
              "turns" % len(bad))
        print("they share, the file did NOT grow, and it does NOT replay a message.id --")
        print("so this is a real, unexplained disagreement between the two read paths.")
        print("Excluded from every figure below -- see blind")
        print("mode 3. A wrong answer does not get to contribute a fast time.")
        for r in bad:
            print("   %s" % tag(r["path"]))

    # The split that keeps the headline honest (blind mode 4).
    big = [r for r in good if r["turns_in_file"] > last_turns]
    small = [r for r in good if r["turns_in_file"] <= last_turns]
    print()
    print("WHERE A SAVING IS POSSIBLE AT ALL -- the %d file(s) with MORE than %d turns:"
          % (len(big), last_turns))
    if big:
        for label, key in (("bytes read", "byte_ratio"), ("lines parsed", "line_ratio"),
                           ("wall-clock", "time_ratio")):
            vals = sorted(r[key] for r in big if r[key] is not None)
            print("   WINDOW / FULL, %-13s median %.3f   best %.3f   worst %.3f"
                  % (label, statistics.median(vals), vals[0], vals[-1]))
        for label, key in (("bytes read", "res_byte_ratio"),
                           ("wall-clock", "res_time_ratio")):
            vals = sorted(r[key] for r in big if r[key] is not None)
            print("   RESUME / FULL, %-13s median %.3f   best %.3f   worst %.3f"
                  % (label, statistics.median(vals), vals[0], vals[-1]))
        tf = sum(r["t_full"] for r in big)
        tw = sum(r["t_win"] for r in big)
        tr = sum(r["t_res"] for r in big)
        print()
        print("   over those %d files: FULL %.0f ms, WINDOW %.0f ms, RESUME %.0f ms total"
              % (len(big), 1000 * tf, 1000 * tw, 1000 * tr))
        print("   bytes:                FULL %.1f MB, WINDOW %.1f MB, RESUME %.1f MB"
              % (_mb(sum(r["full_bytes"] for r in big)),
                 _mb(sum(r["win_bytes"] for r in big)),
                 _mb(sum(r["res_bytes"] for r in big))))
        print("   lines fed to json.loads: FULL %d, WINDOW %d, RESUME %d"
              % (sum(r["full_lines_parsed"] for r in big),
                 sum(r["win_lines_parsed"] for r in big),
                 sum(r["res_lines_parsed"] for r in big)))
    else:
        print("   NONE. Every file in this corpus has <= %d turns, so this run measured no"
              % last_turns)
        print("   case where windowing can help. That is a fact about the corpus, not a")
        print("   result about the code -- lower --last-turns or point ROOTS somewhere with")
        print("   longer sessions.")
    print()
    print("WHERE IT IS NOT -- the %d file(s) with %d turns or fewer:"
          % (len(small), last_turns))
    if small:
        vals = sorted(r["byte_ratio"] for r in small if r["byte_ratio"] is not None)
        print("   WINDOW / FULL, bytes read     median %.3f   best %.3f   worst %.3f"
              % (statistics.median(vals), vals[0], vals[-1]))
        print("   A window that asks for more turns than the session has reads the whole")
        print("   file, and these rows show that rather than being filtered out. A ratio")
        print("   near 1.00 here is the correct answer, not a disappointing one.")
    else:
        print("   none in this corpus.")
    print()
    print("THE PER-FILE RATIOS ARE THE REPRODUCIBLE FIGURE, NOT THE MEDIAN. An independent")
    print("re-run on this box reproduced every per-file ratio to three decimals and got a")
    print("different corpus median, because the median depends on which sessions happen to")
    print("be the largest at the moment of the run. Quote a row, or re-run this yourself;")
    print("do not treat the median as a constant.")
    print()
    print("READ THE BYTES, NOT THE CLOCK, if the two disagree: every file was warmed before")
    print("timing, but wall-clock still carries this box's load and page cache, while the")
    print("byte and line counts are properties of the files and the code. See blind mode 2.")


def sweep(paths, last_turns, candidates):
    """How much does `flags.FIRST_CHUNK` cost? One row per candidate size.

    WHY THIS MODE EXISTS. The first real run of this instrument showed every file's window
    read costing almost exactly 1.25 MB regardless of the file's size -- which is 256 KB
    plus one 1 MB re-read, i.e. the first guess was too small on EVERY transcript in the
    corpus and the growth loop ran every time. That is a property of the default, not of
    the window, and leaving it at a value measured to be wrong would mean the cost figures
    above describe a policy nobody chose. So the default is picked from this table.

    Reported per candidate: total bytes read, total reads performed, and how many files
    needed more than one read. The answer that matters is the bytes, because a first chunk
    big enough to never grow is also a first chunk that over-reads small files -- the
    minimum is somewhere in the middle and is a property of the corpus, which is why this
    is measured rather than reasoned about.
    """
    rows = []
    for c in candidates:
        tot_bytes = tot_reads = grew = 0
        turns_ok = 0
        for p in paths:
            w = FL.read_window(p, last_turns=last_turns, first_chunk=c)
            tot_bytes += w["window"]["bytes_read"]
            tot_reads += w["window"]["chunks"]
            if w["window"]["chunks"] > 1:
                grew += 1
            if w["window"]["turns"] == min(last_turns, w["window"]["turns"]):
                turns_ok += 1
        rows.append(dict(first_chunk=c, bytes=tot_bytes, reads=tot_reads,
                         files_needing_growth=grew, files=len(paths),
                         answers_correct=turns_ok))
    return rows


def print_sweep(rows, last_turns, current):
    print("FIRST_CHUNK SWEEP -- how big should the first tail read be?")
    print()
    print("Window: the newest %d turns, over %d real transcripts. The answer is identical"
          % (last_turns, rows[0]["files"] if rows else 0))
    print("for every candidate (flags.py's own self-test asserts first_chunk cannot change")
    print("the result, only the cost) -- so this table is purely about cost.")
    print()
    print("%12s %12s %7s %22s" % ("first_chunk", "total MB", "reads", "files that grew"))
    print("-" * 56)
    best = min(rows, key=lambda r: r["bytes"]) if rows else None
    for r in rows:
        mark = ""
        if r["first_chunk"] == current:
            mark += "  <- current default"
        if best and r["first_chunk"] == best["first_chunk"]:
            mark += "  <- cheapest here"
        print("%12s %12.2f %7d %12d of %-4d%s"
              % ("%dK" % (r["first_chunk"] // 1024), _mb(r["bytes"]), r["reads"],
                 r["files_needing_growth"], r["files"], mark))
    print()
    if best:
        cur = next((r for r in rows if r["first_chunk"] == current), None)
        if cur and cur["first_chunk"] != best["first_chunk"]:
            print("The current default reads %.2f MB; the cheapest candidate reads %.2f MB"
                  % (_mb(cur["bytes"]), _mb(best["bytes"])))
            print("(%.0f%% less) over this corpus."
                  % (100 * (1 - best["bytes"] / cur["bytes"])))
        elif cur:
            print("The current default is the cheapest candidate over this corpus.")
    print()
    print("A LIMIT WORTH STATING: this is one corpus, and the optimum is a property of how")
    print("many bytes a turn takes in it. A deployment whose turns are much smaller or much")
    print("larger should re-run this rather than inherit the number -- which is why")
    print("read_window() takes first_chunk as a parameter at all.")


def self_test():
    """Controls. What these must prove:

      1. The harness can MEASURE A SAVING when one exists -- on a synthetic file with many
         more turns than the window, the window must read strictly fewer bytes.
      2. The harness can MEASURE NO SAVING when none exists -- on a file shorter than the
         window, the ratio must be ~1.0 and must not be reported as a win.
      3. THE AGREEMENT CHECK CAN FAIL. A harness whose `agree` is always True would hide
         exactly the defect blind mode 3 exists for, so it is fed a deliberately wrong
         window and must mark it MISMATCH.
      4. The refusal on an empty corpus really refuses.
    """
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

    print("BENCH_WINDOW_READ SELF-TEST")
    tmp = tempfile.mkdtemp(prefix="ari-bench-")

    # Bigger than flags.FIRST_CHUNK on purpose: a fixture that fits inside the first tail
    # read is read whole by the window, so it cannot demonstrate a saving and the first
    # version of this control failed on exactly that. And DISTINCT calls per turn, not
    # `_repeat_lines`, because control 3 tampers with a turn and identical turns make
    # tampering undetectable -- the second way the first version of these controls was
    # measuring nothing.
    big = os.path.join(tmp, "big.jsonl")
    with open(big, "w") as fh:
        for ln in FL._content_run_lines(4000):
            fh.write(ln + "\n")
    check(os.path.getsize(big) > FL.FIRST_CHUNK,
          "the fixture (%d bytes) is larger than FIRST_CHUNK (%d), so a window CAN read "
          "less than all of it" % (os.path.getsize(big), FL.FIRST_CHUNK))
    r = measure_file(big, last_turns=10, reps=2)
    check(r["turns_in_file"] == 4000 and r["window_turns"] == 10,
          "a 4000-turn file windowed to 10 turns is seen as 4000 and 10")
    check(r["win_bytes"] < r["full_bytes"] and r["byte_ratio"] < 0.5,
          "control 1: the window read %d of %d bytes (ratio %.3f) -- a saving is "
          "MEASURABLE by this harness" % (r["win_bytes"], r["full_bytes"],
                                          r["byte_ratio"]))
    check(r["res_bytes"] <= r["win_bytes"],
          "the resume read (%d bytes) costs no more than the window read (%d)"
          % (r["res_bytes"], r["win_bytes"]))
    check(r["agree"],
          "and the window AGREES with the full read on the turns they share")

    small = os.path.join(tmp, "small.jsonl")
    with open(small, "w") as fh:
        for ln in FL._repeat_lines(5):
            fh.write(ln + "\n")
    r2 = measure_file(small, last_turns=40, reps=2)
    check(r2["byte_ratio"] == 1.0 and r2["turns_in_file"] == 5,
          "control 2: a 5-turn file windowed to 40 turns reads ratio %.3f -- no saving, "
          "reported as no saving" % r2["byte_ratio"])

    # control 3: the agreement check must be able to say NO.
    truth = FL.read_window(big)
    wrong = FL.read_window(big, last_turns=10)
    wrong["turns"] = wrong["turns"][:-1] + [truth["turns"][0]]
    n = wrong["window"]["turns"]
    check(turn_keys(wrong) != turn_keys(truth)[-n:],
          "control 3: the agreement check REJECTS a window whose turns were tampered "
          "with, so a silently-full or silently-wrong reader would be caught")

    # control 8: a resumed transcript must be labelled RESIDUAL, not MISMATCH.
    res_lines = (FL._repeat_lines(12) + FL._content_run_lines(6)
                 + [FL._mk("rm0", [FL._tu("zz1", "Bash", dict(command="echo resumed"))],
                           parent="gr5", uuid="za9")])
    p_res = os.path.join(tmp, "resumed.jsonl")
    with open(p_res, "w") as fh:
        for ln in res_lines:
            fh.write(ln + "\n")
    rr = measure_file(p_res, last_turns=2, reps=1)
    check(rr["resumed"] is True and rr["agree"] == "residual",
          "a transcript replaying a message.id is labelled RESIDUAL, not MISMATCH -- the "
          "slot reserved for an UNEXPLAINED read-path disagreement stays empty")
    # control 8b (review R3): sidechain INTERLEAVING is not a replay. One main turn whose
    # entries are split by a sidechain turn's entries used to be labelled resumed.
    inter = [
        FL._mk("mMAIN", [FL._tu("ic0", "Bash", dict(command="echo main"))], uuid="iu0"),
        FL._mk("mSIDE", [FL._tu("ic1", "Read", dict(file_path="/s"))], uuid="iu1",
               parent="iu0", sidechain=True),
        FL._res("iu2", "iu1", "ic1", "side done"),
        FL._mk("mMAIN", [FL._tu("ic2", "Bash", dict(command="echo main again"))],
               uuid="iu3", parent="iu0"),
        FL._res("iu4", "iu3", "ic2", "main done"),
    ]
    p_int = os.path.join(tmp, "interleaved.jsonl")
    with open(p_int, "w") as fh:
        for ln in inter:
            fh.write(ln + "\n")
    occ_all = FL._turn_occurrences([l for l in inter])
    check(len(occ_all) != len({m for _i, m in occ_all}),
          "control 8b: the fixture DOES look replayed to a whole-file count (runs %s) -- "
          "so this control can fail" % ([m for _i, m in occ_all],))
    check(measure_file(p_int, last_turns=1, reps=1)["resumed"] is False,
          "but per stream nothing is replayed, so it is NOT labelled resumed: RESIDUAL "
          "keeps meaning 'the one disagreement we accounted for', not 'had a subagent'")
    m_only, s_only = _streams(inter)
    check(not _replays_an_id(m_only) and not _replays_an_id(s_only)
          and (len(m_only), len(s_only)) == (4, 1),
          "the split puts %d entries in main and %d in sidechain, neither replaying -- the "
          "sidechain's own tool_result sorts into MAIN, because only an assistant entry "
          "carries isSidechain. Harmless: a tool_result carries no turn id either, so it "
          "cannot make or break a replay" % (len(m_only), len(s_only)))

    check(measure_file(big, last_turns=10, reps=1)["resumed"] is False,
          "and a transcript that does not replay one is not labelled resumed, so the "
          "detector is not simply always true")

    # control 7: discovery must not count one session twice because two account roots hold
    # a copy of it. Review found 4 such pairs in the real corpus, which weights the median
    # by directory layout rather than by sessions.
    da, db = os.path.join(tmp, "ra"), os.path.join(tmp, "rb")
    for d in (da, db):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "same-session.jsonl"), "w") as fh:
            for ln in FL._repeat_lines(6):
                fh.write(ln + "\n")
    rows_d, _m, dupes_d = discover([da, db], 1)
    check(len(rows_d) == 1 and dupes_d == 1,
          "the same session under two roots is measured ONCE (%d row, %d duplicate "
          "reported, not hidden)" % (len(rows_d), dupes_d))

    # control 5: a file that grows mid-measurement must come back as "no answer", not as a
    # MISMATCH. Forced deterministically by appending from inside the timed section.
    grow = os.path.join(tmp, "grow.jsonl")
    with open(grow, "w") as fh:
        for ln in FL._content_run_lines(80):
            fh.write(ln + "\n")
    real_read_window = FL.read_window
    state = {"n": 0}

    def growing_read(*a, **k):
        state["n"] += 1
        if state["n"] == 2:                      # between the full read and the window read
            with open(grow, "a") as fh:
                for ln in FL._repeat_lines(2):
                    fh.write(ln + "\n")
        return real_read_window(*a, **k)

    FL.read_window = growing_read
    try:
        rg = measure_file(grow, last_turns=5, reps=1)
    finally:
        FL.read_window = real_read_window
    check(rg["grew_during"] is True and rg["agree"] is None,
          "control 5: a file that grew mid-measurement (%d -> %d bytes) reports agree=None "
          "-- no answer -- rather than MISMATCH"
          % (rg["size"], rg["size_after"]))
    # A FIXED input, not the mktemp path. Two reasons, and the second is why this control
    # was rewritten: a known input/output pair is a stronger assertion than "it starts with
    # sess-", AND hashing a temp directory made this control's own output differ on every
    # run, which the fixture-freshness gate (ARI#22) caught immediately -- a published
    # sample whose bytes change per run can never be verified against the code.
    FIXED = "/tmp/example-corpus/session.jsonl"
    check(tag(FIXED) == "sess-" + __import__("hashlib").sha256(
              FIXED.encode()).hexdigest()[:8]
          and tag(grow) != grow and len(tag(grow)) == 13,
          "control 6: a path is reported as an 8-hex sess- tag and never as the path "
          "itself (fixed input %r -> %s)" % (FIXED, tag(FIXED)))

    rows, missing, _d = discover([os.path.join(tmp, "nope")], 1)
    check(rows == [] and missing,
          "control 4: a ROOTS that does not exist yields no rows and is reported missing, "
          "which is what main() refuses on")

    print()
    if fail == 0:
        print("SELF-TEST PASSED -- %d checks: a saving is measurable, no saving is reported "
              "as none," % ok)
        print("the agreement check can fail, a file growing mid-measurement is reported as")
        print("no-answer rather than as a mismatch, paths are tagged not printed, and an")
        print("absent corpus is detected.")
        return 0
    print("SELF-TEST FAILED -- %d of %d checks" % (fail, ok + fail), file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Measure the windowed read path in flags.py against a full-file parse, "
                    "on real transcripts (read-only).",
        epilog="Env: ROOTS (default %r). Positional paths override discovery entirely.\n"
               "Run --help-blind for this instrument's own failure modes."
               % DEFAULT_ROOTS,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcripts", nargs="*",
                    help="measure these paths instead of discovering a corpus")
    ap.add_argument("--last-turns", type=int, default=40, metavar="N",
                    help="window size to measure (default 40)")
    ap.add_argument("--reps", type=int, default=5, metavar="N",
                    help="timed repetitions per read path (default 5; the median is "
                         "reported, with min and max in --json)")
    ap.add_argument("--top", type=int, default=8, metavar="N",
                    help="how many of the largest discovered transcripts to measure "
                         "(default 8)")
    ap.add_argument("--min-bytes", type=int, default=512 * 1024, metavar="N",
                    help="ignore transcripts smaller than this when discovering "
                         "(default 524288)")
    ap.add_argument("--sweep-first-chunk", action="store_true",
                    help="instead of the full/window comparison, measure what flags.py's "
                         "FIRST_CHUNK costs over this corpus -- the table the default was "
                         "chosen from")
    ap.add_argument("--json", action="store_true", help="emit the per-file rows as JSON")
    ap.add_argument("--self-test", action="store_true",
                    help="run the controls and exit")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args(argv)

    if args.help_blind:
        print(__doc__)
        return 0
    if args.self_test:
        return self_test()
    if args.last_turns < 1:
        print("REFUSING: --last-turns %d is below 1." % args.last_turns, file=sys.stderr)
        return 2
    if args.reps < 1:
        print("REFUSING: --reps %d is below 1. A timing needs a run." % args.reps,
              file=sys.stderr)
        return 2

    missing, skipped = [], 0
    if args.transcripts:
        paths = []
        for p in args.transcripts:
            if os.path.isfile(p):
                paths.append(p)
            else:
                missing.append(p)
        roots = ["(explicit paths)"]
        dupes = 0
    else:
        roots = [os.path.expanduser(r.strip()) for r
                 in os.environ.get("ROOTS", DEFAULT_ROOTS).split(":") if r.strip()]
        found, missing, dupes = discover(roots, args.min_bytes)
        skipped = max(0, len(found) - args.top)
        paths = [p for _sz, p in found[:args.top]]

    if not paths:
        print("transcripts measured: 0", file=sys.stderr)
        if missing:
            print("ROOTS NOT FOUND: %s" % ", ".join(missing), file=sys.stderr)
        print("REFUSING (exit 2): a speedup averaged over no files is 1.00, and that "
              "number would look like a finding. Set ROOTS to a directory of .jsonl "
              "transcripts, lower --min-bytes, or pass paths. See --help-blind, blind "
              "mode 1.", file=sys.stderr)
        return 2

    if args.sweep_first_chunk:
        srows = sweep(paths, args.last_turns,
                      [64 * 1024, 128 * 1024, 256 * 1024, 512 * 1024,
                       1024 * 1024, 2048 * 1024, 4096 * 1024])
        if args.json:
            print(json.dumps(dict(last_turns=args.last_turns, current=FL.FIRST_CHUNK,
                                  rows=srows), indent=2))
        else:
            print_sweep(srows, args.last_turns, FL.FIRST_CHUNK)
        return 0

    rows = []
    for p in paths:
        try:
            rows.append(measure_file(p, args.last_turns, args.reps))
        except OSError:
            missing.append(p)

    if not rows:
        print("REFUSING (exit 2): every candidate failed to read. See --help-blind, blind "
              "mode 1.", file=sys.stderr)
        return 2

    if args.json:
        # the path is replaced by its tag, for the same reason the text table shows a tag
        safe = [dict(r, path=tag(r["path"])) for r in rows]
        print(json.dumps(dict(last_turns=args.last_turns, reps=args.reps,
                              roots=["(not shown: a root is a path)"],
                              missing=[tag(m) for m in missing], rows=safe), indent=2))
    else:
        human(rows, args.last_turns, args.reps, roots, missing, args.min_bytes, skipped,
              dupes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
