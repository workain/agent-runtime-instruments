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
import re
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
     '''    instr, convo = [], []
    for m in msgs:
        is_instr = isinstance(m, dict) and m.get("role") in INSTRUCTION_ROLES
        (instr if is_instr else convo).append(m)''',
     '''    instr, convo = [], []
    for m in msgs:
        convo.append(m)'''),

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
     "publishable. This anchors the ONE site that builds the list -- a round-3 branch "
     "added a second copy of it, and because this case anchored on the other one, "
     "mutating the new copy leaked a real `Bearer` value into a capture with every gate "
     "green. The duplication was removed rather than given a second gate case.",
     '    header_names = sorted(headers.keys())       # KEYS only -- see above. One site.',
     '    header_names = sorted(headers.items())'),

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
     """    bad = looks_like_a_fixed_divisor(count)
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

    # ---------------------------------------------------------------------------
    # C24-C35 pin the fixes made after a SECOND review, which verified round 1's fixes
    # and found ten more things. Four of its own mutations survived the whole preflight,
    # and one of those was round 1's fourth survivor answered on the wrong side of the
    # wire -- which is why four of these cases live in capture_proxy.py, where the
    # observation is actually made rather than where it is read.
    ("C24-refusal-not-fatal-beside-a-good-capture", "measure_request_construction",
     "refusing only when NOTHING measured. Adding one valid capture to the command line then defeated every refusal in this file at once: the refusal printed, then a complete table, then exit 0 -- in samples/...both.out's own invocation shape, the one a two-engine comparison actually uses. The `requests: N captured, 1 measurable` line did not even add up, and nothing said so.",
     '''    if rep["errors"]:
        return rep, 2''',
     '''    if rep["errors"] and not rep["rows"]:
        return rep, 2'''),

    ("C25-raw-bytes-unvalidated", "measure_request_construction",
     'a door-check that validates the record and the body and stops short of raw_bytes -- the one field this report calls a direct observation. Absent or 0 divided by zero inside the wire-overrun note; null or a string hit `int += None` in the per-model totals. Four shapes, all exit 1 with a traceback naming no file and no line, which is the class this door-check exists to end. The mutation DELETES the four checks, which is what the code looked like before them: an earlier version of this case neutered only the first and left the rest, so the mutant refused everything with a body and went RED for a reason this case does not name -- arbitrary damage reads as a passing control, which a review caught.',
     '''    raw = rec.get("raw_bytes")
    if raw is None:
        return ("has no `raw_bytes`. That field is the wire length as OBSERVED, and the "
                "only figure in this report that is not computed; defaulting it to 0 "
                "would publish a zero-byte request, and every ratio taken against it "
                "divides by that zero")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return ("has a `raw_bytes` that is not an integer (it is %s). A byte count read "
                "from a socket is an int; a string that looks like one is a recorder "
                "that changed shape, which is worth stopping for"
                % type(raw).__name__)
    if raw < 0:
        return "has a negative `raw_bytes` (%d), which no observation produces" % raw
    if raw == 0 and body:
        return ("has `raw_bytes: 0` but a body that parsed into %d field(s). Those two "
                "cannot both be observations of the same request, and every ratio taken "
                "against the wire figure divides by that zero" % len(body))
''',
     '''    raw = rec.get("raw_bytes")
'''),

    ("C26-counted-containers-unchecked", "measure_request_construction",
     'trusting `tools` and `messages` to be lists. Their lengths are PUBLISHED counts and len() obliges on anything: a dict for tools reported `2 tool schema(s) exposed` from its keys, a string for messages reported `5 message(s)` from its characters, both at exit 0. The coverage invariant is over top-level fields and cannot see inside one.',
     '        for k in ("tools", "messages"):',
     '        for k in ():'),

    ("C27-anthropic-branch-ignores-roles", "measure_request_construction",
     'returning the whole `messages` list as conversation whenever a top-level `system` is present. A body carrying BOTH a top-level system and a 5000-byte role:system message then published `fixed B = 33` with 5070 bytes filed as conversation -- the developer-role defect reached through the other protocol branch, on the headline figure.',
     '''    parts = []
    if body.get("system"):                      # Anthropic Messages
        parts.append(body["system"])''',
     '''    if body.get("system"):
        return [body["system"]], tools, list(msgs)
    parts = []
    if False:
        parts.append(body["system"])'''),

    ("C28-divisor-guard-pins-one-spelling", "measure_request_construction",
     'checking for the literal len(bytes)//4 instead of for a constant ratio. A reviewer who wrote round(len/4), (len+3)//4, len//4+1, len//5 or int(len/3.8) was accepted and labelled tiktoken:cl100k_base -- five of six spellings through a guard whose documentation claimed it rejected division.',
     '''    ratios = []
    for p in _TOK_PROBES:''',
     '''    if all(count(p) == len(p.encode("utf-8")) // 4 for p in _TOK_PROBES):
        return "it is exactly len(bytes)//4"
    return None
    ratios = []
    for p in _TOK_PROBES:'''),

    ("C29-claims-pin-only-the-first-arm", "measure_request_construction",
     'pinning rows[0] only. Nothing then pinned a single figure of the second engine, so the whole right-hand column of the published comparison -- or a swap of the two columns -- passed every gate. A per-arm clause is also the only pinnable form for a small integer: `9` occurs in 62 artefacts here by plain substring.',
     '''    for arm, r in first_by_arm.items():
        out.extend(_claims_for_arm(arm, r))
    return out''',
     '''    arm0 = rep["rows"][0]["arm"]
    out.extend(_claims_for_arm(arm0, first_by_arm[arm0]))
    return out'''),

    ("C30-metadata-share-over-the-wire", "measure_request_construction",
     'dividing the provider-metadata share by the WIRE figure instead of the body. 6519/8225 is 79.3% and 6519/8680 is 75.1%; publishing the first under a sentence naming the second is precisely the error a review found in the prose, and until now the code side of it had no control at all.',
     '        pct = 100.0 * r["provider_metadata_bytes"] / r["body_bytes"]',
     '        pct = 100.0 * r["provider_metadata_bytes"] / r["wire_bytes"]'),

    ("C31-probe-test-ignores-tools", "measure_request_construction",
     'keying the probe test on `messages` alone. A tools-only request is then filed as a startup probe and its bytes leave every total silently -- the omission that looks like good news.',
     '    return not (body.get("tools") or body.get("messages"))',
     '    return not body.get("messages")'),

    ("C32-recorder-reserialises-wire-bytes", "capture_proxy",
     "computing raw_bytes from the PARSED body instead of the bytes that arrived. The field is documented as the only direct observation in the chain, and README's table marks exactly one row as observed. C23 pins where that figure is READ; this pins where it is MADE. The old self-test compared raw_bytes against len(json.dumps(body).encode()) -- which is what this mutant computes, so the check could not tell them apart.",
     '        "raw_bytes": len(raw),',
     '        "raw_bytes": len(json.dumps(body).encode()) if body else 0,'),

    ("C33-recorder-blanks-unparseable", "capture_proxy",
     "storing an empty body for a request this could not parse. Downstream it carries no tools and no messages, so it counts as a startup probe: a review got `3 startup probes` for 144 KB of real traffic, with a 100% coverage banner at exit 0. Round 1's finding 3, reproduced from the recorder side where no reader-side refusal can see it.",
     '            body = {"__unparseable__": raw.decode("utf-8", "replace")[:2000]}',
     '            body = {}'),

    ("C34-recorder-drops-unparseable", "capture_proxy",
     "returning before writing when the body did not parse -- the softer form of C33, and the first review's own fourth preflight survivor. A refusal downstream cannot see a line that was never written, which is why this case has to live here.",
     '    with open(capture, "a", encoding="utf-8") as fh:',
     '''    if isinstance(body, dict) and "__unparseable__" in body:
        return rec
    with open(capture, "a", encoding="utf-8") as fh:'''),

    ("C35-empty-post-marked-unparseable", "capture_proxy",
     'treating a zero-length payload as a body that FAILED to parse. A Content-Length: 0 health check then made the measuring instrument refuse the ENTIRE capture, with a refusal whose own evidence said `0 raw bytes` were affected -- a good capture thrown away over nothing, recoverable only by capturing again.',
     '    if not raw:',
     '    if False:'),

    # ---------------------------------------------------------------------------
    # C36-C41 pin the fixes made after a THIRD review, which was asked to look first for
    # regressions the round-2 fixes had introduced -- because round 1's fixes had already
    # introduced one. It found two, and four more mutations that survived both self-tests
    # and the whole preflight. Three of those four are in capture_proxy.py again: the
    # recorder keeps being the place where a defect cannot be seen from the reader side.
    ("C36-only-the-first-instruction-message", "measure_request_construction",
     "counting only the FIRST instruction-role message. Six characters in the function round 2 rewrote; it survived 51 self-test checks and the whole preflight, because no fixture anywhere carried two instruction-role messages and this leg asserted `system_bytes > 5000` when the exact value was available. Every later message's bytes leave system_bytes, fixed_bytes, system_tokens AND conversation_bytes -- 6038 of them in the review's demonstration, in no column at all, at exit 0. The coverage invariant is over top-level fields and cannot see inside `messages`.",
     '        parts.append(instr)',
     '        parts.append(instr[:1])'),

    ("C37-recorder-decodes-as-ascii", "capture_proxy",
     'decoding the wire bytes as ascii. ONE WORD, and it survived both self-tests and the whole preflight because every body the self-test sent was ASCII -- while samples/fixtures/request_construction.hermes.jsonl already is not. On a CJK payload the replacement characters inflated a 6303-byte body to 18303: a 2.9x overstatement of the headline figure at exit 0, coverage green, whose one visible symptom was the round-1 overrun note telling the reader it was normal.',
     '            body = json.loads(raw.decode("utf-8", "replace"))\n        except Exception:\n            # An unparseable body',
     '            body = json.loads(raw.decode("ascii", "replace"))\n        except Exception:\n            # An unparseable body'),

    ("C38-chunked-body-never-read", "capture_proxy",
     "framing every request by Content-Length alone. A chunked POST carries none, so the recorder read ZERO bytes and wrote `raw_bytes: 0, body: null` -- which the measuring instrument counts as a startup probe: 40391 real bytes reported as `1 measurable, 1 startup probes` with a 100% coverage banner at exit 0. node's own fetch with a stream body sends exactly this, and urllib cannot construct one, which is why no leg here had ever sent one.",
     '        te = (self.headers.get("Transfer-Encoding") or "").strip().lower()',
     '        te = ""'),

    ("C39-unframeable-body-posed-as-empty", "capture_proxy",
     "recording a body that could not be FRAMED as though nothing had been sent. `Content-Length: 0` and `this recorder never read it` are different facts, and last round's empty-POST fix conflated them in the other direction: length UNKNOWN became length ZERO, which reads as an observation and files real traffic under startup probes.",
     '    if unread is not None:',
     '    if False:'),

    ("C40-overrun-note-without-a-ceiling", "measure_request_construction",
     'printing `Not an error` over an overrun of ANY size. A normalised split can legitimately exceed the wire figure, but only by one byte per separator; past that it is not a re-serialisation of those bytes at all. Without the bound, the recorder mutation C37 publishes a 290.4% overrun under a note that reassures the reader, at exit 0. The two fixtures either side of the boundary differ by ONE byte of wire, so the ceiling is the bound and not a threshold somebody picked.',
     '            if row["body_bytes"] - row["wire_bytes"] > row["separator_slack"] > 0:',
     '            if False:'),

    ("C41-two-captures-one-arm-label", "measure_request_construction",
     "merging two captures that share an arm label. The table and the claims are keyed by arm and keep the FIRST request per arm, so the second capture's figures are counted in `measurable` and appear in no row and in no claim. That is round-2 finding 3 -- one whole engine pinned by nothing -- reached by label collision instead of by rows[0], and a review reproduced it with this repository's own two committed dsh fixtures.",
     '            if first_seen != p:',
     '            if False:'),

    # ---------------------------------------------------------------------------
    # C42-C49 pin the fixes made after a FOURTH review, which found that round 3's own
    # R1 fix (reading each prose file as written AS WELL AS joined) false-alarmed on
    # ordinary prose, and that round 3 had added a SECOND copy of the one line in
    # capture_proxy.py whose mistake is irreversible -- so C12 anchored on one of them
    # while the other leaked a real credential with every gate green. Six of these eight
    # are in capture_proxy.py, which is now the third round running that the recorder has
    # been the file where a defect cannot be seen from the reader side.
    ("C42-recorder-loses-an-unencodable-record", "capture_proxy",
     'letting a record that cannot be encoded as UTF-8 take the handler down. A lone surrogate -- what a client emits when it truncates a string mid-character, and JSON.stringify of a sliced emoji really does -- is valid JSON on the wire and raised inside fh.write(), so the request appeared in the capture NOT AT ALL: no record, no reply, nothing to notice. The omission that looks like good news, in the one file whose output is the evidence for everything else. json.dumps does not raise on it, which is why the encodability check has to be explicit.',
     '    line = json.dumps(rec, ensure_ascii=False)\n    try:',
     '    line = json.dumps(rec, ensure_ascii=False)\n    if True:\n        pass\n    elif False:'),

    ("C43-transfer-encoding-read-as-one-token", "capture_proxy",
     'comparing the whole Transfer-Encoding value to `chunked` instead of looking for the token in it. RFC 7230 s3.3.1 makes it a comma-separated LIST, and a proxy in the path really sends `identity, chunked` -- which then stops being de-chunked and becomes a FATAL refusal of the whole capture. A false refusal, from a fix for a missed measurement.',
     '            if "chunked" in te:',
     '            if te == "chunked":'),

    ("C44-transfer-encoding-case-sensitive", "capture_proxy",
     "dropping .lower() on the Transfer-Encoding value. The token is case-insensitive, so `Transfer-Encoding: Chunked` stops being de-chunked. Until a self-test leg sent a capitalised one, the only thing that killed this mutation was ANOTHER gate case's anchor text -- unpinned behaviour protected by a string literal in a different file, which is not protection.",
     '.strip().lower()',
     '.strip()'),

    ("C45-negative-content-length", "capture_proxy",
     'handing a negative Content-Length to rfile.read(-1), which reads to EOF. The body is then recorded framed by nothing at all and the reply is never delivered.',
     '        if n < 0:',
     '        if False:'),

    ("C46-chunked-trailer-never-ends", "capture_proxy",
     "dropping the EOF case from the chunked trailer loop. A client that closes after its last chunk without sending the final CRLF then leaves the recorder spinning in readline() FOREVER: it records nothing and reads from outside as a client that never arrived, which is the failure this file's own streaming leg already exists to prevent.",
     '                    if t in (b"\\r\\n", b"\\n", b""):',
     '                    if t in (b"\\r\\n", b"\\n"):'),

    ("C47-arm-collision-keyed-on-the-endpoint-too", "measure_request_construction",
     "keying the arm collision on `(arm, endpoint)` rather than on the arm. Two captures of the SAME engine that hit different endpoints then merge into one row again, and one of them is back outside every gate's reach -- round-2 finding 3 for the third time, now through the fix for its second form.",
     '            first_seen = arm_source.setdefault(row["arm"], p)',
     '            first_seen = arm_source.setdefault((row["arm"], row["endpoint"]), p)'),

    ("C48-unread-refusal-loses-its-reason", "measure_request_construction",
     "deleting the reader's branch for a body the recorder could not READ. It still exits 2 -- on `has no raw_bytes`, which is true, useless, and about the wrong thing. That is why this survived every gate: nothing asserted WHICH refusal fires, and a refusal that cannot name the request sends an operator to re-capture instead of to the client.",
     '    if isinstance(body, dict) and UNREAD in body:',
     '    if False:'),

    ("C49-unencodable-body-raises-in-the-reader", "measure_request_construction",
     "measuring a body that cannot be encoded as UTF-8. Every byte count here is taken over a UTF-8 encoding, so a lone surrogate reached UnicodeEncodeError inside nbytes() and exited 1 -- against decompose()'s own documented promise, `Raises nothing; errors are fields`, and outside this instrument's documented exit set.",
     '            ser(body).encode("utf-8")',
     '            pass'),

    ("C50-stray-blank-chunk-line", "capture_proxy",
     'deleting the empty-chunk-size tolerance from the de-chunker. A client that sends its terminator twice then raises ValueError on the blank line, and a perfectly de-chunkable body becomes a refusal of the whole capture. It survived every gate until a self-test leg sent a stream with a stray blank line in it -- the same shape of gap as C44: real behaviour nothing exercised.',
     '            if not field:\n                continue',
     '            if False:\n                continue'),

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
    try:
        p = subprocess.run([sys.executable,
                            os.path.join(base, "instruments", module + ".py"),
                            "--self-test"],
                           capture_output=True, text=True, cwd=base, env=env,
                           timeout=600)
    except subprocess.TimeoutExpired:
        # A mutant that HANGS is red, not a crashed gate. One of these cases removes the
        # EOF guard from a read loop, and the honest outcome of that is no outcome.
        return 124, ["FAIL  the mutant's own self-test did not finish in 600 s"]
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
                # mkdtemp names the one thing in this output that cannot repeat.
                # Normalised so this gate's own artefact is regenerable; the
                # refusal text either side of it is what the case is about.
                shown = re.sub(r"/tmp/ari-[a-z-]+-[A-Za-z0-9_]+", "<tmp>",
                               fails[0])
                print("%-34s RED   -- %s" % (name, shown[:88]))
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
    # "turns this gate RED" rather than "turns a control RED": two cases kill the
    # recorder so thoroughly that its self-test cannot reach the leg that would name the
    # defect, and go red on a non-zero exit instead. Each such case says so on its own
    # line. A review pointed out that the summary was claiming a named control for all
    # of them, which is a small overclaim about the one thing this gate exists to say.
    print("PASSED -- all %d fixes are pinned: reverting any one of them turns this gate "
          "RED." % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
