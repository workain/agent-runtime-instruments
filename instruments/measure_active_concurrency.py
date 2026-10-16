#!/usr/bin/env python3
"""How many agent sessions are actually MID-TURN at once, versus how many hold a process?

That ratio is the entire payoff of a turn-scoped worker pool: a resident-session model
pays for the former and provisions for the latter. If they are close, pooling buys
nothing; if the active count is a small fraction of the resident count, the resident
model is paying for a floor that never drops.

ACTIVE is measured from the on-disk transcripts, which are the only per-turn record an
agent CLI leaves behind: a session owns minute M if it wrote >=1 transcript entry in M.
Short gaps are then filled, because a single thinking block or a slow tool writes once
at its END and a raw per-minute count therefore under-reports mid-turn time. Both the
raw and the gap-filled series are printed, because the gap fill is an ASSUMPTION and
the reader is entitled to see what it changed. (Where this was first run it changed the
mean by 0.04 and moved no percentile at all -- i.e. the reading did not depend on it.)

RESIDENT is deliberately NOT taken from this corpus. A transcript cannot record the
thing that costs money, which is a session sitting open holding a live process while
nothing is written. Use `measure_session_footprint.py` for that; it reads /proc.

Read-only: opens transcript files for reading only. Never writes, never deletes.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    `mean=0.00`, `busy_minutes=0`, and every percentile 0 -- which is also exactly what
    a genuinely idle fleet prints. This is the most dangerous instrument in the set for
    that reason: its blind output is a *publishable-looking* number.

    Three ways to be blind, and the guard for each:

      1. ROOTS points at nothing. Then `transcripts found: 0`. The run REFUSES (exit 2)
         rather than printing a distribution over an empty corpus, because "mean 0.43"
         and "mean 0.00 because I looked in the wrong directory" must not be the same
         shaped answer.
      2. ROOTS is right but the files are not transcripts, or use a timestamp field this
         does not parse. Then `files with no parseable timestamp` is non-zero and large.
         That counter is printed on every run, and the run REFUSES (exit 2) if NO file
         in the corpus yielded a single timestamp.
      3. DAYS is shorter than the quiet period, so every file is mtime-skipped. The
         `older than Nd skipped` count is printed next to the scanned count, so a
         corpus that was 99% skipped is visible rather than implied.

    The denominator is also a place to be wrong in the other direction: minutes with
    zero active sessions are REAL and are counted, so the horizon is DAYS*24*60 rather
    than the number of busy minutes. Dividing only by busy minutes would turn
    "mean 0.43 sessions" into "mean 2.06" and make the floor disappear.
"""
import argparse
import collections
import datetime
import hashlib
import os
import re
import sys


# the one shared module in this repo: the engine adapter table (instruments/engines.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import engines
except ImportError:                                  # pragma: no cover
    engines = None

# Default roots come from the engine adapter table, so this instrument learns about a new
# engine when engines.py does. Point ROOTS at your own if your harness relocates them
# (colon-separated, ~ expanded).
DEFAULT_ROOTS = (":".join(engines.transcript_roots()) if engines
                 else "~/.claude/projects:~/.codex/sessions")

TS = re.compile(rb'"timestamp"\s*:\s*"([0-9T:\-\.]+Z)"')


def parse_ts(raw):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(raw, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Active (mid-turn) vs resident agent-session concurrency, from transcripts.",
        epilog=f"Env: ROOTS (default {DEFAULT_ROOTS!r}), DAYS (7), GAP_FILL_MIN (3). "
               f"Run --help-blind for this instrument's own failure modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anonymise", action="store_true",
                    help="hash session keys in the per-session listing (for published output)")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args()
    if args.help_blind:
        print(__doc__)
        return 0

    if engines:
        print(engines.provenance_banner())
        print()
    roots = [os.path.expanduser(r) for r in
             os.environ.get("ROOTS", DEFAULT_ROOTS).split(":") if r.strip()]
    days = int(os.environ.get("DAYS", "7"))
    gap_fill = int(os.environ.get("GAP_FILL_MIN", "3"))
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

    files, missing_roots = [], []
    for root in roots:
        if not os.path.isdir(root):
            missing_roots.append(root)
            continue
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n.endswith(".jsonl"):
                    files.append(os.path.join(dirpath, n))

    def rootlabel(r):
        if not args.anonymise:
            return r
        # keep the meaningful tail, drop the site-specific prefix
        return "<redacted>/" + "/".join(r.rstrip("/").split("/")[-2:])
    print(f"roots: {', '.join(rootlabel(r) for r in roots)}")
    if missing_roots:
        print(f"ROOTS NOT FOUND (skipped): "
              f"{', '.join(rootlabel(r) for r in missing_roots)}")
    print(f"transcripts found: {len(files)}")
    if not files:
        print("REFUSING to report a distribution over an empty corpus. Set ROOTS to the "
              "directory your agent writes .jsonl transcripts into. See --help-blind.",
              file=sys.stderr)
        return 2

    # one "session" = one containing directory, not one .jsonl: a resume/fork writes a
    # new transcript file for the same session, and counting files would inflate the
    # session count without changing the minute coverage.
    per_session = collections.defaultdict(set)
    scanned = skipped = lines = no_ts_files = 0
    for path in files:
        try:
            if datetime.datetime.fromtimestamp(os.path.getmtime(path),
                                               datetime.timezone.utc) < cutoff:
                skipped += 1
                continue
        except OSError:
            skipped += 1
            continue
        key = os.path.basename(os.path.dirname(path))
        scanned += 1
        got_any = False
        try:
            with open(path, "rb") as fh:
                for line in fh:
                    lines += 1
                    m = TS.search(line)
                    if not m:
                        continue
                    t = parse_ts(m.group(1).decode())
                    if t is None:
                        continue
                    got_any = True
                    if t < cutoff:
                        continue
                    per_session[key].add(int(t.timestamp()) // 60)
        except OSError:
            skipped += 1
            continue
        if not got_any:
            no_ts_files += 1

    print(f"corpus: {scanned} transcripts scanned, {skipped} older than {days}d or "
          f"unreadable, {lines} lines")
    print(f"files with no parseable timestamp: {no_ts_files}/{scanned}")
    if scanned and no_ts_files == scanned:
        print("REFUSING: not one scanned file yielded a parseable timestamp, so a zero "
              "here would mean 'I cannot read this format', not 'nothing was active'. "
              "See --help-blind.", file=sys.stderr)
        return 2
    print(f"sessions with any activity in {days}d: {len(per_session)}")

    def concurrency(sessions_minutes):
        c = collections.Counter()
        for mins in sessions_minutes.values():
            for m in mins:
                c[m] += 1
        return c

    def filled(mins):
        out, s = set(mins), sorted(mins)
        for a, b in zip(s, s[1:]):
            if 1 < b - a <= gap_fill + 1:
                out.update(range(a + 1, b))
        return out

    def report(label, counter, horizon_minutes):
        if not counter:
            print(f"{label}: no active minutes in window")
            return
        vals = sorted(counter.values())
        busy = len(vals)
        # minutes with ZERO active sessions are real and belong in the denominator
        allv = [0] * max(0, horizon_minutes - busy) + vals
        n = len(allv)
        p = lambda q: allv[min(n - 1, int(n * q))]
        print(f"{label}: p50={p(.5)} p90={p(.9)} p99={p(.99)} max={max(vals)} "
              f"mean={sum(allv)/n:.2f}  busy_minutes={busy}/{horizon_minutes} "
              f"({100*busy/horizon_minutes:.0f}%)")

    horizon = days * 24 * 60
    report("ACTIVE sessions/min (raw)      ", concurrency(per_session), horizon)
    report(f"ACTIVE sessions/min (gap<={gap_fill}m)  ",
           concurrency({k: filled(v) for k, v in per_session.items()}), horizon)

    tot = sum(len(v) for v in per_session.values())
    print(f"\ntotal session-ACTIVE-minutes in window: {tot} ({tot/60:.0f} session-hours "
          f"in a {horizon/60:.0f}-hour window)")
    print("per-session active minutes, top 10:")
    for k, v in sorted(per_session.items(), key=lambda kv: -len(kv[1]))[:10]:
        shown = ("key-" + hashlib.sha256(k.encode()).hexdigest()[:8]) if args.anonymise \
            else k[-60:]
        print(f"  {len(v):6}  {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
