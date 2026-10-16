#!/usr/bin/env python3
"""Per-turn boot cost: how long does a headless agent process take to become ready,
and what does it cost in RAM to get there?

This is the number a turn-scoped worker pool pays on EVERY turn and a resident session
pays once, so it decides whether the pool is cheaper in practice or only on paper.

TWO figures are reported, because one is not enough and the cheap one is a FLOOR:

  (a) `boot` -- the clock stops at the readiness event (`system/init` for Claude Code,
      `thread.started` for Codex) and the process is killed, so NO MODEL REQUEST IS
      SPENT. But `init` is the FIRST event in the stream, and MCP servers appear in it
      with status "pending": the CLI does not connect them before emitting it. So this
      clock stops BEFORE any tool is callable. An earlier version of this docstring
      asserted that `init` came *after* MCP connect; that was wrong, and this
      instrument's own output is what refuted it. Use `bench_mcp_connect.py` for the
      figure `init` hides.

  (b) `turn` -- run to the `result` event on a trivial prompt. Spends ONE cheap model
      request, so it includes MCP connect plus one inference round-trip: the honest
      end-to-end cost of a turn a pool would pay.

Report both. (a) alone understates a pooled turn; (b) alone conflates boot with
inference.

Peak RSS is summed `VmHWM` (high-water mark) over the whole process tree at the instant
readiness is seen, so it includes MCP servers the boot spawned.

`stdin=DEVNULL` is load-bearing, not hygiene -- see the comment at the `Popen` call.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    `NO-READY-EVENT` in the ready column, with a `first_out` that may still be
    populated. That is the honest shape and it is printed, not swallowed.

    Four ways the number can be wrong while still LOOKING fine:

      1. A flat constant added to EVERY arm. This happened: the process inherited the
         invoking shell's stdin, and when that descriptor is a pipe nobody writes to,
         the CLI waits for piped input and warns "no stdin data received in 3s". A flat
         +3 s is INVISIBLE to any comparison between arms -- every ratio still looked
         plausible -- and because the fd is INHERITED it is environment-dependent, so it
         recurs unpredictably rather than always. Measured: one re-run read 4.04 s/3.48 s
         where the arms otherwise read 1.15 s/0.65 s. It was caught by reading stderr,
         not by the numbers looking wrong. Hence: stdin is pinned to DEVNULL, and stderr
         is captured and PRINTED rather than discarded.
      2. The binary is absent. Then the arm is SKIPPED with a visible notice, never
         silently reported as zero or omitted from the output.
      3. The readiness predicate does not match this CLI's event vocabulary, so the loop
         drains stdout and reports `NO-READY-EVENT`. Distinguish from (2) by whether
         `first_out` is populated: output arrived but was never recognised.
      4. Dispersion is mistaken for noise. On a loaded box the SAME arm measured
         1.16 s and 3.16 s minutes apart. Every rep is printed, not just the p50,
         because twice in this work the unstable spread WAS the finding while the
         median looked fine. Treat these as single-digit-second figures, not constants.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

REPS = int(os.environ.get("REPS", "3"))


def tree_vmhwm_kb(root):
    """Summed VmHWM over the process tree rooted at `root`, and the process count."""
    ppid = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                s = fh.read()
            ppid[int(d)] = int(s[s.rindex(")") + 2:].split()[1])
        except Exception:
            pass
    kids = {}
    for p, pa in ppid.items():
        kids.setdefault(pa, []).append(p)
    total, seen, stack = 0, set(), [root]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(kids.get(p, []))
        try:
            with open(f"/proc/{p}/status") as fh:
                m = re.search(r"VmHWM:\s+(\d+) kB", fh.read())
            if m:
                total += int(m.group(1))
        except Exception:
            pass
    return total, len(seen)


def run(label, argv, ready_pred, reps=REPS):
    if not shutil.which(argv[0]):
        print(f"\n### {label}\n    SKIPPED: {argv[0]!r} is not on PATH. "
              f"Not a measurement of zero -- this arm did not run.")
        return []
    rows = []
    for _ in range(reps):
        t0 = time.monotonic()
        # stdin=DEVNULL is load-bearing. Omit it and the child INHERITS the invoking
        # shell's stdin; when that fd is a pipe nobody writes to, the CLI waits for
        # piped input and adds a flat ~3s to this arm and every other one. See the
        # docstring's failure mode (1).
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)
        ready = first = None
        try:
            for line in p.stdout:
                if first is None:
                    first = time.monotonic() - t0
                if ready_pred(line):
                    ready = time.monotonic() - t0
                    break
        except Exception as ex:
            print("  read error:", ex)
        hwm, nproc = tree_vmhwm_kb(p.pid) if p.poll() is None else (0, 0)
        p.kill()
        # stderr is PRINTED, not discarded: the +3s artefact lived here for two runs.
        err = (p.stderr.read() or "")[:200].replace("\n", " ") if p.stderr else ""
        rows.append((ready, first, hwm / 1024, nproc, err))
        time.sleep(1)

    print(f"\n### {label}")
    print(f"    argv: {' '.join(argv)[:150]}")
    for r in rows:
        rd = f"{r[0]:.2f}s" if r[0] else "NO-READY-EVENT"
        print(f"    ready={rd:>14}  first_out={(('%.2fs' % r[1]) if r[1] else 'none'):>8}  "
              f"peakRSS(tree)={r[2]:7.0f} MB  procs={r[3]}"
              + (f"  stderr: {r[4]}" if r[4].strip() else ""))
    ok = [r[0] for r in rows if r[0]]
    if ok:
        # every rep is above; the spread is reported because it has twice been the finding
        print(f"    => ready p50={sorted(ok)[len(ok)//2]:.2f}s  min={min(ok):.2f}s  "
              f"max={max(ok):.2f}s  (n={len(ok)}/{len(rows)})")
    else:
        print("    => NO ARM PRODUCED A READINESS EVENT. If first_out is populated, the "
              "readiness predicate does not match this CLI's events; if not, nothing ran.")
    return rows


def main():
    ap = argparse.ArgumentParser(
        description="Per-turn boot cost for a headless agent CLI: boot-to-ready and "
                    "boot-to-first-result.",
        epilog="Env: REPS (3). Spends ONE cheap model request per rep in the `turn` arm "
               "only; the `boot` arms spend none. Run --help-blind for failure modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-turn", action="store_true",
                    help="run only the no-cost boot arms (spends no model requests at all)")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args()
    if args.help_blind:
        print(__doc__)
        return 0

    flat = lambda l: l.replace(" ", "")
    is_init = lambda l: '"subtype":"init"' in flat(l) or (
        '"type":"system"' in flat(l) and "init" in l)
    is_codex = lambda l: ("thread.started" in l or "turn.started" in l
                          or "session_configured" in l)
    is_result = lambda l: '"type":"result"' in flat(l)

    run("Claude Code, full harness (hooks + MCP + project-instruction discovery)",
        ["claude", "-p", "noop", "--output-format", "stream-json", "--verbose",
         "--max-turns", "1"],
        is_init)

    run("Claude Code, --bare (no hooks/MCP/skills/project instructions)",
        ["claude", "--bare", "-p", "noop", "--output-format", "stream-json", "--verbose",
         "--max-turns", "1"],
        is_init)

    run("Codex exec --json",
        ["codex", "exec", "--json", "--skip-git-repo-check", "noop"],
        is_codex)

    if args.skip_turn:
        print("\n(`turn` arm skipped by --skip-turn: the boot figures above stop before "
              "any tool is callable and are a FLOOR, not an end-to-end turn cost.)")
        return 0

    # (b) end-to-end: stop at `result`, so MCP connect and one inference are INSIDE the
    # figure. This is what a pooled worker actually pays per turn.
    run("Claude Code, FULL TURN to result (includes MCP connect + 1 inference)",
        ["claude", "-p", "say only: ok", "--output-format", "stream-json", "--verbose",
         "--max-turns", "1"],
        is_result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
