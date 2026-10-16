#!/usr/bin/env python3
"""How many LIVE agent sessions own background work -- the one class that cannot be
turn-scoped?

A session that started a dev server, a watch build or a long `&` job still needs it. A
turn-scoped worker exits at the end of the turn and takes that work with it. So before
proposing a pool, you have to size this class. It is the only question in the set whose
answer can veto the architecture.

THREE VERSIONS, AND THE FIRST TWO BOTH ANSWERED CONFIDENTLY AND WERE BOTH WRONG. The
history is in this docstring on purpose: each version's defect is the reason the next one
exists, and a reader who sees only v3 cannot tell which parts are load-bearing.

  v1  walked PROCESS PARENTAGE -> "0 of 16". Two defects:
      - a process backgrounded with `&` is REPARENTED to pid 1 when its shell exits, and
        so leaves the tree v1 walked;
      - its agent-internal filter contained `npm exec` and `^sh -c `, which discarded
        `npm exec vite`, `sh -c npm run dev`, `sh -c uvicorn ...` -- THE REALISTIC
        SHAPES -- *before* the age test. Only an unwrapped `vite --host` could ever have
        been seen. A filter that excludes the shapes it exists to find.

  v2  enumerated ONE cgroup directory -> "2 of 15". Also wrong, in BOTH halves:
      - NUMERATOR: both hits were cgroups holding exactly one process and NO agent
        process at all -- dead sessions' leaked orphans, not live sessions owning work.
      - DENOMINATOR: one whole class of session lived in a different cgroup subtree,
        outside the directory v2 listed. v1 saw that class; v2 traded one blindness for
        another.

  v3  (this one) discovers session cgroups TWO WAYS -- any configured session-cgroup
      directory AND the cgroup of every live agent process, which is what catches a
      class living elsewhere in the hierarchy -- and separates LIVE sessions (>=1 agent
      process in the cgroup) from ORPHAN cgroups (none). Background work is counted only
      for live sessions; orphans are reported separately, because a leaked process in an
      abandoned cgroup is a real but DIFFERENT finding.

STATED LIMIT v3 DOES NOT FIX: where several sessions share ONE cgroup (a multiplexer
rail, e.g. all sessions under one tmux service unit), per-session attribution is
impossible by this handle. Work there is reported as rail-level, never as "N sessions".
A hit inside such a cgroup is DETECTED but NOT ATTRIBUTABLE.

`cgroup.procs` is a kernel file: `stat` reports size 0, so `test -s` on it is ALWAYS
false and a guard written that way never runs. Read the CONTENT.

Read-only with one exception, disclosed: the self-test SPAWNS two short-lived decoy
processes of its own and kills them again. Nothing else is written, moved or deleted.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    `LIVE sessions owning background work older than Ns: 0` -- which is also the correct
    answer on a fleet where no session owns background work. v1 and v2 both printed a
    number of exactly this shape and both were wrong.

    That is why the zero is not trusted on its own. A SELF-TEST runs by default, plants
    TWO decoys, and the process EXITS 3 if either is missed:

      - `setsid sleep N`         tests the CGROUP HANDLE, i.e. v1's reparenting defect;
      - `setsid sh -c 'sleep N'` tests the FILTER, i.e. v2's `^sh -c ` defect.

    TWO decoys, not one, and this is the whole point: a single `setsid sleep` decoy is
    excluded by NEITHER v1's filter nor v2's, so it passed identically on both versions
    and could not discriminate the fix it was written for. The second decoy is the one
    that can. A self-test that passes on the broken version is decoration.

    So: a zero from this instrument means "none found, by an instrument just shown to
    fire on both known defect classes". A zero with `SELF-TEST FAILED` above it means
    nothing at all, and the exit code says so.

    Never use `pgrep -f` to find a decoy: the pattern matches the searching command's
    own command line and returns the wrong pid. Read cmdlines from /proc.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import time

# the one shared module in this repo: the engine adapter table
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import engines
except ImportError:                                  # pragma: no cover
    engines = None

CLK = os.sysconf("SC_CLK_TCK")

# Where per-session cgroups live, if your harness makes them. Colon-separated.
# This is only HALF the discovery: the other half walks live agent processes and reads
# their cgroup, which is what finds sessions whose cgroups are somewhere else entirely.
DEFAULT_SESSION_DIRS = "/sys/fs/cgroup/agent/sessions"

# Processes that ARE the agent itself (or its plumbing), and so are not "background work
# the session owns". Keep these specific: a generic `npm exec` / `sh -c` entry here is
# exactly v1's defect, because it discards the wrappers real background work arrives in.
# Add site-specific entrypoints via EXTRA_INTERNAL_RE instead of broadening these.
AGENT_INTERNAL = (
    r"claude-agent-acp"
    r"|npm exec @agentclientprotocol"
    r"|claude-agent-sdk.*/claude(\s|$)"
    r"|/usr/bin/claude(\s|$)|/claude\.exe(\s|$)|claude --dangerously|--remote-control"
    r"|(^|/)codex(\s|$)"
    r"|mcp[-_]server|mcp_server"
    r"|shell-snapshots"
    r"|^(-?(ba)?sh|/bin/(ba)?sh)$"
    r"|^node /.*\.bin/claude-agent-acp"
    r"|tmux|^/usr/bin/(python3?|env)$"
)

# A SESSION ENTRYPOINT: the command that starts one session. Deliberately narrower than
# AGENT_INTERNAL -- used only to count how many sessions share a cgroup, by counting
# entrypoints whose ancestors are NOT themselves entrypoints. Counting every *matching*
# process instead is wrong and visibly so: one native session's tree holds three
# processes containing `claude-agent-acp` (the npm wrapper, its `sh -c`, the node bin),
# so a naive count reported 15 of 15 cgroups as "shared" -- a detector that fires on
# everything discriminates nothing.
ENTRYPOINT = re.compile(
    engines.entrypoint_regex() if engines else
    r"claude-agent-acp|npm exec @agentclientprotocol"
    r"|claude-agent-sdk.*/claude(\s|$)"
    r"|/usr/bin/claude(\s|$)|/claude\.exe(\s|$)"
    r"|(^|/)codex(\s|$)", re.I)

# Shapes that look like real background work. Advisory only -- it annotates output with
# `<-- work-like`, it never filters. Filtering on it would hide the unfamiliar shapes,
# which are the ones worth finding.
WORKLIKE = re.compile(
    r"vite|webpack|next (dev|start)|npm (run|start)|yarn |pnpm |uvicorn|gunicorn|flask|"
    r"pytest|jest|vitest|playwright|tsc -w|nodemon|watchman|entr|tail -f|docker|"
    r"http\.server|serve|dev-server|cargo (watch|run)|go run|sleep |hang_server", re.I)


def build_internal_re():
    extra = os.environ.get("EXTRA_INTERNAL_RE", "").strip()
    pattern = AGENT_INTERNAL + (f"|{extra}" if extra else "")
    return re.compile(pattern, re.I)


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline") as fh:
            return fh.read().replace("\x00", " ").strip()
    except Exception:
        return ""


def age(pid):
    """Seconds since this process started.

    /proc/uptime is sampled PER CALL and never cached. Caching it at import gave every
    process started after import a NEGATIVE age, which silently failed every
    `age > threshold` test -- the enumeration was right and the clock was wrong. The
    self-test is what caught it.
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            st = fh.read()
        start = int(st[st.rindex(")") + 2:].split()[19])
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        return up - start / CLK
    except Exception:
        return -1.0


def cgroup_of(pid):
    try:
        with open(f"/proc/{pid}/cgroup") as fh:
            return fh.read().strip().split(":")[-1]
    except Exception:
        return ""


def procs_in(cg):
    try:
        # cgroup.procs is a kernel file and stats as size 0: read the CONTENT.
        with open(f"/sys/fs/cgroup{cg}/cgroup.procs") as fh:
            return [int(x) for x in fh.read().split()]
    except OSError:
        return []


def discover(internal_re, session_dirs):
    """Session cgroups, found two ways so that no one class of session is invisible."""
    cgs = set()
    for base in session_dirs:
        try:
            for n in os.listdir(base):
                if os.path.isdir(os.path.join(base, n)):
                    # isdir-filtered: cgroup CONTROL FILES in this directory are not
                    # sessions, and treating them as such produced hundreds of noisy
                    # errors per run in v2.
                    cgs.add(os.path.join(base, n).replace("/sys/fs/cgroup", "", 1))
        except OSError:
            continue
    for d in os.listdir("/proc"):            # the half v2 was missing
        # ENTRYPOINT, not internal_re. internal_re deliberately includes a bare shell
        # and `tmux` -- it answers "is this process background work?", not "is this a
        # session?". Using it here discovered every cgroup on the box that happened to
        # contain a `/bin/bash`, which on a CI host meant the build runners were
        # reported as live agent sessions owning background work. Same defect class as
        # v1 and v2: a filter used for the job it was not built for.
        if d.isdigit() and ENTRYPOINT.search(cmdline(int(d))):
            cg = cgroup_of(int(d))
            if cg and cg != "/":
                cgs.add(cg)
    return sorted(cgs)


def classify_by_parentage(min_age, internal_re):
    """Fallback for a box with no per-session cgroups -- e.g. ANY ordinary local setup.

    Walks process parentage from each session entrypoint instead of reading a cgroup.

    THIS IS THE INSTRUMENT'S OWN v1 METHOD, AND v1 WAS WRONG. It under-counts, for a
    reason that cannot be fixed by walking better: a process backgrounded with `&` is
    REPARENTED to pid 1 when its shell exits, so it leaves the tree entirely. A dev
    server started with `npm run dev &` is exactly the case this instrument exists to
    find, and exactly the case this fallback cannot see.

    It runs anyway, because for a local user the alternative is no answer at all, and a
    loud under-count beats `exit 2`. The warning is printed on every run and the exit
    code is distinct, so a reader can never mistake this for the cgroup result.
    """
    kids = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            kids.setdefault(ppid_of(int(d)), []).append(int(d))
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        if not ENTRYPOINT.search(cmdline(pid)):
            continue
        anc = ppid_of(pid)
        seen = set()
        is_root = True
        while anc and anc not in seen:
            seen.add(anc)
            if ENTRYPOINT.search(cmdline(anc)):
                is_root = False
                break
            anc = ppid_of(anc)
        if not is_root:
            continue
        tree, stack = [], [pid]
        while stack:
            q = stack.pop()
            tree.append(q)
            stack.extend(kids.get(q, []))
        bg = [(q, cmdline(q), age(q)) for q in tree
              if cmdline(q) and not internal_re.search(cmdline(q))
              and age(q) > min_age]
        out[f"<parentage>/pid-{pid}"] = (len(tree), bg)
    return out


def ppid_of(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            st = fh.read()
        return int(st[st.rindex(")") + 2:].split()[1])
    except Exception:
        return 0


def count_session_roots(cg):
    """How many distinct sessions live in this cgroup?

    A session root is an ENTRYPOINT process none of whose ancestors is itself an
    entrypoint. One session contributes exactly one root however many entrypoint-shaped
    processes its tree contains.
    """
    pids = set(procs_in(cg))
    roots = 0
    for q in pids:
        if not ENTRYPOINT.search(cmdline(q)):
            continue
        anc, is_root = ppid_of(q), True
        seen = set()
        while anc and anc not in seen:
            seen.add(anc)
            if ENTRYPOINT.search(cmdline(anc)):
                is_root = False
                break
            anc = ppid_of(anc)
        if is_root:
            roots += 1
    return roots


def classify(min_age, internal_re, session_dirs):
    live, orphan = {}, {}
    for cg in discover(internal_re, session_dirs):
        pids = procs_in(cg)
        if not pids:
            continue
        entries = [(q, cmdline(q)) for q in pids]
        # LIVE means "an agent session still lives here", so this keys on ENTRYPOINT
        # too; a cgroup holding only a stray shell is an orphan, not a live session.
        has_agent = any(ENTRYPOINT.search(c) for _, c in entries if c)
        bg = [(q, c, age(q)) for q, c in entries
              if c and not internal_re.search(c) and age(q) > min_age]
        (live if has_agent else orphan)[cg] = (len(pids), bg)
    return live, orphan


def self_test_age(internal_re, session_dirs):
    """Control for `age()` itself, and for the AGE THRESHOLD -- which the two decoy cases
    below do not cover.

    Why this is separate. Those cases call classify() with `min_age=1` against a decoy
    about two seconds old, while the published criterion is `MIN_AGE_S` (default 900).
    So they exercise discovery and filtering, but the age comparison only in a 1-2 s
    band. An independent reviewer demonstrated the consequence: with `age()` clamped to
    return at most 10 s, a planted 900 s decoy went undetected at `MIN_AGE_S=50` while
    the run still printed SELF-TEST PASSED and exited 0 -- a confident false zero.

    Three assertions, each catching a different `age()` defect class:

      1. POSITIVE. A cached `/proc/uptime` produced NEGATIVE ages for every process
         started after import, which silently failed every `age > threshold` test. This
         is the defect that bug actually had.
      2. MONOTONIC, by the right amount. Sampled twice `SELF_TEST_AGE_GAP_S` apart, the
         reading must grow by about that much. A cached clock grows by zero; a clamp
         grows by zero once clamped; a wrong CLK_TCK divisor grows by the wrong factor.
      3. DISCRIMINATING. The same decoy must be INCLUDED below a low threshold and
         EXCLUDED above a high one. A comparison that cannot exclude is not a threshold.

    RESIDUAL, stated because it is the same shape as the gap this closes: assertion 2
    can only catch a distortion that manifests inside the gap it samples. A clamp set
    far above `SELF_TEST_AGE_GAP_S` would still pass here. Raise the gap, at the cost of
    run time, if you need a wider band.
    """
    gap = float(os.environ.get("SELF_TEST_AGE_GAP_S", "4"))
    # A UNIQUE token per run. `sleep 300` collided with a leftover decoy from an earlier
    # run, so the test measured a 45-second-old stranger and reported it as its own
    # freshly planted decoy -- the self-test's own version of finding itself.
    token = 300 + (os.getpid() % 97)
    proc = subprocess.Popen(["sh", "-c", f"setsid --fork sleep {token}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    hits = [q for q in (int(d) for d in os.listdir("/proc") if d.isdigit())
            if f"sleep {token}" in cmdline(q)]
    ok = True
    if not hits:
        print("SELF-TEST [age() and the age THRESHOLD]: could not plant a decoy -- "
              "UNTESTED, which is a failure, not a skip")
        ok = False
    else:
        pid = hits[0]
        a1 = age(pid)
        time.sleep(gap)
        a2 = age(pid)
        positive = a1 > 0 and a2 > 0
        grew = abs((a2 - a1) - gap) <= max(1.5, gap * 0.5)
        # INDEPENDENT re-computation on a long-lived process (pid 1 always exists and is
        # always old). age() is compared against a second implementation that does NOT
        # share its code, which is the only way to catch a distortion -- a clamp, a wrong
        # divisor -- that a young decoy is too young to reveal. A reviewer clamped age()
        # at 10 s and every other assertion here still passed, because the decoy was 6 s
        # old. This leg catches that immediately and without waiting.
        try:
            with open("/proc/1/stat") as fh:
                st1 = fh.read()
            with open("/proc/uptime") as fh:
                up1 = float(fh.read().split()[0])
            indep = up1 - int(st1[st1.rindex(")") + 2:].split()[19]) / CLK
            measured = age(1)
            agrees = indep > 0 and abs(measured - indep) <= max(2.0, indep * 0.02)
        except Exception:
            indep, measured, agrees = -1.0, -1.0, False
        # classify() returns (live, orphan) -- take [0]
        inc = any(pid == q for _, (_, bg) in
                  classify(1, internal_re, session_dirs)[0].items() for q, _, _ in bg)
        exc = not any(pid == q for _, (_, bg) in
                      classify(100000, internal_re, session_dirs)[0].items()
                      for q, _, _ in bg)
        print(f"SELF-TEST [age() and the age THRESHOLD]: age {a1:.1f}s -> {a2:.1f}s "
              f"over {gap:.0f}s  positive={'YES' if positive else 'NO'}  "
              f"grew_correctly={'YES' if grew else 'NO'}  "
              f"included_below_threshold={'YES' if inc else 'NO'}  "
              f"excluded_above_threshold={'YES' if exc else 'NO'}")
        print(f"  long-process cross-check: age(1)={measured:.0f}s vs independently "
              f"computed {indep:.0f}s  agrees={'YES' if agrees else 'NO'}")
        if not (positive and grew and inc and exc and agrees):
            print("  -> the age comparison is NOT controlled on this run; a zero below "
                  "could be an age() defect rather than an absence of work")
        ok = positive and grew and inc and exc and agrees
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
    for q in hits:
        try:
            os.kill(q, 9)
        except OSError:
            pass
    return ok


def self_test(internal_re, session_dirs):
    ok = True
    # Decoy 1 MUST actually reparent to pid 1, or it does not exercise the defect its
    # label names. Measured on this box: `Popen(["setsid","sleep",N])` leaves the decoy
    # with its parent still alive (ppid = the spawning process), so it is reachable by a
    # parentage walk and tests only that a cgroup lookup finds a process in the cgroup.
    # `sh -c "setsid --fork sleep N"` reaches ppid 1, because both the sh and setsid's
    # own parent exit. The assertion below checks ppid == 1 and FAILS the self-test if
    # the shape stops reparenting on some future kernel or shell.
    cases = (("cgroup handle (v1 reparenting defect)",
              ["sh", "-c", "setsid --fork sleep 120"], True),
             ("agent-internal FILTER (v2 `sh -c` defect)",
              ["setsid", "sh", "-c", "sleep 120"], False))
    for label, argv, must_reparent in cases:
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        # the decoy may be `setsid`'s child, so find it by cgroup+cmdline. NEVER by
        # `pgrep -f`, which matches the searching command's own command line.
        mine = cgroup_of(os.getpid())
        want = "sleep 120"
        hits = [q for q in procs_in(mine) if want in cmdline(q) and q != os.getpid()]
        live, _ = classify(1, internal_re, session_dirs)
        seen = {q for _, (_, bg) in live.items() for q, _, _ in bg}
        got = bool(hits) and any(q in seen for q in hits)
        extra = ""
        if must_reparent:
            # the decoy must have left the process tree, or this case tests nothing
            ppids = [ppid_of(q) for q in hits]
            reparented = bool(ppids) and all(x == 1 for x in ppids)
            extra = f" ppid={ppids} reparented={'YES' if reparented else 'NO'}"
            if not reparented:
                extra += "  <-- DECOY DID NOT REPARENT: this case does not exercise the"
                extra += " defect it names"
            got = got and reparented
        print(f"SELF-TEST [{label}]: decoy pids={hits or '?'} "
              f"detected={'YES' if got else 'NO'}{extra}")
        ok = ok and got
        p.kill()
        for q in hits:
            try:
                os.kill(q, 9)
            except OSError:
                pass
    return ok


def demonstrate_parentage_floor(internal_re):
    """Show the degraded method MISSING a reparented decoy, live.

    The cgroup self-test does not cover this mode: it validates the cgroup handle, and
    in degraded mode the cgroup handle is not what is being used. So rather than let a
    PASS from the wrong control stand in for one, this plants the same `setsid sleep`
    decoy and shows the parentage walk failing to find it.

    A MISS here is the EXPECTED result and is reported as a demonstration, not a
    failure -- it is the reason the degraded count is labelled a floor. A HIT would be
    the surprise, and is reported as one, because it would mean this box reparents
    differently than assumed and the floor caveat may be too pessimistic here.
    """
    # same reparenting shape as self-test decoy 1 -- a decoy whose parent is still
    # alive would be found by the parentage walk and would demonstrate nothing
    p = subprocess.Popen(["sh", "-c", "setsid --fork sleep 121"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    hits = [q for q in (int(d) for d in os.listdir("/proc") if d.isdigit())
            if "sleep 121" in cmdline(q) and ppid_of(q) == 1]
    found = classify_by_parentage(1, internal_re)
    seen = {q for _, (_, bg) in found.items() for q, _, _ in bg}
    detected = bool(hits) and any(q in seen for q in hits)
    print(f"FLOOR DEMONSTRATION: planted a detached `setsid sleep` decoy "
          f"(pids={hits or '?'}, ppid=1 confirmed); parentage walk "
          f"detected={'YES' if detected else 'NO'}")
    if detected:
        print("  UNEXPECTED: this box did not reparent the decoy out of the tree. The "
              "floor caveat may be weaker here than stated -- do not generalise it.")
    else:
        print("  As expected, and this is the point: the decoy was reparented out of "
              "the tree, so the degraded count below CANNOT include work of this shape.")
    try:
        p.wait(timeout=3)
    except Exception:
        p.kill()
    for q in hits:
        try:
            os.kill(q, 9)
        except OSError:
            pass
    return detected


def main():
    ap = argparse.ArgumentParser(
        description="Which live agent sessions own background work a turn boundary "
                    "would kill? (cgroup-based, self-testing)",
        epilog=f"Env: MIN_AGE_S (900), SELF_TEST (1; set 0 to skip -- then the count is "
               f"uncontrolled), CGROUP_SESSION_DIRS (default {DEFAULT_SESSION_DIRS!r}), "
               f"EXTRA_INTERNAL_RE (added to the agent-internal pattern), "
               f"FORCE_PARENTAGE=1 (exercise the no-cgroup fallback). "
               f"Exit 3 = self-test failed, counts unmeasured. Run --help-blind.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anonymise", action="store_true",
                    help="hash session identifiers in cgroup paths (for published output)")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args()
    if args.help_blind:
        print(__doc__)
        return 0

    def cgname(cg):
        """Render a cgroup path for publication.

        Under --anonymise the whole path is replaced, not just its identifier: a cgroup
        PATH is itself site-specific (it names the harness that created it), so hashing
        only the trailing id would still publish the layout. The hash is stable within a
        run, so two lines about the same cgroup still read as the same cgroup.
        """
        if not args.anonymise:
            return cg
        return "<cgroup>/sess-" + hashlib.sha256(cg.encode()).hexdigest()[:8]

    min_age = int(os.environ.get("MIN_AGE_S", "900"))
    do_self_test = os.environ.get("SELF_TEST", "1") != "0"
    session_dirs = [d for d in os.environ.get("CGROUP_SESSION_DIRS",
                                              DEFAULT_SESSION_DIRS).split(":") if d]
    internal_re = build_internal_re()

    # No cgroup v2 is not a refusal any more: for an ordinary local setup there are no
    # per-session cgroups at all, and refusing would make this instrument useless to
    # exactly the reader most likely to run it. It degrades loudly instead -- see
    # classify_by_parentage() and exit code 4.
    have_cgroups = os.path.isdir("/sys/fs/cgroup")
    # FORCE_PARENTAGE exists so the degraded path can be EXERCISED on a box that does
    # have cgroups. Without it the fallback is unreachable here and therefore untested,
    # and an untested fallback is the one that breaks for the local user it was added
    # for. scripts/test_instruments.sh uses this to assert exit 4 and the warning text.
    if os.environ.get("FORCE_PARENTAGE") == "1":
        have_cgroups = False

    # THREE states, never two. Collapsing "skipped" into "passed" is how a control that
    # never ran ends up printing that it fired -- the decorative-gate failure this
    # instrument exists to argue against.
    if not do_self_test:
        state = "skipped"
        print("SELF-TEST SKIPPED by SELF_TEST=0 -- the counts below are uncontrolled.\n")
    elif self_test(internal_re, session_dirs) and \
            self_test_age(internal_re, session_dirs):
        state = "passed"
        print()
    else:
        state = "failed"
        print("SELF-TEST FAILED -- the counts below are UNMEASURED.\n")

    live, orphan = (classify(min_age, internal_re, session_dirs) if have_cgroups
                    else ({}, {}))
    degraded = False
    if not live:
        # No session cgroups found -- either no cgroup v2 at all, or a runtime that does
        # not put each session in its own cgroup, which is EVERY ordinary local setup.
        # Degrade loudly rather than return a zero that looks like a finding.
        degraded = True
        live = classify_by_parentage(min_age, internal_re)
        orphan = {}
        print("=" * 78)
        why = ("forced via FORCE_PARENTAGE=1" if os.environ.get("FORCE_PARENTAGE") == "1"
               else ("no cgroup v2 on this box" if not os.path.isdir("/sys/fs/cgroup")
                     else "cgroups exist but no per-session cgroup was found"))
        print(f"DEGRADED MODE: {why}.")
        print("Falling back to PROCESS PARENTAGE, which is this instrument's own v1")
        print("method, and v1 UNDER-COUNTED: a process backgrounded with `&` is")
        print("reparented to pid 1 and leaves the tree being walked. A dev server")
        print("started with `npm run dev &` is precisely what this instrument looks for")
        print("and precisely what this mode cannot see.")
        print("So: a zero below is a FLOOR, not a count. Exit code will be 4, not 0.")
        print("=" * 78)
        print()
        if do_self_test:
            demonstrate_parentage_floor(internal_re)
            print()
    print(f"criterion: a process in a LIVE session's own "
          f"{'cgroup' if not degraded else 'PROCESS TREE (degraded)'}, "
          f"not agent-internal, older than {min_age}s\n")

    owning = {cg: v for cg, v in live.items() if v[1]}
    for cg, (n, bg) in owning.items():
        print(f"LIVE {cgname(cg)}  ({n} procs)  BACKGROUND WORK:")
        for q, c, a in bg:
            print(f"    {q:>8} age={a:>8.0f}s  {c[:88]}"
                  + ("  <-- work-like" if WORKLIKE.search(c) else ""))
    for cg, (n, _bg) in orphan.items():
        for q, c in [(x, cmdline(x)) for x in procs_in(cg)]:
            if c:
                print(f"ORPHAN {cgname(cg)}  leaked: {q} age={age(q):.0f}s  {c[:80]}")

    # A cgroup holding more than one SESSION cannot attribute work per session, so flag
    # it rather than silently reporting a per-session number.
    shared = [cg for cg in live if count_session_roots(cg) > 1]
    print(f"\nLIVE session cgroups: {len(live)}"
          + (f"   (of which {len(shared)} hold{'s' if len(shared)==1 else ''} more than one SESSION -- a "
             f"shared rail: per-session attribution is impossible there, see docstring)"
             if shared else ""))
    print(f"LIVE sessions owning background work older than {min_age}s: {len(owning)}")
    print(f"ORPHAN cgroups (dead session, process left behind): {len(orphan)}")
    if degraded:
        print("MODE: degraded (process parentage). The count above is a FLOOR.")
        print("The cgroup SELF-TEST above validated the cgroup handle, which is NOT the "
              "method used in this mode -- see the FLOOR DEMONSTRATION. Do not read a "
              "zero here as 'no background work'; read it as 'none that parentage can "
              "see'." if do_self_test else
              "No control covered this mode at all (SELF_TEST=0).")
        return 4
    print({
        "passed": "SELF-TEST PASSED -- a zero above is 'none found', by an instrument "
                  "just shown to fire on both known defect classes AND to measure age "
                  "monotonically and threshold it in both directions.",
        "failed": "SELF-TEST FAILED -- do NOT believe the counts above.",
        "skipped": "SELF-TEST NOT RUN -- the counts above are uncontrolled. A zero here "
                   "is indistinguishable from the two wrong answers v1 and v2 gave.",
    }[state])
    # a failed control must not exit 0; a SKIPPED one is the caller's choice, so it does.
    # 4 == degraded: it measured something, but by the method its own history shows to
    # under-count, so it must not share an exit code with the cgroup result.
    rc = {"passed": 0, "failed": 3, "skipped": 0}[state]
    if rc == 0 and degraded:
        return 4
    return rc


if __name__ == "__main__":
    sys.exit(main())
