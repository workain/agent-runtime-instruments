#!/usr/bin/env python3
"""Does prompt-cache behaviour depend on the CLIENT PROCESS, or only on the prefix bytes?

This decides whether a turn-scoped worker pool loses a warm cache that a resident
process was holding. If it does, pooling has a per-turn token cost that looks expensive
on paper. The documentation describes caching as a prefix hash with a TTL and says
nothing about the client process -- but "the docs do not mention it" is not a
measurement, and a reviewer proposed a concrete counter-mechanism: a resident process
might re-render its prefix identically while a fresh `--resume` process renders it even
slightly differently, which would make the fresh one miss.

Arguing it twice settles nothing. This measures it.

ARMS, and why each exists:

  1 RESIDENT   -- one process, two turns over stream-json stdin.
  2 FRESH      -- turn 1 in one process, turn 2 in a NEW process via `--resume`, inside
                  the TTL. Comparing two FRESH invocations instead would measure the
                  TTL, not residency.
  3 SUFFIX PERTURBATION -- KEPT AND PUBLISHED AS A FAILED CONTROL. See below.
  4 PREFIX PERTURBATION -- switch model. This is the control that actually works.
  5 DEAD-WRITER TEST -- the arm that actually tests the headline claim, with its own
                  control. Process A writes a cache under a unique marker and EXITS.
                  Process B then resumes with the SAME marker; process C resumes with a
                  DIFFERENT one. If B reads substantially more cache than C, B read what
                  the now-dead A wrote. Arms 1-2 cannot show this on their own: on a
                  busy box their prefixes may already be warm from earlier runs, in
                  which case both "hit" without either having read the other's write.

WHY ARM 3 IS STILL HERE. It was meant to prove the instrument CAN show a cache miss --
without such a control, "both arms hit" cannot be distinguished from "this field is
always populated". It does not work: `--append-system-prompt` lands at the END of the
system block, so the prefix BEFORE it still matches and the read need not drop. It is a
SUFFIX perturbation, not a prefix one. That is an error in the experiment's design, not
a finding about caching, and it is published rather than deleted because an instrument
whose failed controls have been removed looks more reliable than it is. Arm 4 replaces
it: caches are keyed per model, so switching models forces a genuine prefix miss.

Reports cache READ and cache CREATION tokens for every arm. Zero read with non-zero
creation is a miss; non-zero read with zero creation is a hit.

COSTS MONEY: this spends roughly 5-6 cheap model requests per run.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    `None` for every usage field -- which happens whenever the `result` event is absent
    or its `usage` block is shaped differently than expected. That is printed as `None`,
    never as 0, because a zero cache read is a MEASURED MISS and a missing field is NO
    MEASUREMENT, and the whole reading turns on telling those apart.

    The failure mode that matters more is subtler: A NULL RESULT THAT READS AS
    CONFIRMATION. If both arms hit, that looks like "residency does not matter" -- but it
    looks identical to "every prefix in this experiment was already warm, so nothing
    could have missed". That is exactly what happened on the first use of this
    instrument: both of one run's arms were pre-warmed by an earlier run, so the run
    could not have detected the counter-mechanism it was cited as refuting. The
    citation was withdrawn. ARM 4 IS THE GUARD: if arm 4 does not show a drop, the
    instrument did not discriminate anything on this run and the run proves nothing,
    whatever arms 1 and 2 did. The script says so in its own reading.

    Two further limits, stated because one run would otherwise read as proof:
      - n=1 per arm. Arms 1 and 2 are NOT prefix-identical -- the resident arm runs both
        turns in one process, so by turn 2 it has a longer cached prefix. "Resident read
        76,443 vs fresh 50,105" is therefore NOT a like-for-like comparison and must not
        be quoted as residency being cheaper.
      - The same instrument has produced opposite patterns on different runs (one run
        gave the resident arm `read=0, creation=76443`, a cold write). That instability
        is the honest reason this is a data point and not a result.

    A CRITERION WAS CHANGED AFTER SEEING DATA, AND BOTH ARE STILL PRINTED. The original
    control test was `perturbed_read < 0.5 * unperturbed_read`. On a real run it called
    the control FAILED while cache_creation had gone 2,875 -> 14,005 (4.9x) under the
    perturbation -- i.e. the control had plainly fired. The read-ratio test cannot tell
    a PARTIAL miss from a hit, because a partial miss still reads the prefix that did
    match; that is a flaw in the test on its face, not a conclusion drawn from this
    run's outcome. The hit-fraction `read/(read+creation)` is now primary. Changing a
    criterion after seeing data is how results get manufactured, so BOTH verdicts are
    printed on every run and the reader can see what the change did.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

MODEL = os.environ.get("MODEL", "")
ALT_MODEL = os.environ.get("ALT_MODEL", "haiku")
TURN1 = "Reply with exactly: one"
TURN2 = "Reply with exactly: two"


def usage_of(lines):
    for l in reversed(lines):
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("type") == "result":
            u = d.get("usage") or {}
            # .get() with no default: a MISSING field stays None and must never be
            # printed as 0, because 0 cache_read is a measured MISS
            return dict(sid=d.get("session_id"),
                        read=u.get("cache_read_input_tokens"),
                        create=u.get("cache_creation_input_tokens"),
                        inp=u.get("input_tokens"))
    return None


def run(argv, stdin_lines=None, cap=180):
    p = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_lines else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(
            "\n".join(stdin_lines) + "\n" if stdin_lines else None, timeout=cap)
    except subprocess.TimeoutExpired:
        p.kill()
        return [], "TIMEOUT"
    return out.splitlines(), (err or "")[-300:]


def main():
    ap = argparse.ArgumentParser(
        description="Does a prompt-cache hit depend on the client process being resident?",
        epilog="Env: MODEL (default: CLI default), ALT_MODEL (haiku) for the "
               "prefix-perturbation control. SPENDS ~5-6 cheap model requests. "
               "Run --help-blind for failure modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anonymise", action="store_true",
                    help="hash session_ids before printing (for published output)")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args()
    if args.help_blind:
        print(__doc__)
        return 0

    def show(u):
        """Render a usage dict, hashing the session_id under --anonymise.

        The TOKEN COUNTS are never touched -- they are the evidence. Only the identifier
        is replaced, and visibly so.
        """
        if not u:
            return str(u)
        d = dict(u)
        if args.anonymise and d.get("sid"):
            d["sid"] = "sid-" + hashlib.sha256(d["sid"].encode()).hexdigest()[:8]
        return str(d)

    base = ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--max-turns", "1"] + (["--model", MODEL] if MODEL else [])

    print("ARM 1 -- RESIDENT: one process, two turns over stream-json stdin")
    msg = lambda t: json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": t}]}})
    res_lines, err = run(
        ["claude", "-p", "--output-format", "stream-json", "--verbose",
         "--input-format", "stream-json", "--max-turns", "4"]
        + (["--model", MODEL] if MODEL else []),
        stdin_lines=[msg(TURN1), msg(TURN2)])
    res_usages = []
    for l in res_lines:
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("type") == "result":
            u = d.get("usage") or {}
            res_usages.append((d.get("session_id"),
                               u.get("cache_read_input_tokens"),
                               u.get("cache_creation_input_tokens"),
                               u.get("input_tokens")))
    for i, (sid, r, c, inp) in enumerate(res_usages, 1):
        print(f"  resident turn{i}: cache_read={r} cache_creation={c} input={inp}")
    if err.strip():
        print("  stderr:", err.strip().replace("\n", " ")[:200])

    print("\nARM 2 -- FRESH: turn 1 in one process, turn 2 in a NEW process via --resume")
    l1, e1 = run(base + [TURN1])
    u1 = usage_of(l1)
    print(f"  fresh turn1: {show(u1)}")
    if not u1 or not u1.get("sid"):
        print("  CANNOT CONTINUE: no session_id in the result event, so there is nothing "
              "to resume. This is NO MEASUREMENT, not a null result.")
        print("  stderr:", (e1 or "").strip()[:200])
        return 2
    time.sleep(5)
    l2, _e2 = run(base + ["--resume", u1["sid"], TURN2])
    u2 = usage_of(l2)
    print(f"  fresh turn2 (new process, resumed, inside TTL): {show(u2)}")

    print("\nARM 3 -- SUFFIX PERTURBATION (kept, and it FAILED as a control)")
    l3, _e3 = run(base + ["--resume", u1["sid"], "--append-system-prompt",
                          f"Irrelevant marker {uuid.uuid4()}", TURN2])
    u3 = usage_of(l3)
    print(f"  perturbed turn2: {show(u3)}")

    print(f"\nARM 4 -- PREFIX PERTURBATION, the real control: switch model to "
          f"{ALT_MODEL!r} (caches are keyed per model)")
    l4, _e4 = run(base + ["--resume", u1["sid"], "--model", ALT_MODEL, TURN2])
    u4 = usage_of(l4)
    print(f"  model-switched turn2: {show(u4)}")

    # ARM 5: the headline claim, properly controlled.
    print("\nARM 5 -- DEAD-WRITER TEST: does a cache written by an EXITED process get "
          "read by a new one?")
    marker_a = f"Cache-probe marker {uuid.uuid4()}"
    marker_c = f"Cache-probe marker {uuid.uuid4()}"
    la, _ea = run(base + ["--append-system-prompt", marker_a, TURN1])
    ua = usage_of(la)
    print(f"  A (writer; exits before B and C run): {show(ua)}")
    if not ua or not ua.get("sid"):
        print("  CANNOT RUN ARM 5: no session_id from the writer. NO MEASUREMENT.")
        ub = uc = None
    else:
        # run() uses communicate(), so process A has already exited by this line.
        time.sleep(3)
        lb, _eb = run(base + ["--resume", ua["sid"], "--append-system-prompt",
                              marker_a, TURN2])
        ub = usage_of(lb)
        print(f"  B (new process, SAME marker as the dead writer):      {show(ub)}")
        lc, _ec = run(base + ["--resume", ua["sid"], "--append-system-prompt",
                              marker_c, TURN2])
        uc = usage_of(lc)
        print(f"  C (new process, DIFFERENT marker -- the control):     {show(uc)}")

    print("\nREADING")
    if len(res_usages) > 1 and u2:
        print(f"  resident 2nd-turn cache_read={res_usages[1][1]}   "
              f"fresh 2nd-turn cache_read={u2['read']}")
        print("  NOT like-for-like: the resident arm's turn 2 follows a longer cached "
              "prefix. Do not quote this pair as residency being cheaper.")
    if u2 and u3:
        dropped = (u3["read"] or 0) < (u2["read"] or 0) * 0.5
        print(f"  suffix-perturbed cache_read={u3['read']} vs unperturbed {u2['read']}"
              f"  <- {'dropped' if dropped else 'did NOT drop'}; either way this arm is "
              f"NOT a reliable control (--append-system-prompt is a SUFFIX change)")

    def hit_frac(u):
        if not u:
            return None
        r, c = (u.get("read") or 0), (u.get("create") or 0)
        return (r / (r + c)) if (r + c) else None

    control_holds = False
    if u2 and u4:
        f2, f4 = hit_frac(u2), hit_frac(u4)
        # PRIMARY criterion: hit fraction read/(read+creation). A partial miss still
        # reads the prefix that matched, so a read-only test cannot see it.
        if f2 is not None and f4 is not None:
            control_holds = f4 < f2 - 0.15
            print(f"  model-switched hit_frac={f4:.0%} (read={u4['read']} "
                  f"creation={u4['create']}) vs unperturbed hit_frac={f2:.0%} "
                  f"(read={u2['read']} creation={u2['create']})")
            print(f"    PRIMARY (hit-fraction) -> control "
                  f"{'HOLDS (the instrument can show a miss)' if control_holds else 'FAILED'}")
        # SECONDARY, the superseded criterion, printed so the change is auditable
        old = (u4["read"] or 0) < (u2["read"] or 0) * 0.5
        print(f"    SUPERSEDED (read-ratio) -> control "
              f"{'HOLDS' if old else 'FAILED'}  [kept visible: this is the test that "
              f"misread a 4.9x creation increase as no-miss]")

    print("\nVERDICT")
    # Arm 4 controls arms 1-2 ONLY. Arm 5 carries its own control (process C), so it
    # must be read whether or not arm 4 fired -- gating it on arm 4 would discard a
    # self-controlled result because an unrelated control was noisy.
    if not control_holds:
        print("  ARMS 1-2 ARE UNCONTROLLED ON THIS RUN. Arm 4 did not show a miss, so "
              "on this run the instrument was not shown able to discriminate a hit from "
              "a miss, and a run in which nothing COULD have missed cannot evidence "
              "that nothing DID. Arm 4 is itself unreliable on a shared box: other "
              "processes warm the alternate model's cache. Arm 5 below does not depend "
              "on it.")
    if u2 and (u2["read"] or 0) > 0:
        print(f"  Arm 2: a FRESH process resuming the session read {u2['read']} cached "
              f"tokens (creation={u2['create']}, the new suffix). Residency was not "
              f"required for a cache hit.")
        print("  But on a busy box arm 2's prefix may ALREADY have been warm from an "
              "earlier run, in which case it shows a hit without showing WHOSE write "
              "was read. Arm 5 is the arm that separates those.")
    else:
        print("  Arm 2 did not show a hit at all, so the counter-mechanism (a fresh "
              "process rendering a differing prefix) is NOT excluded by this run.")

    arm5_ok = False
    if ub and uc:
        rb, rc = (ub.get("read") or 0), (uc.get("read") or 0)
        print(f"  Arm 5: B (same marker as the dead writer) read {rb}; "
              f"C (different marker) read {rc}.")
        if rb > rc * 1.2 and rb > 0:
            arm5_ok = True
            print(f"  => B read MORE than C by {rb - rc} tokens. The only difference "
                  f"between them is whether the marker matches what the now-EXITED "
                  f"process A wrote. A cache entry outlived the process that created "
                  f"it and was read by a different one.")
            # Exact-arithmetic check: if B read precisely C's common prefix PLUS what A
            # created, the attribution is not merely directional. A near-miss here is
            # still consistent with the claim; an exact match is much harder to get by
            # coincidence, so it is called out separately rather than folded in.
            ac = (ua.get("create") or 0) if ua else 0
            if ac and (rb - rc) == ac:
                print(f"     EXACT: B_read({rb}) == C_read({rc}) + A_creation({ac}). "
                      f"B read precisely the bytes A wrote, on top of the prefix both "
                      f"share. Token-exact, not directional.")
            elif ac:
                print(f"     (A created {ac}; B-C = {rb - rc}. Directional, not exact -- "
                      f"consistent with the claim, weaker than a token-exact match.)")
        elif rb > 0 and rc > 0 and rb <= rc * 1.2:
            print("  => B and C read comparably, so this run does NOT isolate A's write: "
                  "both may be hitting a prefix cached before A ran. NOT established "
                  "by arm 5 on this run.")
        else:
            print("  => arm 5 did not produce a usable comparison on this run.")
    print("  LIMIT: n=1 per arm throughout. Supported, not settled.")
    # exit 0 only if SOMETHING was controlled: arm 5 on its own control, or arms 1-2
    # under a working arm 4. Otherwise this run measured nothing and must not exit 0.
    if arm5_ok or control_holds:
        return 0
    print("  NOTHING ON THIS RUN WAS CONTROLLED -- exiting 3.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
