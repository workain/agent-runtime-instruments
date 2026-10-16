#!/usr/bin/env python3
"""What does one live agent session actually cost this box, right now?

Enumerates every process tree rooted at an agent-session entrypoint and reports
RSS *and* PSS per tree.

PSS is load-bearing here, not a refinement. A compiled agent CLI is a single large
native binary -- the Claude Code CLI measured 323,021,104 bytes on the box this was
written on -- so its file-backed pages are shared between every concurrent session.
Summed RSS counts those pages once per session; summed PSS counts them once. A
capacity claim made from summed RSS is therefore pessimistic *by construction*. Both
are printed so the gap is visible rather than argued about.

Read-only: /proc only. No database, no network, no writes.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    `session trees: 0`, and then nothing else -- every summary line is inside
    `if rows:`. That is the failure mode to watch for, because on a box with no agent
    running it is also the CORRECT answer, and the two are indistinguishable from the
    output alone. Three ways to be blind while a session is running:

      1. PATTERNS does not match your agent's entrypoint. The default matches the
         Claude Code / ACP / Codex shapes. Run with `--list-unmatched` to print the
         heaviest processes this instrument did NOT classify; if your agent is in that
         list, the zero is an instrument defect, not a measurement.
      2. /proc is not readable for other users' processes (hidepid=2, or a container
         that does not share the host PID namespace). Then trees owned by another uid
         are silently absent. `--list-unmatched` plus a known-running session is the
         check.
      3. `smaps_rollup` is unreadable (permissions, or a kernel older than 4.14), in
         which case PSS prints `n/a` per tree and the PSS total silently omits it.
         Printed as `n/a`, never as 0 -- a missing PSS must not read as a free session.

    The positive control: start one agent session by hand, run this, and confirm the
    count goes up by exactly one.
"""
import argparse
import collections
import hashlib
import os
import re
import sys

CLK = os.sysconf("SC_CLK_TCK")

# the one shared module in this repo: the engine adapter table (instruments/engines.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import engines
except ImportError:                                  # pragma: no cover
    engines = None


def default_patterns():
    """One (regex, label) per engine adapter, most specific first.

    Taken from instruments/engines.py so that adding an engine is one edit there rather
    than a regex change here. Falls back to the two engines this repo has measured if
    that module is missing, rather than matching nothing.
    """
    if engines:
        return [(e["entrypoint"],
                 f"{e['name']}" + ("" if e["verified"] else " [UNTESTED adapter]"))
                for e in engines.ENGINES]
    return [(r"claude-agent-acp", "claude-code-acp"),
            (r"(^|/)claude(\.exe)?(\s|$)", "claude-code")]


def build_patterns():
    pats = list(default_patterns())
    extra = os.environ.get("EXTRA_PATTERNS", "").strip()
    if extra:
        for chunk in extra.split(";"):
            if not chunk.strip():
                continue
            rx, _, label = chunk.partition("=")
            pats.insert(0, (rx.strip(), (label.strip() or rx.strip())))
    return [(re.compile(rx), label) for rx, label in pats]


def read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf8", "replace")
    except Exception:
        return ""


def collect():
    procs = {}
    for pid in sorted(int(d) for d in os.listdir("/proc") if d.isdigit()):
        stat = read(f"/proc/{pid}/stat")
        if not stat:
            continue
        # comm can contain spaces and parens -> split on the LAST ')'
        try:
            rest = stat[stat.rindex(")") + 2:].split()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(rest[1])
            utime, stime = int(rest[11]), int(rest[12])
        except Exception:
            continue
        cmd = read(f"/proc/{pid}/cmdline").replace("\x00", " ").strip()
        rss = 0
        m = re.search(r"VmRSS:\s+(\d+) kB", read(f"/proc/{pid}/status"))
        if m:
            rss = int(m.group(1))
        # PSS: absent (None) is NOT zero. A tree with any unreadable member reports n/a.
        pss = None
        m = re.search(r"^Pss:\s+(\d+) kB", read(f"/proc/{pid}/smaps_rollup"), re.M)
        if m:
            pss = int(m.group(1))
        procs[pid] = dict(pid=pid, ppid=ppid, comm=comm, cmd=cmd, rss=rss, pss=pss,
                          cpu_s=(utime + stime) / CLK)
    return procs


def anonymise(tag):
    """Session tags are identifiers. Published output must not carry them."""
    if not tag:
        return ""
    return "sess-" + hashlib.sha256(tag.encode()).hexdigest()[:8]


def main():
    ap = argparse.ArgumentParser(
        description="Per-session RSS and PSS for live agent process trees (read-only, /proc).",
        epilog="Env: EXTRA_PATTERNS='<regex>=<label>;...' adds entrypoint shapes. "
               "Run --help-blind for this instrument's own failure modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anonymise", action="store_true",
                    help="hash session tags before printing (use for published output)")
    ap.add_argument("--list-unmatched", action="store_true",
                    help="also print the heaviest UNCLASSIFIED processes -- the check for "
                         "'is this zero real, or is my pattern wrong?'")
    ap.add_argument("--binary-sizes", action="store_true",
                    help="also report the ON-DISK size of each matched session's "
                         "executable -- the quantity the PSS argument rests on, and one "
                         "that is easy to confuse with a process tree's peak RSS")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args()
    if args.help_blind:
        print(__doc__)
        return 0

    patterns = build_patterns()
    if engines:
        # printed every run: an UNTESTED adapter matching nothing looks exactly like an
        # engine that was not running, so the reader has to be told which is which
        print(engines.provenance_banner())
        print()
    procs = collect()

    kids = collections.defaultdict(list)
    for p in procs.values():
        kids[p["ppid"]].append(p["pid"])

    def classify(p):
        for rx, label in patterns:
            if rx.search(p["cmd"]):
                return label
        return None

    roots = []
    for p in procs.values():
        kind = classify(p)
        if not kind:
            continue
        # only the TOPMOST classified process in a chain is a tree root
        anc, is_root = p["ppid"], True
        while anc in procs:
            if classify(procs[anc]):
                is_root = False
                break
            anc = procs[anc]["ppid"]
        if is_root:
            roots.append((p, kind))

    def subtree(pid):
        out, stack = [], [pid]
        while stack:
            q = stack.pop()
            if q in procs:
                out.append(procs[q])
                stack.extend(kids.get(q, []))
        return out

    print(f"{'root pid':>8} {'procs':>5} {'RSS MB':>8} {'PSS MB':>8} {'cpu_s':>9} "
          f"{'kind':<40} tag")
    tot_rss = tot_pss = 0.0
    pss_partial = 0
    rows = []
    claimed = set()
    for p, kind in sorted(roots, key=lambda r: -r[0]["rss"]):
        tree = subtree(p["pid"])
        claimed.update(t["pid"] for t in tree)
        rss = sum(t["rss"] for t in tree) / 1024
        known = [t["pss"] for t in tree if t["pss"] is not None]
        pss = sum(known) / 1024 if len(known) == len(tree) else None
        if pss is None:
            pss_partial += 1
        cpu = sum(t["cpu_s"] for t in tree)
        m = (re.search(r"--remote-control (\S+)", p["cmd"])
             or re.search(r"/(sess[-\w]{4,})", p["cmd"]))
        tag = m.group(1) if m else ""
        if args.anonymise:
            tag = anonymise(tag)
        rows.append((p["pid"], len(tree), rss, pss, cpu, kind, tag))
        tot_rss += rss
        tot_pss += pss if pss is not None else 0.0
        print(f"{p['pid']:>8} {len(tree):>5} {rss:>8.0f} "
              f"{('%.0f' % pss) if pss is not None else 'n/a':>8} "
              f"{cpu:>9.0f} {kind:<40} {tag}")

    print(f"\nsession trees: {len(rows)}")
    if not rows:
        print("NO AGENT SESSION TREES MATCHED. This is the same output as a genuinely idle "
              "box -- see --help-blind. Re-run with --list-unmatched to check the patterns.")
    if rows:
        if tot_rss:
            print(f"summed RSS: {tot_rss:.0f} MB   summed PSS: {tot_pss:.0f} MB "
                  f"(PSS/RSS = {tot_pss / tot_rss:.2f})")
        if pss_partial:
            print(f"WARNING: {pss_partial} of {len(rows)} trees had an unreadable "
                  f"smaps_rollup, so the summed PSS above OMITS them and is a FLOOR.")
        r = sorted(x[2] for x in rows)
        q = sorted(x[3] for x in rows if x[3] is not None)
        print(f"per-session RSS  p50={r[len(r)//2]:.0f} MB  max={r[-1]:.0f} MB")
        if q:
            print(f"per-session PSS  p50={q[len(q)//2]:.0f} MB  max={q[-1]:.0f} MB")
        print(f"procs per session p50={sorted(x[1] for x in rows)[len(rows)//2]}")

    if args.binary_sizes:
        # The shared-pages argument depends on the size of the BINARY on disk, which is a
        # different quantity from a process tree's peak RSS and has been confused with it.
        # Reported in BOTH conventions because 323,021,104 bytes is "323 MB" decimal and
        # "308 MiB" binary, and a review disagreement here turned out to be exactly that
        # plus two different files on the same box.
        print("\non-disk size of each session's executable (the basis of the PSS argument):")
        seen_exe = {}
        # EVERY member of every session tree, not just the roots: the root is typically a
        # node/npm wrapper and the large agent binary sits deeper down, so scanning roots
        # alone reported the wrapper and missed the thing the argument is about.
        for pid in claimed:
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
                seen_exe.setdefault(exe, os.path.getsize(exe))
            except OSError:
                continue
        if not seen_exe:
            print("  none readable (/proc/<pid>/exe needs the same uid or CAP_SYS_PTRACE)")
        for exe, n in sorted(seen_exe.items(), key=lambda kv: -kv[1]):
            shown = exe
            if args.anonymise:
                # an exe path under a per-account directory is site-specific; keep the
                # package/binary tail, which is what identifies WHICH binary this is
                parts = exe.strip("/").split("/")
                shown = ("/".join(parts[-3:]) if len(parts) > 3 else exe)
                if not exe.startswith(("/usr/", "/bin/", "/sbin/", "/lib")):
                    shown = "<redacted>/" + shown
            print(f"  {n} bytes = {n/1e6:.0f} MB decimal = {n/1048576:.0f} MiB  {shown}")

    if args.list_unmatched:
        print("\nheaviest UNCLASSIFIED processes (is your agent in here? then the count "
              "above is wrong):")
        others = sorted((p for p in procs.values() if p["pid"] not in claimed),
                        key=lambda p: -p["rss"])[:15]
        for p in others:
            print(f"  {p['pid']:>8} {p['rss']/1024:>7.0f} MB  {p['cmd'][:96] or p['comm']}")

    mem = read("/proc/meminfo")
    print()
    for key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Committed_AS"):
        m = re.search(rf"{key}:\s+(\d+) kB", mem)
        # absent is printed, never skipped: a cgroup-limited container does not expose
        # all of these, and a silently missing line reads as "nothing to report"
        print(f"{key}: {int(m.group(1))/1024:.0f} MB" if m else f"{key}: n/a (absent from /proc/meminfo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
