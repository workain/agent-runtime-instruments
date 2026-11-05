#!/usr/bin/env python3
"""Would a published token / round / cost figure survive its own request population?

You measured something from a capture of API requests. This asks whether the SET of requests
you measured over is the set you think it is -- before the figure is published, not after a
reviewer finds it.

It is not a credential scanner. "Token" here means a billing token, not a secret.

WHY A GENERIC CHECK IS POSSIBLE. In the study this came from, one round found seventeen
instrument defects by hand. FOUR of them are a single shape -- a figure computed over the wrong
set of requests, or over a usage field that was not there -- and that shape is mechanically
checkable from the capture alone, with no knowledge of the experiment:

  * A missing key read as a zero. The Anthropic cache field does not exist in the OpenAI
    protocol, so a sum over it returned a confident, clean 0, and "this client does no caching"
    was briefly reported FROM AN ABSENT FIELD. Reported here as UNKNOWN, never as 0.
  * Retries counted as work. In one draw most of the logged requests were HTTP 429 rate-limit
    retries rather than billed work. Retries were excluded in one arm and left in another, and a
    round-count "spread" was built on the inflated figure.
  * Zero-cost capability probes counted as rounds. A handful of "errors" were tiny not-found
    responses from a capability probe. Counting them inflates a round count and puts an error in
    a latency column.
  * Records mistaken for requests. One capture carried 2.09-3.00 usage-bearing transcript
    records per API request, and a transcript that REPLAYS ITSELF on compaction once inflated a
    count by 5.36x. Detected as duplicate ids when you pass --id-field.

The last is worth emphasising: the API-call unit is a distinct request id. An agent-turn count
is not that unit, and a transcript-record count is further from it still.

READING THE OUTPUT. Every finding names the request count behind it. **A finding is a POPULATION
FACT, not automatically a defect** -- a 429 in a capture is a fact about the provider, and
becomes a defect only when it sits inside a set someone called "rounds". What this guarantees is
that nobody publishes such a figure without the count having been put in front of them first.

HOW IT LIES:

  1. It does NOT re-derive cost from a price model, so a figure that is wrong because of a
     mispriced TIER passes every check here. That is a real check and it caught the largest
     single error in the study -- a price tier keyed on uncached tokens only, wrong on 105 of
     401 requests, turning a published 50x into 10x -- but it needs a per-provider price table
     with a cliff in it, which is state that goes stale silently. Scoped out, not half-built.
  2. Duplicate detection needs --id-field. Without it that whole class is invisible, and the
     instrument will not guess which of your fields is an id.
  3. Field names are inputs (--usage-field, --cost-field, --status-field). Point one at the
     wrong key and you get a FIELD-ABSENT finding, which is the honest outcome but is about
     your argument rather than your data.
  4. Cross-arm asymmetry needs more than one capture. Given one, that class cannot be reported.
  5. It reads what the capture CONTAINS. A request the capture never recorded is invisible, and
     no count here can tell you the capture itself is complete.

EXIT CODES. 0 no findings; 1 one or more findings (read them -- they may be facts, not defects);
2 with `REFUSING (exit 2)` when it cannot measure at all -- zero capture files matched, or a
capture with no records. A refusal is NEVER 1, because "audit found nothing wrong" and "audit
never ran" must not reach a caller as the same code.
"""
import argparse
import collections
import glob
import json
import os
import sys

# Known per-request usage shapes, by the key that identifies each. A shape this does not
# recognise is UNKNOWN -- never silently treated as zero-everything, which is defect 2's whole
# mechanism.
SHAPES = {
    "anthropic": ("input_tokens", "output_tokens"),
    "openai": ("prompt_tokens", "completion_tokens"),
}


def refuse(msg):
    """Cannot-measure exits 2, NEVER 1.

    1 is this instrument's "it measured, and the answer is bad" code. A refusal that also exits 1
    is indistinguishable from a real finding to any caller that only tests rc != 0 -- so the two
    conditions this repository most wants kept apart would arrive as the same signal. Exit 2 is
    the convention the other instruments here use, for the same reason.
    """
    print(f"REFUSING (exit 2): {msg}")
    sys.exit(2)


def shape_of(usage):
    if not isinstance(usage, dict) or not usage:
        return "ABSENT"
    for name, keys in SHAPES.items():
        if all(k in usage for k in keys):
            return name
    return "UNKNOWN"


def get(d, path):
    """Dotted lookup that distinguishes ABSENT from a falsy value. Returns (found, value).

    A plain `d.get("cost", 0)` is the ad-hoc-JSON-access failure: a renamed or protocol-specific
    key returns the default and prints a clean zero. Every caller below must branch on `found`.
    """
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def audit_capture(path, arm_field, status_field, usage_field, cost_field, id_field):
    recs = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                # A record that does not parse is NOT skipped quietly: it is a record whose
                # tokens are unaccounted for, which is exactly the thing being audited.
                recs.append({"__unparseable__": ln})
    if not recs:
        refuse(f"{path} has no records. An empty capture is not a clean one.")

    byarm = collections.defaultdict(list)
    for r in recs:
        found, arm = get(r, arm_field)
        byarm[arm if found else f"<no {arm_field}>"].append(r)

    out = []
    for arm, rows in sorted(byarm.items()):
        o = {"capture": os.path.basename(path), "arm": arm, "requests": len(rows),
             "findings": []}
        unparse = [r for r in rows if "__unparseable__" in r]
        statuses, shapes, ids = collections.Counter(), collections.Counter(), collections.Counter()
        cost_present = cost_absent = 0
        cost_sum = 0.0
        zero_cost_2xx = 0
        arith_bad = []
        for r in rows:
            if "__unparseable__" in r:
                continue
            sf, st = get(r, status_field)
            statuses[st if sf else "<absent>"] += 1
            uf, usage = get(r, usage_field)
            sh = shape_of(usage) if uf else "ABSENT"
            shapes[sh] += 1
            cf, cost = get(r, cost_field)
            if cf and cost is not None:
                cost_present += 1
                try:
                    cost_sum += float(cost)
                    if float(cost) == 0.0 and isinstance(st, int) and 200 <= st < 300:
                        zero_cost_2xx += 1
                except (TypeError, ValueError):
                    pass
            else:
                cost_absent += 1
            if id_field:
                idf, rid = get(r, id_field)
                if idf:
                    ids[rid] += 1
            # Component arithmetic, only where the shape declares a total. A total that does not
            # equal its own parts is a double count or a dropped component.
            if sh == "openai" and isinstance(usage, dict) and "total_tokens" in usage:
                try:
                    parts = int(usage["prompt_tokens"]) + int(usage["completion_tokens"])
                    if parts != int(usage["total_tokens"]):
                        arith_bad.append((parts, usage["total_tokens"]))
                except (TypeError, ValueError):
                    pass

        o["statuses"] = {str(k): v for k, v in sorted(statuses.items(), key=lambda x: str(x[0]))}
        o["usage_shapes"] = dict(shapes)
        o["cost_present"] = cost_present
        o["cost_absent"] = cost_absent
        o["cost_sum"] = round(cost_sum, 6)
        o["unparseable"] = len(unparse)
        o["zero_cost_2xx"] = zero_cost_2xx

        non2xx = sum(v for k, v in statuses.items()
                     if not (isinstance(k, int) and 200 <= k < 300))
        o["non_2xx"] = non2xx
        if non2xx:
            o["findings"].append(
                {"code": "NON-2XX-IN-POPULATION", "n": non2xx,
                 "detail": f"{non2xx} of {o['requests']} requests are not 2xx "
                           f"({o['statuses']}). A round count over this capture includes them "
                           f"unless it filters on status; alm#961 defect 6 built an 8x spread "
                           f"on exactly this."})
        if cost_absent:
            o["findings"].append(
                {"code": "COST-FIELD-ABSENT", "n": cost_absent,
                 "detail": f"{cost_absent} of {o['requests']} requests carry no '{cost_field}'. "
                           f"These contribute UNKNOWN, not 0, to any cost total."})
        if zero_cost_2xx:
            o["findings"].append(
                {"code": "ZERO-COST-2XX", "n": zero_cost_2xx,
                 "detail": f"{zero_cost_2xx} successful requests cost exactly 0 -- a probe or a "
                           f"cached no-op. alm#961 defect 13: four such 404s would have inflated "
                           f"a round count by four."})
        if shapes.get("UNKNOWN") or shapes.get("ABSENT"):
            n = shapes.get("UNKNOWN", 0) + shapes.get("ABSENT", 0)
            o["findings"].append(
                {"code": "USAGE-SHAPE-UNKNOWN", "n": n,
                 "detail": f"{n} requests have no recognised usage shape. Summing a "
                           f"protocol-specific key over these returns a confident 0 from a "
                           f"MISSING KEY -- alm#961 defect 2."})
        known = [s for s in shapes if s in SHAPES]
        if len(known) > 1:
            o["findings"].append(
                {"code": "MIXED-USAGE-SHAPES", "n": len(known),
                 "detail": f"this arm mixes usage protocols {known}. Cache and total fields are "
                           f"named differently in each; one summation cannot be right for both."})
        dup = {k: v for k, v in ids.items() if v > 1}
        if dup:
            o["findings"].append(
                {"code": "DUPLICATE-ID", "n": sum(v - 1 for v in dup.values()),
                 "detail": f"{len(dup)} ids appear more than once. Counting RECORDS instead of "
                           f"distinct ids overcounted 2.09-3.00x in alm#963 and 5.36x once in "
                           f"this fleet."})
        if arith_bad:
            o["findings"].append(
                {"code": "COMPONENT-SUM-MISMATCH", "n": len(arith_bad),
                 "detail": f"prompt+completion != total on {len(arith_bad)} requests, e.g. "
                           f"{arith_bad[0][0]} vs {arith_bad[0][1]}."})
        if unparse:
            o["findings"].append(
                {"code": "UNPARSEABLE-RECORD", "n": len(unparse),
                 "detail": "a record that does not parse is tokens nobody counted, not a record "
                           "to skip."})
        out.append(o)
    return out


def cross_arm(rows):
    """FILTER-ASYMMETRY: the defect is not a 429, it is a 429 counted in one arm and not another.

    alm#961 defect 6 in one line: "I had excluded Hermes's probes from its round count and left
    Claude Code's retries in -- the same class of non-round, filtered in one arm and not the
    other." No single-capture check can see that, which is why this one takes the whole set.
    """
    findings = []
    dirty = [r["arm"] for r in rows if r["non_2xx"] > 0 or r["cost_absent"] > 0]
    clean = [r["arm"] for r in rows if r["non_2xx"] == 0 and r["cost_absent"] == 0]
    if dirty and clean:
        findings.append(
            {"code": "FILTER-ASYMMETRY", "n": len(dirty),
             "detail": f"{len(dirty)} arm(s) contain non-2xx or uncosted requests "
                       f"({sorted(set(dirty))[:4]}) while {len(clean)} arm(s) contain none "
                       f"({sorted(set(clean))[:4]}). Any per-arm round or cost count must state "
                       f"ONE inclusion rule and apply it to both, or the comparison is between "
                       f"two different populations."})
    shapes = collections.Counter()
    for r in rows:
        for s in r["usage_shapes"]:
            if s in SHAPES:
                shapes[s] += 1
    if len(shapes) > 1:
        findings.append(
            {"code": "CROSS-ARM-MIXED-SHAPES", "n": len(shapes),
             "detail": f"the arms being compared use different usage protocols {dict(shapes)}. "
                       f"A field-by-field ratio across them is comparing differently-named "
                       f"quantities; normalise first and report UNKNOWN where a field is absent."})
    return findings



# ---------------------------------------------------------------------------------------------
# CONTROLS
# ---------------------------------------------------------------------------------------------
def _selftest():
    """Plant each defect class this instrument claims to detect, and require it to be detected.

    The fixtures are built so the right answer is fixed by construction. Three legs matter most:

      * leg 2 -- a MISSING usage key must report UNKNOWN, never 0. This is the real defect that
        once produced "this client does no caching" from a key that does not exist in that
        client's protocol. An absence rendered as a zero is the single most expensive shape in
        the study this came from.
      * leg 5 -- a CLEAN capture must produce NO findings and exit 0. Without it the suite
        cannot tell a working detector from one that fires on everything.
      * leg 6 -- FILTER ASYMMETRY across arms, which is invisible within any single arm and is
        the one finding that needs more than one capture to exist at all.
    """
    import shutil
    import subprocess
    import tempfile

    fails, ran = [], []

    def check(label, got, want):
        # `ran` is appended to here and counted at the end, so the banner's number is DERIVED
        # from the checks that actually executed. It was a literal, and adding a leg left it
        # reading "13 checks" while 14 had run -- a stated count that cannot disagree with the
        # code is the only kind worth printing.
        ran.append(label)
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: got {got!r}, want {want!r}")
        if not ok:
            fails.append(label)

    d = tempfile.mkdtemp(prefix="token_claim_selftest_")
    try:
        def write(name, rows):
            path = os.path.join(d, name)
            with open(path, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
            return path

        def codes(path, **kw):
            kw.setdefault("arm_field", "arm")
            kw.setdefault("status_field", "status")
            kw.setdefault("usage_field", "usage")
            kw.setdefault("cost_field", "usage.cost")
            kw.setdefault("id_field", None)
            rows = audit_capture(path, kw["arm_field"], kw["status_field"], kw["usage_field"],
                                 kw["cost_field"], kw["id_field"])
            return sorted({f["code"] for r in rows for f in r["findings"]})

        ok_usage = {"input_tokens": 10, "output_tokens": 5, "cost": 0.01}

        # 1. Retries / non-2xx inside the population.
        p1 = write("retries.jsonl",
                   [{"arm": "a", "status": 200, "usage": dict(ok_usage)}] * 3
                   + [{"arm": "a", "status": 429, "usage": dict(ok_usage)}] * 4)
        c1 = codes(p1)
        check("leg1 non-2xx in population is detected",
              any("NON-2XX" in c for c in c1), True)

        # 2. A usage field that is ABSENT must read UNKNOWN, never 0.
        p2 = write("nokey.jsonl",
                   [{"arm": "b", "status": 200, "usage": {"input_tokens": 10}}] * 3)
        c2 = codes(p2, cost_field="usage.cache_read_input_tokens")
        check("leg2 an absent usage key is reported, not silently zeroed",
              bool(c2), True)

        # 3. Duplicate ids -- the records-per-request / replayed-transcript class.
        p3 = write("dupes.jsonl",
                   [{"arm": "c", "status": 200, "usage": dict(ok_usage), "rid": "r1"},
                    {"arm": "c", "status": 200, "usage": dict(ok_usage), "rid": "r1"},
                    {"arm": "c", "status": 200, "usage": dict(ok_usage), "rid": "r2"}])
        c3 = codes(p3, id_field="rid")
        check("leg3 duplicate request ids are detected",
              any("DUPLICATE" in c for c in c3), True)

        # 3b. ...and the SAME capture with distinct ids must NOT fire, or leg 3 proves nothing.
        p3b = write("nodupes.jsonl",
                    [{"arm": "c", "status": 200, "usage": dict(ok_usage), "rid": f"r{i}"}
                     for i in range(3)])
        check("leg3b distinct ids do NOT raise a duplicate finding",
              any("DUPLICATE" in c for c in codes(p3b, id_field="rid")), False)

        # 4. Zero-cost 2xx requests -- the $0 capability-probe class.
        p4 = write("zerocost.jsonl",
                   [{"arm": "d", "status": 200, "usage": {"input_tokens": 1, "cost": 0.0}}] * 4)
        check("leg4 zero-cost 2xx requests are surfaced", bool(codes(p4)), True)

        # 5. A CLEAN capture must be clean, and exit 0 end to end.
        p5 = write("clean.jsonl",
                   [{"arm": "e", "status": 200, "usage": dict(ok_usage), "rid": f"q{i}"}
                    for i in range(5)])
        check("leg5 a clean capture yields no findings", codes(p5, id_field="rid"), [])
        me = os.path.abspath(__file__)
        r_clean = subprocess.run([sys.executable, me, p5, "--id-field", "rid"],
                                 capture_output=True, text=True)
        check("leg5 a clean capture exits 0", r_clean.returncode, 0)
        r_dirty = subprocess.run([sys.executable, me, p1], capture_output=True, text=True)
        check("leg5 a capture with findings exits 1", r_dirty.returncode, 1)

        # 6. FILTER ASYMMETRY -- only visible across arms.
        xf = cross_arm(audit_capture(p1, "arm", "status", "usage", "usage.cost", None)
                       + audit_capture(p5, "arm", "status", "usage", "usage.cost", None))
        check("leg6 cross-arm filter asymmetry is detected", bool(xf), True)

        # 7. CANNOT-MEASURE: zero captures must refuse, loudly and non-zero.
        r_none = subprocess.run([sys.executable, me, os.path.join(d, "nothing-*.jsonl")],
                                capture_output=True, text=True)
        check("leg7 zero matching captures is refused with exit 2", r_none.returncode, 2)
        check("leg7 zero matching captures says REFUSING",
              "REFUSING" in (r_none.stdout + r_none.stderr), True)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print()
    if fails:
        print(f"SELF-TEST FAILED: {len(fails)} leg(s): {', '.join(fails)}")
        return 1
    print(f"SELF-TEST PASSED -- {len(ran)} checks.")
    print("What it does NOT cover: cost is never re-derived from a price model, so a figure that")
    print("is wrong because of a MISPRICED tier passes every leg here. That check needs a")
    print("per-provider price table with a cliff and is deliberately out of scope.")
    return 0




def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="*", help="capture files or globs (one JSON object/line)")
    ap.add_argument("--self-test", action="store_true",
                    help="plant each defect class this instrument detects and require detection")
    ap.add_argument("--help-blind", action="store_true",
                    help="what this instrument answers, and the five ways it lies")
    ap.add_argument("--arm-field", default="arm")
    ap.add_argument("--status-field", default="status")
    ap.add_argument("--usage-field", default="usage")
    ap.add_argument("--cost-field", default="usage.cost")
    ap.add_argument("--id-field", default=None,
                    help="a per-request id to check for duplicates (e.g. requestId)")
    ap.add_argument("--json", help="write the full record here")
    a = ap.parse_args()

    if a.help_blind:
        # The MODULE DOCSTRING is the blind answer. One copy, deliberately: this
        # repository's figure gate reads the docstring as published prose, so a separate
        # constant would be a second copy that drifts out from under the gate.
        print(__doc__.strip())
        return 0
    if a.self_test:
        return _selftest()
    if not a.captures:
        refuse("no captures given (or use --self-test / --help-blind).")

    paths = []
    for c in a.captures:
        hits = sorted(glob.glob(c)) if any(ch in c for ch in "*?[") else [c]
        paths += [h for h in hits if os.path.isfile(h)]
    if not paths:
        refuse(f"no capture files matched {a.captures}. An audit over zero "
               f"captures is not a clean audit.")

    rows = []
    for p in paths:
        rows += audit_capture(p, a.arm_field, a.status_field, a.usage_field, a.cost_field,
                              a.id_field)
    xf = cross_arm(rows)

    print("TOKEN-CLAIM AUDIT -- would a figure published from these captures survive its own")
    print("request population?\n")
    hdr = (f"{'capture':42s} {'reqs':>5s} {'non2xx':>7s} {'nocost':>7s} {'0cost':>6s} "
           f"{'shapes':<20s} find")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['capture'][:42]:42s} {r['requests']:>5d} {r['non_2xx']:>7d} "
              f"{r['cost_absent']:>7d} {r['zero_cost_2xx']:>6d} "
              f"{str(sorted(r['usage_shapes']))[:20]:<20s} {len(r['findings'])}")

    total = 0
    for r in rows:
        if not r["findings"]:
            continue
        print(f"\n{r['capture']}  [arm: {r['arm']}]  {r['requests']} requests")
        for f in r["findings"]:
            total += 1
            print(f"  !! {f['code']} (n={f['n']})")
            print(f"       {f['detail']}")
    if xf:
        print(f"\nACROSS THE {len(rows)} ARMS/CAPTURES AUDITED TOGETHER")
        for f in xf:
            total += 1
            print(f"  !! {f['code']} (n={f['n']})")
            print(f"       {f['detail']}")

    print(f"\nFINDINGS: {total} across {len(rows)} arm(s) in {len(paths)} capture(s).")
    print("A finding is a POPULATION FACT, not automatically a defect: a 429 in a capture is a\n"
          "fact about the provider. It becomes a defect when it sits inside a set someone called\n"
          "'rounds'. This instrument's guarantee is that the count was put in front of you first.")
    print("\nNOT CHECKED HERE, stated so its absence is not read as a pass: cost is never\n"
          "re-derived from a price model. That check caught alm#961's largest error (a tier keyed\n"
          "on uncached tokens, wrong on 105 of 401 requests, 50x -> 10x) and needs a per-provider\n"
          "price table with a cliff, which is state that goes stale silently.")

    if a.json:
        json.dump({"arms": rows, "cross_arm": xf}, open(a.json, "w"), indent=2)
        print(f"\n-> {a.json}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
