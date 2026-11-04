"""Revert each fix the request-construction instruments make; assert a control turns RED.

Driven by scripts/test_request_construction_controls.sh. Same shape, and the same reason,
as scripts/test_flags_controls.py: a gate that has only ever been seen passing is
indistinguishable from a gate that cannot fail, and a self-test whose checks pin the
REVIEWER'S FINDINGS rather than the FIXES leaves the fixes unprotected. That happened on
this repository before -- 13 of 53 mutants left the suite green -- so a new instrument
arrives with this gate rather than acquiring one after the next review.

THE RULE: every behaviour `measure_request_construction.py` or `capture_proxy.py` relies on
ships with an input that distinguishes it from the code it replaced. Each mutation below
is a REVERT to a plausible, simpler implementation -- the one somebody would actually
write -- and not arbitrary damage.

Two choices carried over deliberately from the gate this is modelled on:

  * Mutations are applied to a COPY of instruments/ in a temp directory. The real tree is
    never written to, so an interrupted run cannot leave a mutant behind.
  * Every mutation asserts its anchor matched EXACTLY ONCE before anything runs. A
    `str.replace` that matches nothing reports "equivalent" for a mutation that never
    happened, which is a probe that cannot fail hiding a control that cannot fail.
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
    # C1 reverts BOTH halves at once, and that is deliberate. Framing-as-the-remainder is
    # arithmetically IDENTICAL to the structural prediction whenever the split is
    # complete -- this gate's own first run proved it, GREEN, and that run is kept at
    # samples/gate-evidence/test_request_construction_controls.RED.out. So it is not a
    # defect by itself; it is the ENABLER. Together with a narrowed split it makes the
    # coverage equality tautological, which is exactly the implementation somebody would
    # really write. Reverting one half alone would pin a difference that does not exist.
    ("C1-three-columns-and-remainder-framing", "measure_request_construction",
     "the familiar system/tools/messages decomposition WITH framing defined as the "
     "remainder. Its own arithmetic closes perfectly, so the coverage equality cannot "
     "fail, and on the real dsh capture it reports a quarter of the payload as the whole "
     "request.",
     '''    fields = {k: nbytes(v) for k, v in body.items()}
    whole = nbytes(body)
    framing = predicted_framing(body)''',
     '''    fields = {k: nbytes(v) for k, v in body.items()
              if k in ("system", "tools", "messages")}
    whole = nbytes(body)
    framing = whole - sum(fields.values())'''),

    ("C2-coverage-three-columns", "measure_request_construction",
     "splitting only system/tools/messages -- the decomposition every comparable tool "
     "ships -- while leaving framing honestly predicted. On the real dsh capture that "
     "omits three quarters of the body while printing a complete-looking table.",
     '    fields = {k: nbytes(v) for k, v in body.items()}',
     '    fields = {k: nbytes(v) for k, v in body.items()\n'
     '              if k in ("system", "tools", "messages")}'),

    ("C3-probe-counts-as-turn", "measure_request_construction",
     "counting every captured line as a request. A client's startup probes land in the "
     "same capture, so a run where the agent never constructed a turn reports a "
     "measurement instead of refusing.",
     '    return not (body.get("tools") or body.get("messages"))',
     '    return False'),

    ("C4-unknown-field-ignored", "measure_request_construction",
     "letting an unrecognised body field pass unreported. The next engine's own "
     "invention then costs real bytes that appear in no column.",
     '        "unknown_fields": sorted(set(body) - KNOWN_FIELDS),',
     '        "unknown_fields": [],'),

    ("C5-metadata-billed-as-input", "measure_request_construction",
     "counting provider metadata as model input -- the exact error that inverted the "
     "source study's headline: dsh went from ~44% cheaper to ~20% more expensive on the "
     "same bytes.",
     '        "model_input_bytes": whole - meta,',
     '        "model_input_bytes": whole,'),

    ("C6-openai-system-as-conversation", "measure_request_construction",
     "leaving an OpenAI `role: system` message in the conversation column. The fixed "
     "per-turn overhead then reads as conversation for one protocol and as system for "
     "the other, and the two arms are no longer comparable.",
     '''    sysm, convo = [], []                        # OpenAI chat completions
    for m in msgs:
        is_instr = isinstance(m, dict) and m.get("role") in INSTRUCTION_ROLES
        (sysm if is_instr else convo).append(m)
    return (sysm or None), tools, convo''',
     '''    return None, tools, list(msgs)'''),

    ("C7-one-assumed-model", "measure_request_construction",
     "grouping the whole capture under one model instead of the model each request "
     "NAMED. Pricing a capture against the model you configured rather than the one the "
     "request carried is how a published cost comparison goes wrong.",
     '        g = by_model.setdefault(row["model_named"], {',
     '        g = by_model.setdefault(rep["rows"][0]["model_named"], {'),

    ("C8-corrupt-line-skipped", "measure_request_construction",
     "skipping an unreadable capture line and measuring the rest. The missing lines "
     "shrink every total silently, which is the one failure that looks like good news.",
     '''            except Exception as e:  # noqa: BLE001
                return None, ("capture line %d of %s is not JSON (%r). A partially "
                              "readable capture is refused rather than measured: the "
                              "missing lines would silently shrink every total."
                              % (n, path, e))''',
     '''            except Exception:  # noqa: BLE001
                continue'''),

    ("C9-claims-whitespace-exact", "measure_request_construction",
     "comparing a published claim to the prose by exact substring only. The gate this "
     "was ported from failed on a claim that had WRAPPED across a markdown line break -- "
     "the number was right and the gate said drift, which is how a gate gets switched "
     "off and then stops protecting the numbers too.",
     '        hit = claim in text or flatten_ws(claim) in flat',
     '        hit = claim in text'),

    ("C10-faithfulness-always-ok", "measure_request_construction",
     "a faithfulness check that cannot fail. Its only value is that a fresh capture CAN "
     "fail to reproduce a stored one; asserted rather than checked it is worse than "
     "absent, because it licenses everything built on top.",
     '''    ok = True
    for f in FAITHFULNESS_FIELDS:
        x, y = a[f], b[f]
        mark = "MATCH" if x == y else "%+d  <-- DIFFERS" % (x - y)
        if x != y:
            ok = False
        lines.append("  %-26s %12d %12d  %s" % (f, x, y, mark))''',
     '''    ok = True
    for f in FAITHFULNESS_FIELDS:
        x, y = a[f], b[f]
        lines.append("  %-26s %12d %12d  %s" % (f, x, y, "MATCH"))'''),

    ("C11-empty-reference-passes", "measure_request_construction",
     "treating a reference capture with no measurable request as a PASS. 'nothing to "
     "compare against' then reads exactly like 'it reproduced'.",
     '''    if not ref:
        return ["FAITHFULNESS: CANNOT CHECK -- reference capture has no measurable "
                "request in it"], False''',
     '''    if not ref:
        return ["FAITHFULNESS: nothing to compare"], True'''),

    ("C12-recorder-logs-header-values", "capture_proxy",
     "recording header VALUES as well as names. The Authorization / x-api-key value of a "
     "misconfigured run then lands in a capture file whose whole purpose is to be "
     "publishable.",
     '        "header_names": sorted(headers.keys()),',
     '        "header_names": sorted(headers.items()),'),

    # ---------------------------------------------------------------------------
    # C14-C23 pin the fixes made after an independent review of this branch returned
    # BLOCK. Every one of them is a revert to the code that was really there, or to the
    # obvious implementation the review substituted and found the whole preflight green
    # against. They are listed in the order the findings were reported.
    ("C14-no-shape-check-at-the-door", "measure_request_construction",
     "appending whatever parsed, which is the obvious implementation and was the real "
     "one: a capture line that is valid JSON but a LIST or a STRING then died with "
     "`AttributeError: 'list' object has no attribute 'get'` inside is_probe -- a "
     "traceback naming a dict method, with no capture file, no line number, and exit 1, "
     "which is not in this instrument's documented set of exit codes.",
     """            fault = structural_fault(rec)
            if fault:
                return None, ("capture line %d of %s %s. The whole capture is refused "
                              "rather than measured around it: skipping the line would "
                              "shrink every total silently."
                              % (n, path, fault))
            recs.append(rec)""",
     """            recs.append(rec)"""),

    ("C15-shape-fault-skips-the-line", "measure_request_construction",
     "skipping the unreadable record and measuring the rest -- the SOFTER revert, and "
     "the more tempting one, because it still produces a table. It is the C8 failure "
     "with a different cause: the dropped record shrinks every total, and a total that "
     "came out smaller is the one wrong answer nobody goes looking for.",
     """            if fault:
                return None, ("capture line %d of %s %s. The whole capture is refused "
                              "rather than measured around it: skipping the line would "
                              "shrink every total silently."
                              % (n, path, fault))""",
     """            if fault:
                continue"""),

    ("C16-duplicate-keys-lost-silently", "measure_request_construction",
     "parsing with a plain json.loads, which keeps the LAST value for a repeated key and "
     "drops the rest without a word. `{\"system\": <10 KB>, \"system\": \"x\"}` then "
     "measured a 3-byte system prompt, the coverage invariant held because the PARSED "
     "body really is self-consistent, and the published fixed-per-turn cost was 10 KB "
     "too low with no symptom.",
     """                rec = json.loads(line,
                                 object_pairs_hook=lambda p: duplicate_keys(p, dups))""",
     """                rec = json.loads(line)"""),

    ("C17-unparseable-measured-around", "measure_request_construction",
     "measuring a capture that contains requests the RECORDER could not parse. They "
     "carry no `tools` and no `messages`, so they were filed as startup probes: a review "
     "fed in three 48 KB records and got `3 startup probes`, a 100% coverage banner and "
     "exit 0. 144 KB of real traffic not omitted but MISLABELLED, which is worse, "
     "because the count looks accounted for.",
     """        unp = [(i, r) for i, r in enumerate(recs, 1)
               if UNPARSEABLE in (r.get("body") or {})]""",
     """        unp = []"""),

    ("C18-unparseable-is-a-probe", "measure_request_construction",
     "the one-line classification the refusal above rests on. Reverting it alone is "
     "quieter than C17 and that is exactly why it is a separate case: with is_probe "
     "calling an unparseable body a probe again, any future code path that trusts "
     "is_probe rather than re-scanning for the marker silently inherits the old bug.",
     """    if UNPARSEABLE in body:""",
     """    if False:"""),

    ("C19-coverage-allows-a-deficit", "measure_request_construction",
     "the headline invariant's comparison, `==` relaxed to `<=`. One character, and it "
     "passes every DEFICIT -- the only direction a dropped field can go. A review "
     "mutated it and the entire preflight stayed green, because every other control "
     "feeds a complete split, where the two operators agree.",
     """    return summed + framing == whole""",
     """    return summed + framing <= whole"""),

    ("C20-framing-ignores-key-escaping", "measure_request_construction",
     "framing predicted as `len(key) + 2` instead of over the SERIALIZED key. The two "
     "agree for every short ASCII key, which is what every fixture used, so a review "
     "mutated this and nothing noticed. A multibyte key costs more bytes than "
     "characters and a key needing escapes costs more serialized than raw, so on a real "
     "non-English payload the coverage invariant would start failing for no visible "
     "reason -- or worse, be relaxed to make it pass.",
     """    per_key = sum(len(ser(k).encode("utf-8")) + len(": ".encode("utf-8")) for k in body)""",
     """    per_key = sum(len(k) + 2 + len(": ".encode("utf-8")) for k in body)"""),

    ("C21-developer-role-as-conversation", "measure_request_construction",
     "recognising only `role: system` on the OpenAI side. `developer` is what OpenAI "
     "RENAMED it to and both are live, so a 12 KB instruction block landed in the "
     "conversation column and the instrument published `fixed B = 49`. This is case C6's "
     "defect exactly, for the spelling the provider documents today.",
     """        is_instr = isinstance(m, dict) and m.get("role") in INSTRUCTION_ROLES""",
     '        is_instr = isinstance(m, dict) and m.get("role") == "system"'),

    ("C22-tokeniser-guard-not-consulted", "measure_request_construction",
     "loading an encoder without putting it past the bytes/4 guard. tiktoken is not a "
     "dependency here and is not installed, so the token path has no executed coverage "
     "at all: a review replaced the encoder with `len(bytes)//4` and every gate stayed "
     "green while the output still called itself `tiktoken:cl100k_base`.",
     """    bad = looks_like_bytes_over_4(count)
    if bad:
        return None, bad
    return count, label""",
     """    return count, label"""),

    ("C23-wire-bytes-re-derived", "measure_request_construction",
     "deriving the wire figure from the parsed body instead of reading what the recorder "
     "OBSERVED. `wire_bytes` is documented here as the only direct observation in the "
     "whole report, and it was untestable until recently because the fixture helper set "
     "raw_bytes to the body's own length -- making the two equal by construction, so the "
     "substitution changed nothing any control could see.",
     """        "wire_bytes": rec.get("raw_bytes", 0),""",
     """        "wire_bytes": whole,"""),

    ("C13-recorder-keepalive-sse", "capture_proxy",
     "answering an event stream under keep-alive. With no Content-Length the client "
     "cannot tell the body ended and blocks until its own timeout -- and a recorder that "
     "hangs the client reads as a client that never sent anything.",
     '        self.close_connection = True',
     '        self.close_connection = False'),
]


def run_selftest(base, module):
    """-> (returncode, [FAIL/PASS-absent lines]). Reads the mutant's own self-test."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    p = subprocess.run([sys.executable,
                        os.path.join(base, "instruments", module + ".py"),
                        "--self-test"],
                       capture_output=True, text=True, cwd=base, env=env, timeout=600)
    fails = [l.strip() for l in (p.stdout + p.stderr).splitlines()
             if l.strip().startswith("FAIL")]
    return p.returncode, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args()
    if a.list:
        for name, mod, why, _, _ in CASES:
            print("%-34s %-32s %s" % (name, mod, why.split(".")[0]))
        return 0
    cases = [c for c in CASES if not a.only or c[0] == a.only]

    print("REVERT-EACH-FIX GATE (request construction) -- %d cases" % len(cases))
    print("Each reverts one fix to the simpler code somebody would really write; a "
          "control must turn RED.")
    print()
    tmp = tempfile.mkdtemp(prefix="ari-mrc-controls-")
    gaps, errors = [], []
    try:
        base = os.path.join(tmp, "__baseline__")
        shutil.copytree(SRC, os.path.join(base, "instruments"))
        for mod in ("measure_request_construction", "capture_proxy"):
            rc, fails = run_selftest(base, mod)
            if rc != 0:
                print("ABORT: the UNMUTATED copy of %s already fails -- nothing below "
                      "would mean anything.\n  %s" % (mod, "\n  ".join(fails)))
                return 2
        print("baseline: the unmutated copies of both files PASS their self-tests, so a "
              "RED below is caused by the mutation")
        print()

        for name, mod, why, old, new in cases:
            path = os.path.join(SRC, mod + ".py")
            text = open(path, encoding="utf-8").read()
            n = text.count(old)
            if n != 1:
                errors.append((name, n))
                print("%-34s ANCHOR LOST (%d matches) -- the mutation never happened, "
                      "which is NOT a pass" % (name, n))
                continue
            work = os.path.join(tmp, name)
            shutil.copytree(SRC, os.path.join(work, "instruments"))
            with open(os.path.join(work, "instruments", mod + ".py"), "w",
                      encoding="utf-8") as fh:
                fh.write(text.replace(old, new, 1))
            rc, fails = run_selftest(work, mod)
            if rc != 0 and fails:
                print("%-34s RED   -- %s" % (name, fails[0][:88]))
            elif rc != 0:
                print("%-34s RED   -- exit %d (no FAIL line; a raise or a hang, which "
                      "still counts)" % (name, rc))
            else:
                gaps.append((name, why))
                print("%-34s GREEN -- COVERAGE GAP" % name)
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
            print("    what the revert costs: %s" % why)
        return 1
    if errors:
        return 1
    print("PASSED -- all %d fixes are pinned: reverting any one of them turns a control "
          "RED." % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
