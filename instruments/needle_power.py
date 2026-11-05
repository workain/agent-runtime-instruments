#!/usr/bin/env python3
"""Is a marker-grep a valid oracle for "my instructions are loaded" in THIS corpus?

It answers one question and refuses to answer a second one that looks identical. Before you
trust a grep for a phrase from your rules file as evidence that an agent LOADED that file,
this measures how many files the agent can ALREADY SEE that contain the same phrase. That
count is the false-positive population, and if it is not zero the grep is not an oracle.

WHY IT EXISTS. In the study this came from, a marker-grep oracle was built twice and was
invalid both times, in OPPOSITE directions, and each invalid version produced a confident,
plausible number:

  * FALSE NEGATIVES -- one client does not store the injected rules text in its transcript in
    any greppable form, so every arm came back "absent", with and without the harness alike.
    Read at face value that invalidates a whole column of results.
  * FALSE POSITIVES -- every candidate phrase from the rules file also appeared in dozens of
    files of the repository under test, which every arm carried in its own clone. One arm duly
    "hit" the needle. The hit was the arm reading a log file that QUOTED the phrase while
    describing the probe. The control shared a corpus with the thing it was controlling.
  * A later round produced 188 findings from this class, every single one a false positive.

Each cost about an hour of a live, paid sweep plus one nearly-published wrong column. This
check costs nothing and runs in seconds.

WHAT THE OUTPUT MEANS. Per needle, the files containing it, split into the harness surface
(what you are trying to detect) and everything else (the false-positive population), then one
of three verdicts:

  survives        in the harness surface and nowhere else. NOT the same as valid -- see below.
  INVALID         also present in files the agent can see. Cannot tell "loaded" from "read".
  NOT-IN-HARNESS  absent from the harness surface, so it detects nothing at all.

`--auto` asks the stronger question: is there ANY line of the rules file unique to it within
this corpus? If the answer is none, no marker-grep oracle exists here at all and the only
valid instrument is a behavioural probe. On the repository this was built against the answer
was five unique lines out of 256 candidates, and the five phrases a human would reach for first
each appeared in dozens of non-harness files.

HOW THIS INSTRUMENT LIES, in its own words:

  1. "survives" is NOT validity. It clears the false-positive half only. The false-NEGATIVE
     half is a property of your client's record-keeping and is INVISIBLE from any corpus scan.
     Corroborate with a behavioural probe before publishing either way.
  2. It greps FILES ON DISK. A phrase can also reach an agent through a route no file in the
     root carries: a sibling worktree outside it, git history (deleted-but-reachable blobs), a
     session transcript store, another agent's message. None of those is visible here.
  3. "harness" is whatever --harness names. Get that list wrong and the split is wrong, and
     nothing in this instrument can tell you that you did -- it is an input, not a finding.
  4. --auto samples candidate lines (default 120) unless --auto-sample 0. A sampled run can
     miss a unique line that exists.
  5. Its own answer DECAYS IF YOU PUBLISH IT. --auto-show-lines prints the unique lines' text;
     committing that output inside the corpus makes every line it names stop being unique. That
     is why the text is off by default, and it already happened once.

EXIT CODES. 0 every needle survived the false-positive check; 1 at least one is unusable as a
harness-loaded oracle; non-zero with REFUSING on stdout/stderr when it cannot measure at all
(no such root, nothing asked for). It never reports a clean result it did not establish.
"""
import argparse
import hashlib
import os
import subprocess
import sys

DEFAULT_HARNESS = ["CLAUDE.md", "AGENTS.md", ".hermes.md", ".mcp.json",
                   ".claude/CLAUDE.md", ".claude/settings.json"]


def grep_files(root, needle):
    """Files under root containing needle, as a fixed string. Returns a sorted list.

    -r -l -F: recursive, names only, FIXED string. Fixed is deliberate -- a needle lifted out of
    a real instruction file is full of regex metacharacters (`*`, `(`, `.`, `|`), and treating it
    as a pattern silently changes what is being counted.

    `-e needle` rather than a bare positional, and this was found by running it rather than by
    reading it: real instruction files are markdown, so a great many of their lines START WITH
    `-`, and a bare positional made grep parse the needle as an option bundle. It exited 2 with
    "invalid option", which the rc>=2 guard below turned into a loud refusal -- had that guard
    said `rc != 0 -> []` instead, every hyphen-leading needle would have scored a confident
    clean 0 non-harness files and been reported as a VALID oracle. The bug and the guard that
    caught it are the same lesson from both ends.
    """
    cmd = ["grep", "-rlF", "--exclude-dir=.git", "-e", needle, root]
    p = subprocess.run(cmd, capture_output=True, text=True)
    # grep: 0 = found, 1 = not found, >=2 = real error. A real error must never read as "none".
    if p.returncode >= 2:
        raise SystemExit(f"REFUSING: grep failed (rc={p.returncode}) on needle "
                         f"{needle[:40]!r}: {p.stderr.strip()[:200]}")
    return sorted(f for f in p.stdout.splitlines() if f)


def is_harness(path, root, harness_names):
    rel = os.path.relpath(path, root)
    return any(rel == h or rel.endswith("/" + h) for h in harness_names)


def unique_lines(root, harness_file, harness_names, minlen, limit):
    """Lines of harness_file that appear in NO other file under root.

    This is the strong question: does a discriminating marker-grep oracle exist here at all?

    Candidates and findings carry their 1-based line number in harness_file, so a finding can be
    NAMED (file:Lnnn + sha256) instead of quoted -- see the docstring on why quoting it is what
    made the published figure decay.
    """
    path = os.path.join(root, harness_file)
    if not os.path.isfile(path):
        raise SystemExit(f"REFUSING: --auto needs a readable harness file; {path} is not one.")
    lines = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh, 1):
            s = raw.strip()
            if len(s) >= minlen:
                lines.append((n, s))
    # Dedupe while keeping order, so a file that repeats a line does not inflate the scan. The
    # FIRST occurrence's line number is kept; a repeated line is one candidate, as before.
    seen, cand = set(), []
    for n, s in lines:
        if s not in seen:
            seen.add(s)
            cand.append((n, s))
    if limit and len(cand) > limit:
        step = len(cand) / float(limit)
        cand = [cand[int(i * step)] for i in range(limit)]
    uniq = []
    for n, s in cand:
        hits = grep_files(root, s)
        others = [h for h in hits if not is_harness(h, root, harness_names)]
        if not others:
            uniq.append((n, s, len(hits)))
    return cand, uniq


def line_id(harness_file, lineno, s):
    """Name a finding without putting its text into the corpus.

    Re-derivable by hand, which is the point -- the identifier is checkable rather than trusted:

        sed -n '123p' CLAUDE.md | python3 -c \
          "import hashlib,sys; l=sys.stdin.read().strip(); \
           print(len(l), hashlib.sha256(l.encode()).hexdigest()[:12])"
    """
    return (f"{harness_file}:L{lineno} len={len(s)} "
            f"sha256:{hashlib.sha256(s.encode('utf-8')).hexdigest()[:12]}")



# ---------------------------------------------------------------------------------------------
# CONTROLS
# ---------------------------------------------------------------------------------------------
def _selftest():
    """Build a corpus whose answer is known by construction, then require each verdict.

    The three verdicts this instrument emits are not interchangeable and the difference is the
    whole point, so each gets a case whose correct answer is fixed by how the fixture is built
    rather than by what the code happens to return:

        survives        the needle is in the harness surface and NOWHERE else
        INVALID         the needle is in the harness surface AND in a file the arm can see
        NOT-IN-HARNESS  the needle is not in the harness surface at all, so it detects nothing

    Leg 4 is the one that makes this a control rather than a demonstration: it runs the SAME
    needle twice, once with the non-harness copy present and once with it removed, and requires
    the verdict to CHANGE. A check that only ever sees one fixture cannot tell a discriminating
    oracle from a constant -- that defect shipped in the study this came from, where a control
    shared a corpus with the thing it was controlling.
    """
    import shutil
    import subprocess
    import tempfile

    fails = []

    def check(label, got, want):
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: got {got!r}, want {want!r}")
        if not ok:
            fails.append(label)

    root = tempfile.mkdtemp(prefix="needle_power_selftest_")
    try:
        uniq_line = "a line that exists only in the harness surface of this fixture"
        shared_line = "a line the harness shares with an ordinary file in this corpus"
        with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(uniq_line + "\n" + shared_line + "\n")
        os.makedirs(os.path.join(root, "notes"), exist_ok=True)
        shared_copy = os.path.join(root, "notes", "quoting_doc.md")
        with open(shared_copy, "w", encoding="utf-8") as fh:
            fh.write("prose that happens to quote: " + shared_line + "\n")
        with open(os.path.join(root, "notes", "unrelated.md"), "w", encoding="utf-8") as fh:
            fh.write("nothing relevant here\n")

        names = ["CLAUDE.md"]

        def verdict(needle):
            hits = grep_files(root, needle)
            h = [x for x in hits if is_harness(x, root, names)]
            o = [x for x in hits if not is_harness(x, root, names)]
            if not h:
                return "NOT-IN-HARNESS"
            return "INVALID" if o else "survives"

        # 1-3. one case per verdict, each fixed by the fixture.
        check("leg1 harness-only needle", verdict(uniq_line), "survives")
        check("leg2 needle also in a non-harness file", verdict(shared_line), "INVALID")
        check("leg3 needle absent from the harness", verdict("string in no file at all"),
              "NOT-IN-HARNESS")

        # 4. DISCRIMINATION: remove the false-positive source and the same needle must flip.
        os.remove(shared_copy)
        check("leg4 same needle flips once its non-harness copy is gone",
              verdict(shared_line), "survives")
        with open(shared_copy, "w", encoding="utf-8") as fh:
            fh.write("prose that happens to quote: " + shared_line + "\n")

        # 5. --auto must find exactly the line unique to the harness file.
        cand, uniq = unique_lines(root, "CLAUDE.md", names, 10, 0)
        check("leg5 --auto candidate count", len(cand), 2)
        check("leg5 --auto unique-line count", len(uniq), 1)
        check("leg5 --auto picked the right line", uniq[0][1], uniq_line)

        # 6. EXIT CODES, end to end through the real CLI -- a verdict nobody can act on is not a
        #    result. An INVALID needle must exit 1; an all-clear must exit 0.
        me = os.path.abspath(__file__)
        r_bad = subprocess.run([sys.executable, me, "--root", root, "--needle", shared_line,
                                "--harness", "CLAUDE.md"], capture_output=True, text=True)
        check("leg6 INVALID needle exits 1", r_bad.returncode, 1)
        r_ok = subprocess.run([sys.executable, me, "--root", root, "--needle", uniq_line,
                               "--harness", "CLAUDE.md"], capture_output=True, text=True)
        check("leg6 surviving needle exits 0", r_ok.returncode, 0)

        # 7. CANNOT-MEASURE must be non-zero and must not look like a clean result.
        r_noroot = subprocess.run([sys.executable, me, "--root", os.path.join(root, "nope"),
                                   "--needle", "x"], capture_output=True, text=True)
        check("leg7 absent --root is refused non-zero", r_noroot.returncode != 0, True)
        check("leg7 absent --root says REFUSING",
              "REFUSING" in (r_noroot.stderr + r_noroot.stdout), True)
        r_nothing = subprocess.run([sys.executable, me, "--root", root],
                                   capture_output=True, text=True)
        check("leg7 nothing-to-measure is refused non-zero", r_nothing.returncode != 0, True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if fails:
        print(f"SELF-TEST FAILED: {len(fails)} leg(s): {', '.join(fails)}")
        return 1
    print("SELF-TEST PASSED -- 13 checks.")
    print("What it does NOT cover, so its silence is not read as a pass: the FALSE-NEGATIVE half.")
    print("Whether a client records the injected text in a greppable form at all is a property")
    print("of that client, invisible from any corpus scan, and no fixture here can establish it.")
    return 0




def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="corpus root the arm can see")
    ap.add_argument("--self-test", action="store_true",
                    help="run this instrument's own controls against a fixture whose answer is "
                         "known by construction, and exit non-zero if any leg fails")
    ap.add_argument("--help-blind", action="store_true",
                    help="what this instrument answers, and the five ways it lies")
    ap.add_argument("--needle", action="append", default=[],
                    help="a candidate marker, repeatable. Matched as a FIXED string.")
    ap.add_argument("--harness", action="append", default=None,
                    help=f"a harness-surface filename, repeatable. Default: {DEFAULT_HARNESS}")
    ap.add_argument("--auto", metavar="HARNESS_FILE",
                    help="also ask whether ANY line of this file is unique to it in the corpus")
    ap.add_argument("--auto-minlen", type=int, default=40,
                    help="shortest line --auto will consider a candidate (default 40)")
    ap.add_argument("--auto-sample", type=int, default=120,
                    help="cap on --auto candidates, evenly spaced (default 120; 0 = all)")
    ap.add_argument("--auto-show-lines", action="store_true",
                    help="print each unique line's TEXT as well as its id. Off by default "
                         "because committing that output inside the corpus un-finds every line "
                         "it names -- see this script's docstring.")
    a = ap.parse_args()

    if a.help_blind:
        # The MODULE DOCSTRING is the blind answer. One copy, deliberately: this
        # repository's figure gate reads the docstring as published prose, so a separate
        # constant would be a second copy that drifts out from under the gate.
        print(__doc__.strip())
        return 0
    if a.self_test:
        return _selftest()
    if not a.root:
        raise SystemExit("REFUSING: --root is required (or use --self-test / --help-blind).")

    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        raise SystemExit(f"REFUSING: --root is not a directory: {root}")
    if not a.needle and not a.auto:
        raise SystemExit("REFUSING: nothing to measure. Give --needle and/or --auto.")
    harness_names = a.harness if a.harness else list(DEFAULT_HARNESS)

    print(f"NEEDLE POWER -- root: {root}")
    print(f"harness surface (as given, check it rather than trust it): {harness_names}\n")

    invalid = 0
    if a.needle:
        print(f"{'files':>6s} {'harness':>8s} {'non-harn':>9s}  verdict        needle")
        for n in a.needle:
            hits = grep_files(root, n)
            h = [x for x in hits if is_harness(x, root, harness_names)]
            o = [x for x in hits if not is_harness(x, root, harness_names)]
            # A needle absent from the harness surface entirely detects nothing at all; a needle
            # present elsewhere cannot tell "loaded" from "read off disk". Both are unusable as a
            # harness-loaded oracle, for different reasons, so they are named differently.
            if not h:
                v, bad = "NOT-IN-HARNESS", True
            elif o:
                v, bad = "INVALID", True
            else:
                v, bad = "survives", False
            invalid += bad
            print(f"{len(hits):>6d} {len(h):>8d} {len(o):>9d}  {v:<14s} {n[:58]!r}")
            for x in o[:3]:
                print(f"{'':>26s}  false-positive source: {os.path.relpath(x, root)}")
            if len(o) > 3:
                print(f"{'':>26s}  ... and {len(o) - 3} more non-harness files")
        print()

    if a.auto:
        cand, uniq = unique_lines(root, a.auto, harness_names, a.auto_minlen, a.auto_sample)
        print(f"--auto on {a.auto}: {len(cand)} candidate lines "
              f"(>= {a.auto_minlen} chars, sampled to {a.auto_sample or 'all'})")
        print(f"lines unique to the harness surface in this corpus: {len(uniq)}")
        for n, s, nh in uniq:
            print(f"    unique ({nh} file(s)): {line_id(a.auto, n, s)}")
            if a.auto_show_lines:
                print(f"{'':>24s}{s!r}")
        if a.auto_show_lines and uniq:
            print("    ^ --auto-show-lines printed the line TEXT. Do not commit this output into\n"
                  "      the corpus: every line above would stop being unique the moment you did,\n"
                  "      which is how the published figure decayed once already.")
        if not uniq:
            print("    NONE. No marker-grep oracle exists for this corpus: every candidate line\n"
                  "    of the harness file is also present in a non-harness file the arm can see.\n"
                  "    Use a behavioural probe (harness_loaded.sh), not a grep.")
        print()

    if invalid:
        print(f"{invalid} of {len(a.needle)} needle(s) are NOT usable as a harness-loaded oracle.")
        print("A needle that fails here can still answer 'did the arm obtain our instruction TEXT\n"
              "from anywhere' -- that is a different question. It cannot answer 'was the harness\n"
              "loaded'. Do not reuse it for the second question because it passed for the first.")
        return 1
    if a.needle:
        print("Every needle given survives the FALSE-POSITIVE check. That is not validity: the\n"
              "false-NEGATIVE half is a property of the client's record-keeping and is invisible\n"
              "from any corpus scan. Corroborate with a behavioural probe before publishing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
