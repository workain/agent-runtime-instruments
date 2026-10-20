"""Revert each fix this branch made; assert a control turns RED.

Driven by scripts/test_flags_controls.sh -- see that file's header for why this exists.
Run it directly or through the wrapper; both take --list and --only NAME.

THE RULE THIS ENCODES: a fix ships with the input that distinguishes it from the code it
replaced, or it is not protected. Round 2 of the ARI#24 review found 13 of 53 mutants green
because the controls pinned the reviewer's findings instead of the fixes -- four of six
changed behaviours were revertible with the suite green, two of them losing data. Every
mutation below is a REVERT to code that was really there, not arbitrary damage.

Two deliberate choices, both lessons from that review:

  * Mutations are applied to a COPY of instruments/ in a temp directory. The real tree is
    never written, so an interrupted run cannot leave a mutant behind.
  * Every mutation asserts its anchor matched exactly once BEFORE running anything. A
    `str.replace` that silently matches nothing reports "equivalent" for a mutation that
    never happened -- which is how a probe that cannot fail hides a control that cannot
    fail, one level further out. That failure happened for real in round 1.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "instruments")

# (name, module, why this behaviour exists, old, new)
CASES = [
    ("N3-splitlines", "flags",
     "splitting decoded text instead of raw bytes: str.splitlines() also splits on \\r, "
     "\\v, \\f, U+2028 and U+2029, which occur INSIDE JSON strings. LOSES A TURN.",
     '    parts = raw.split(b"\\n")',
     '    parts = [s.encode("utf-8", "surrogateescape")\n'
     '             for s in raw.decode("utf-8", "replace").splitlines(True)]\n'
     '    parts = [p.rstrip(b"\\r\\n") for p in parts] or [b""]'),

    ("N4-char-offsets", "flags",
     "counting characters where bytes are meant: a multibyte line moves every later "
     "offset, so start_offset points into the middle of a line.",
     "        pos += len(chunk) + 1",
     '        pos += len(chunk.decode("utf-8", "replace")) + 1'),

    ("N5-drop-final-line", "flags",
     "popping the last element unconditionally: a transcript with no trailing newline -- "
     "which is what a LIVE session always has -- loses its newest turn.",
     '    if parts and parts[-1] == b"" and raw.endswith(b"\\n"):\n        parts.pop()',
     "    if parts:\n        parts.pop()"),

    ("R2-phantom-empty-line", "flags",
     "b\"\".split(b\"\\n\") is [b\"\"], so an empty read reported one line of nothing -- "
     "contradicting --help-blind mode 7's own promise of an empty window.",
     "    if not raw:\n        return lines, offsets"
     "            # see below: NO raw bytes is NO lines, not one empty",
     "    if False:\n        return lines, offsets"),

    ("N6-identity-cut", "flags",
     "cutting the window at the first line carrying any kept id: on a resumed transcript "
     "the newest ids' FIRST appearance is far earlier, so last_turns=N returns more than "
     "N while still reporting head_complete=True.",
     "    occ = []\n    for i, mid in _assistant_mids(lines):\n"
     "        if occ and occ[-1][1] == mid:\n            continue\n"
     "        occ.append((i, mid))\n    return occ",
     "    occ, seen = [], set()\n    for i, mid in _assistant_mids(lines):\n"
     "        if mid in seen:\n            continue\n        seen.add(mid)\n"
     "        occ.append((i, mid))\n    return occ"),

    ("N9-no-lookback", "flags",
     "dropping the one-byte look-back: a read position landing exactly on a line start "
     "cannot be told from a mid-line one, so the loop falls back to reading the whole file.",
     "            new_probe = max(0, want - 1) if want > 0 else 0",
     "            new_probe = want"),

    ("N10-no-span-reset", "flags",
     "letting a CONTENT run cross a real user turn: not the unit alm#833 measured or "
     "priced, so agent_reuse_gap recommends one subagent for two unrelated briefs.",
     '        if t["span"] != prev_span:\n'
     "            cur = 0                     # a real user turn intervened: new brief, new run\n"
     '            prev_span = t["span"]',
     '        if False:\n            cur = 0\n            prev_span = t["span"]'),

    ("N11-span-reset-no-op", "flags",
     "the same behaviour broken a second way -- the branch still taken, but the counter "
     "not actually reset. Included because one site, two spellings: a control that only "
     "catches the `if False` form is pinning the syntax, not the behaviour.",
     "            cur = 0                     # a real user turn intervened: new brief, new run",
     "            cur = cur                   # a real user turn intervened: new brief, new run"),

    ("N12-no-mcp-strip", "flags",
     "publishing the MCP server segment: the F4 leak, straight back.",
     '    short = _strip_mcp(label)\n    return ("mcp:" + short) if short != label else label',
     '    short = label\n    return ("mcp:" + short) if short != label else label'),

    ("N13-strip-overreach", "flags",
     "rewriting a non-MCP label too: `Read` becomes `mcp:Read`, so the published "
     "vocabulary stops meaning anything.",
     '    return ("mcp:" + short) if short != label else label',
     '    return "mcp:" + short'),

    ("N17-span-rows-bypass", "flags",
     "a PARTIAL bypass -- only the in-span evidence rows skipping public_label. The one "
     "that matters most: a control looking at the wrong row kind sees nothing wrong.",
     '        rows.append(dict(window="span", thread=None, span=idx,\n'
     '                         label=public_label(lab), fingerprint=sig[:8], count=n))',
     '        rows.append(dict(window="span", thread=None, span=idx,\n'
     "                         label=lab, fingerprint=sig[:8], count=n))"),

    ("N2-start-offset-early", "flags",
     "start_offset naming the line BEFORE the window's first kept line: a resumed window "
     "silently gains a turn.",
     '    win["start_offset"] = offsets[cut] if cut < len(offsets) else size',
     '    win["start_offset"] = offsets[max(0, cut - 1)] if cut < len(offsets) else size'),

    ("R3-resumed-whole-file", "bench_window_read",
     "counting replayed ids across BOTH streams: a main turn whose entries are split by a "
     "sidechain turn's reads as replayed, so a real read-path defect surfacing on such a "
     "transcript is filed as the known residual and stops being visible.",
     '    resumed = any(_replays_an_id(ls) for ls in _streams(full["lines"]))',
     '    resumed = _replays_an_id(full["lines"])'),
]


def run_selftest(tree, module):
    p = subprocess.run([sys.executable, os.path.join(tree, "instruments", module + ".py"),
                        "--self-test"], capture_output=True, text=True)
    fails = [l.strip() for l in (p.stdout + p.stderr).splitlines()
             if l.strip().startswith("FAIL")]
    return p.returncode, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args()

    if a.list:
        for name, mod, why, _o, _n in CASES:
            print("%-24s %-18s %s" % (name, mod, why.split(":")[0]))
        return 0

    cases = [c for c in CASES if not a.only or c[0] == a.only]
    if not cases:
        print("no such case: %s" % a.only)
        return 2

    src = open(os.path.join(SRC, "flags.py"), encoding="utf-8").read()
    print("REVERT-EACH-FIX GATE -- %d cases" % len(cases))
    print("Each reverts one fix on this branch; a control must turn RED.")
    print()

    tmp = tempfile.mkdtemp(prefix="flagsmut.")
    gaps, errors = [], []
    try:
        base = os.path.join(tmp, "base")
        shutil.copytree(SRC, os.path.join(base, "instruments"))
        rc, fails = run_selftest(base, "flags")
        if rc != 0:
            print("ABORT: the UNMUTATED copy already fails -- nothing below would mean "
                  "anything.\n  %s" % "\n  ".join(fails))
            return 2
        print("baseline: the unmutated copy PASSES both self-tests, so a RED below is "
              "caused by the mutation")
        rc_b, _ = run_selftest(base, "bench_window_read")
        if rc_b != 0:
            print("ABORT: bench self-test fails unmutated")
            return 2
        print()

        for name, mod, why, old, new in cases:
            path = os.path.join(SRC, mod + ".py")
            text = open(path, encoding="utf-8").read()
            n = text.count(old)
            if n != 1:
                errors.append((name, "anchor matched %d times, not 1" % n))
                print("%-24s ANCHOR LOST (%d matches) -- the mutation never happened, "
                      "which is NOT a pass" % (name, n))
                continue
            work = os.path.join(tmp, name)
            shutil.copytree(SRC, os.path.join(work, "instruments"))
            mpath = os.path.join(work, "instruments", mod + ".py")
            mutant = text.replace(old, new, 1)
            assert mutant != text
            with open(mpath, "w", encoding="utf-8") as fh:
                fh.write(mutant)
            rc, fails = run_selftest(work, mod)
            if rc != 0 and fails:
                print("%-24s RED   -- %s" % (name, fails[0][:96]))
            elif rc != 0:
                print("%-24s RED   -- exit %d (no FAIL line; a raise, which still counts)"
                      % (name, rc))
            else:
                gaps.append((name, why))
                print("%-24s GREEN -- COVERAGE GAP: %s" % (name, why))
            shutil.rmtree(work, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if errors:
        print("%d anchor(s) no longer match. Update this file: a case that cannot be "
              "applied is not a case that passed." % len(errors))
    if gaps:
        print("FAILED -- %d of %d fixes are unprotected:" % (len(gaps), len(cases)))
        for name, why in gaps:
            print("  %s: reverting it changes nothing the self-test can see" % name)
        return 1
    if errors:
        return 1
    print("PASSED -- all %d fixes are pinned: reverting any one of them turns a control "
          "RED." % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
