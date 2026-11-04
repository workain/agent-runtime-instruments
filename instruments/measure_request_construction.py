#!/usr/bin/env python3
"""Where do the bytes a coding agent SENDS actually go -- and which of them get billed?

A different vantage point from `measure_token_breakdown.py`, which reads a finished
transcript and tells you what a session turned out to have spent. This reads what the
client **constructed and put on the wire**, captured by `capture_proxy.py` before any
provider saw it. The two answer different questions and neither substitutes for the other:

    measure_token_breakdown.py  ->  where did this session's tokens go, after the fact
    measure_request_construction.py  ->  what does this client PUT IN every request,
                                         and what is the fixed part of that

The fixed part is the interesting one, because it is paid on every single turn: the
system prompt plus the tool schemas. For three real clients measured here it ranged from
a few hundred to tens of thousands of bytes, which is a per-turn tax the engine chooses
and the user cannot see.

WHAT IT REPORTS, per request and per arm
    * `wire_bytes`          -- exact serialized length as it arrived. No tokeniser, no
                               estimate, no assumption. This is the only figure here that
                               is a direct observation.
    * a byte split over EVERY top-level body field, plus JSON framing, which must
      account for 100% of the body (see the invariant below).
    * `system` / `tools` / `conversation`, normalised across both protocols, because
      Anthropic puts the instructions in a top-level `system` and OpenAI puts them in a
      `role: system` message, and comparing the two needs them in the same column.
    * `provider_metadata_bytes` vs `model_input_bytes` -- see PROVIDER METADATA below.
    * the model each request NAMED, as its own group. Never one assumed model for the
      whole capture: a client is free to route different requests to different models,
      and pricing a capture by the model you configured rather than the model each
      request actually named is how a published cost comparison goes wrong.

THE 100% COVERAGE INVARIANT, which is the whole reason this file is not a one-liner
    A decomposition that knows `system`, `tools` and `messages` LOOKS complete and can
    silently omit half the payload. On the first DeepSeek Harness request measured here,
    two fields that are not part of the Anthropic API at all -- `dsh_session_log` and
    `dsh_plugin_packages` -- were **79.3% of the body** (6,522 of 8,228 bytes). A
    three-column decomposition would have reported the remaining 20.7% as the whole
    request and been wrong by a factor of five, with no symptom.

    So the field split is checked against the JSON structure itself: the sum of the
    per-field byte counts plus the STRUCTURALLY PREDICTED framing (braces, quoted keys,
    separators) must equal the whole serialized body EXACTLY. Drop one field from the
    split and the two stop matching, because the dropped field's bytes are no longer in
    either term. A mismatch exits 3 and refuses to report.

    And a top-level field this file has never seen exits 4 and NAMES it, rather than
    letting `.get()` return None and print a clean-looking zero.

PROVIDER METADATA IS NOT MODEL INPUT, and getting this backwards inverts the answer
    A field being present in a request body does NOT mean it is billed as model input.
    Two of the fields above are documented by the vendor as costing zero model-input
    tokens -- quoted verbatim in PROVIDER_METADATA below, read first-hand from the
    installed packages. In the study this was ported from, counting them as input made
    dsh look ~20% more expensive than Claude Code; excluding them made it ~44% cheaper.
    Same bytes, opposite conclusion, and the evidence was one `cat` away the whole time.

    So both quantities are reported side by side and neither is collapsed into the other:
    `wire_bytes` is what the duplication costs the network, `model_input_bytes` is what a
    per-token bill is computed from. The classification is VENDOR DOCUMENTATION, not a
    bill we have seen; the citation is in the source so a reader can disagree with it.

BYTES, AND TOKENS ONLY IF YOU SUPPLY A TOKENISER
    This repository has no dependencies, and a bundled tokeniser would be both a
    dependency and a guess about the provider's own. So the measured unit is bytes, which
    is exact. Pass `--tokeniser tiktoken:cl100k_base` and tokens are reported too, from
    whatever tokeniser you installed; without it the token columns print
    `NOT MEASURED -- no tokeniser given` rather than a plausible byte/4 estimate.

    The honest limit: **bytes are a proxy for tokens, and the proxy is not uniform.** A
    tool-schema array is dense in JSON punctuation and tokenises differently from prose,
    so a bytes ratio between two arms is not their token ratio. When a tokeniser IS
    given, bytes-per-token is printed per arm so a reader can see whether the proxy held.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    `requests: 0` and nothing else -- which is also the correct output for a capture file
    from a client that was never pointed at the proxy. Worse, there is a second blind
    state no zero reveals: a decomposition that silently drops a field prints a complete,
    plausible, internally consistent table that is simply too small.

    Five things separate those cases, and all five can be observed failing
    (`--self-test`, and `scripts/test_request_construction_controls.sh` reverts each one
    and requires a control to turn RED):

      1. A missing or empty capture, or one with no MEASURABLE request in it, exits 2 and
         prints NO-MEASUREMENT. A client's startup probes (`/api/show`, `/v1/models`) land
         in the capture too, so counting LINES would report a measurement from a run where
         the agent never sent a turn. Only a body carrying `tools` or `messages` counts.
         (The 0-BYTE case is exercised by `--self-test` rather than by a committed fixture,
         and the reason is worth knowing: a contentless file cannot be tracked here at all.
         This repository's scrubber refuses tracked binaries, and it detects them with
         `grep -I`, which reports a blank file as binary. The empty-capture refusal is
         therefore checked in a temp directory, and the committed artefacts cover the
         missing-file and probes-only cases instead.)
      2. The coverage invariant above: a dropped field exits 3.
      3. An unknown top-level field exits 4, naming it.
      4. `--reference REF` re-measures a stored reference capture and requires the fresh
         one to reproduce it FIELD BY FIELD to the digit, exiting 5 otherwise. Nothing
         built on top of a capture is worth believing until a fresh capture of the same
         arm reproduces an old one; this makes that a gate instead of a hope.
      5. `--check-claims PROSE` requires every headline figure just measured to be quoted
         in that prose file, exiting 6 if one drifted. The comparison is
         WHITESPACE-INSENSITIVE on purpose: in the study this came from, the equivalent
         gate failed because a claim had wrapped across a markdown line break, and a gate
         that cries wolf at reflowed prose is a gate somebody switches off.

    What it still cannot tell you: whether the client would have constructed the same
    request against a real provider. It would not always -- see CONFOUNDS below, which
    this instrument prints on every run rather than keeping in a doc nobody opens.
"""
import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Every top-level body field these clients are known to send. An ABSENT field is fine
# and reports 0; an UNEXPECTED one is a finding and exits 4, because a field silently
# dropped from the split makes the client look cheaper than it is.
KNOWN_FIELDS = {
    # Anthropic Messages
    "model", "messages", "system", "tools", "max_tokens", "stream", "temperature",
    "top_p", "top_k", "stop_sequences", "tool_choice", "metadata", "thinking",
    "anthropic_version", "service_tier", "container", "mcp_servers",
    # DeepSeek Harness additions, NOT part of the Anthropic API
    "dsh_session_log", "dsh_plugin_packages", "output_config",
    # OpenAI chat completions (the rail a custom provider puts Hermes on)
    "frequency_penalty", "presence_penalty", "n", "logprobs", "top_logprobs",
    "response_format", "seed", "functions", "function_call", "user", "stream_options",
    "parallel_tool_calls", "max_completion_tokens", "reasoning_effort", "store",
    "logit_bias", "modalities", "prediction", "verbosity",
    # the recorder's own marker for a body it could not parse -- kept KNOWN so an
    # unparseable request is reported as such instead of masquerading as a new field
    "__unparseable__",
}

# NOT BILLED AS MODEL INPUT -- per the vendor's own installed documentation, read
# first-hand from node_modules rather than from a summary of it.
#
#   @deepseek-ai/dsh-session-log-deepseek/README.md, "Model Experience":
#     "What the model sees: Nothing. `dsh_session_log` is a sibling of the DeepSeek
#      request's model-input fields and is not inserted into `messages`, the system
#      prompt, or tool schemas."
#     "Token effect: Zero model-input tokens; the field only increases HTTP request
#      bytes, bounded by `maxBytes`."
#     "KV Cache effect: None; the model-visible request prefix remains unchanged."
#   @deepseek-ai/dsh-plugin-package-inventory-deepseek/README.md, same section:
#     "What the model sees: Nothing. `dsh_plugin_packages` is provider metadata outside
#      the model's messages, system prompt, and tool schemas."
#     "Token effect: Zero model-input tokens; the complete inventory adds only HTTP
#      request bytes."
#
# These are the ONLY two packages in that tree making the claim: `grep -rl "Zero
# model-input tokens"` over the installed node_modules returns exactly these two READMEs.
# Checked on the box this was written on, which is why it is a citation and not a belief.
#
# `output_config` is deliberately NOT here. It is a request parameter (`{"effort":
# "high"}`, 18 bytes) analogous to Anthropic's own `thinking`, which the comparator
# figures also count. Leaving it counted is the choice that flatters dsh LEAST.
PROVIDER_METADATA = {"dsh_session_log", "dsh_plugin_packages"}

# Fields that are one engine's own invention, with no counterpart in the others. Reported
# separately from PROVIDER_METADATA because the two questions differ: "is this field
# comparable across engines" is not "is this field billed".
ENGINE_SPECIFIC = {"dsh_session_log", "dsh_plugin_packages", "output_config"}

# Printed on every run. Each one is a way this measurement can be true about the run and
# false about the engine, and each cost somebody a wrong published number once already.
CONFOUNDS = [
    "A capture needs the client pointed at a NON-FIRST-PARTY base URL, and for Claude "
    "Code that is not a neutral act: it DISABLES tool-search deferral, which the source "
    "study measured at +49.5% tokens. A proxied Claude Code arm is a different "
    "configuration from a first-party one, not the same one observed.",
    "Some clients embed LIVE GIT STATE in their own system prompt -- Hermes embeds cwd, "
    "branch, status and recent commit SUBJECTS. Such a capture is NOT reproducible "
    "across commits: committing changes the payload. Freeze one git state for a batch, "
    "or capture outside a repository, and say which you did.",
    "A field PRESENT in a payload is not necessarily billed as model input. Check the "
    "vendor's own documentation per field before counting it; see PROVIDER_METADATA in "
    "this file for the two that are documented at zero.",
    "The model never answers, so nothing that needs real model progress is measurable "
    "here: no compaction, no context growth across turns, no tool-use loop. This is the "
    "FIRST request, which is the fixed cost -- not a whole session.",
]

TOKENS_UNMEASURED = "NOT MEASURED -- no tokeniser given (bytes are exact and need none)"


# ---------------------------------------------------------------------------
def ser(obj):
    """The canonical serialization every byte count here is taken over.

    `ensure_ascii=False` because the wire carries UTF-8, and escaping non-ASCII would
    inflate every non-English payload by a factor this instrument would then report as a
    property of the client.
    """
    return json.dumps(obj, ensure_ascii=False)


def nbytes(obj):
    return len(ser(obj).encode("utf-8"))


def predicted_framing(body):
    """Bytes `json.dumps` spends on structure alone, derived from the structure.

    For the default separators: two braces, then per key its quoted form plus `": "`,
    then `", "` between pairs. Deriving this rather than defining framing as
    `total - sum(fields)` is the entire point: defined that way it would absorb a dropped
    field and the coverage check could never fail.
    """
    if not body:
        return len("{}".encode("utf-8"))
    per_key = sum(len(ser(k).encode("utf-8")) + len(": ".encode("utf-8")) for k in body)
    return 2 + per_key + (len(body) - 1) * len(", ".encode("utf-8"))


def load_tokeniser(spec):
    """-> (callable, label) or (None, why-not). Never raises; a missing tokeniser is a
    reported state, not a crash."""
    if not spec:
        return None, None
    if ":" not in spec:
        return None, ("--tokeniser wants <module>:<encoding>, e.g. "
                      "tiktoken:cl100k_base (got %r)" % spec)
    mod, enc = spec.split(":", 1)
    if mod != "tiktoken":
        return None, ("only 'tiktoken:<encoding>' is supported, because it is the only "
                      "tokeniser whose encodings are named in public documentation "
                      "(got %r)" % mod)
    try:
        import tiktoken
    except Exception as e:  # noqa: BLE001
        return None, ("tiktoken is not importable (%r). It is NOT a dependency of this "
                      "repository; install it yourself if you want token columns." % (e,))
    try:
        e = tiktoken.get_encoding(enc)
    except Exception as exc:  # noqa: BLE001
        return None, "tiktoken has no encoding %r (%r)" % (enc, exc)

    def count(obj):
        s = obj if isinstance(obj, str) else ser(obj)
        return len(e.encode(s, disallowed_special=()))
    return count, "tiktoken:" + enc


# ---------------------------------------------------------------------------
def is_probe(rec):
    """True for a startup probe rather than a turn.

    A client's model-list / capability probes land in the same capture as its real
    requests. Counting capture LINES would therefore report a measurement from a run in
    which the agent never constructed a turn at all -- which is precisely the
    no-measurement case this instrument exists to refuse.
    """
    body = rec.get("body") or {}
    return not (body.get("tools") or body.get("messages"))


def normalise_scopes(body):
    """-> (system_obj, tools_obj, conversation_list), one shape for both protocols."""
    tools = body.get("tools") or []
    msgs = body.get("messages") or []
    if body.get("system"):                      # Anthropic Messages
        return body["system"], tools, list(msgs)
    sysm, convo = [], []                        # OpenAI chat completions
    for m in msgs:
        tgt = sysm if isinstance(m, dict) and m.get("role") == "system" else convo
        tgt.append(m)
    return (sysm or None), tools, convo


def decompose(rec, tok=None):
    """One captured request -> its full accounting. Raises nothing; errors are fields."""
    body = rec.get("body") or {}
    fields = {k: nbytes(v) for k, v in body.items()}
    whole = nbytes(body)
    framing = predicted_framing(body)
    summed = sum(fields.values())
    meta = sum(v for k, v in fields.items() if k in PROVIDER_METADATA)
    system, tools, convo = normalise_scopes(body)
    row = {
        "arm": rec.get("arm"),
        "endpoint": rec.get("endpoint"),
        "model_named": body.get("model") or "<ABSENT>",
        "wire_bytes": rec.get("raw_bytes", 0),
        "body_bytes": whole,
        "summed_field_bytes": summed,
        "framing_bytes": framing,
        # The invariant. Equality is NOT definitional: framing is predicted from the
        # structure, so a field missing from `fields` breaks it.
        "coverage_ok": summed + framing == whole,
        "coverage_residual": whole - (summed + framing),
        "provider_metadata_bytes": meta,
        "model_input_bytes": whole - meta,
        "engine_specific_bytes": sum(v for k, v in fields.items() if k in ENGINE_SPECIFIC),
        "system_bytes": nbytes(system) if system else 0,
        "tools_bytes": nbytes(tools) if tools else 0,
        "conversation_bytes": nbytes(convo) if convo else 0,
        "n_tools": len(tools),
        "n_messages": len(convo),
        "fields": fields,
        "unknown_fields": sorted(set(body) - KNOWN_FIELDS),
    }
    row["fixed_bytes"] = row["system_bytes"] + row["tools_bytes"]
    if tok:
        row["body_tokens"] = tok(body)
        row["provider_metadata_tokens"] = sum(
            tok(v) for k, v in body.items() if k in PROVIDER_METADATA)
        row["model_input_tokens"] = row["body_tokens"] - row["provider_metadata_tokens"]
        row["system_tokens"] = tok(system) if system else 0
        row["tools_tokens"] = tok(tools) if tools else 0
        row["conversation_tokens"] = tok(convo) if convo else 0
        row["fixed_tokens"] = row["system_tokens"] + row["tools_tokens"]
    return row


def read_capture(path):
    """-> (records, error-string). A fault is returned, never printed here."""
    if not os.path.exists(path):
        return None, "capture file does not exist: %s" % path
    recs = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception as e:  # noqa: BLE001
                return None, ("capture line %d of %s is not JSON (%r). A partially "
                              "readable capture is refused rather than measured: the "
                              "missing lines would silently shrink every total."
                              % (n, path, e))
    if not recs:
        return None, "capture file is empty (0 requests): %s" % path
    return recs, None


def measure(paths, tok=None):
    """-> (report, exit_code). Every refusal carries its reason in the report."""
    rep = {"captures": list(paths), "requests": 0, "probes": 0, "measurable": 0,
           "rows": [], "errors": [], "unknown_fields": [], "coverage_broken": []}
    for p in paths:
        recs, err = read_capture(p)
        if err:
            rep["errors"].append(err)
            continue
        rep["requests"] += len(recs)
        for r in recs:
            if is_probe(r):
                rep["probes"] += 1
                continue
            row = decompose(r, tok)
            row["capture"] = p
            rep["rows"].append(row)
            rep["measurable"] += 1
            for f in row["unknown_fields"]:
                if f not in rep["unknown_fields"]:
                    rep["unknown_fields"].append(f)
            if not row["coverage_ok"]:
                rep["coverage_broken"].append(
                    "%s request %d: fields(%d) + framing(%d) != body(%d), residual %d"
                    % (p, len(rep["rows"]) - 1, row["summed_field_bytes"],
                       row["framing_bytes"], row["body_bytes"], row["coverage_residual"]))
    if rep["errors"] and not rep["rows"]:
        return rep, 2
    if not rep["measurable"]:
        rep["errors"].append(
            "%d request(s) captured but NONE is measurable: a measurable request carries "
            "`tools` or `messages`. %d were startup probes. This is NO MEASUREMENT, not a "
            "zero result." % (rep["requests"], rep["probes"]))
        return rep, 2
    if rep["unknown_fields"]:
        return rep, 4
    if rep["coverage_broken"]:
        return rep, 3
    # per-model groups, because one capture may name more than one model
    by_model = {}
    for row in rep["rows"]:
        g = by_model.setdefault(row["model_named"], {
            "requests": 0, "wire_bytes": 0, "body_bytes": 0, "model_input_bytes": 0,
            "provider_metadata_bytes": 0, "fixed_bytes": 0, "conversation_bytes": 0})
        g["requests"] += 1
        for k in list(g):
            if k != "requests":
                g[k] += row[k]
        if tok:
            g["model_input_tokens"] = g.get("model_input_tokens", 0) + \
                row["model_input_tokens"]
            g["body_tokens"] = g.get("body_tokens", 0) + row["body_tokens"]
    rep["by_model"] = by_model
    rep["tokeniser"] = None
    return rep, 0


# ---------------------------------------------------------------------------
# Faithfulness: a fresh capture must reproduce a stored one, field by field, to the digit.
FAITHFULNESS_FIELDS = ("system_bytes", "tools_bytes", "conversation_bytes",
                       "provider_metadata_bytes", "model_input_bytes", "body_bytes",
                       "n_tools", "n_messages")


def faithfulness(fresh_rows, ref_path):
    """-> (lines, ok). Compares the FIRST measurable request of each.

    Why the first and not an aggregate: the first request is the one with no conversation
    history in it, so it is the only one two runs can be expected to match exactly. A
    later request depends on what the stub happened to reply and how the client reacted.
    """
    recs, err = read_capture(ref_path)
    if err:
        return ["FAITHFULNESS: CANNOT CHECK -- " + err], False
    ref = [r for r in recs if not is_probe(r)]
    if not ref:
        return ["FAITHFULNESS: CANNOT CHECK -- reference capture has no measurable "
                "request in it"], False
    a, b = fresh_rows[0], decompose(ref[0])
    lines = ["FAITHFULNESS of this capture against %s" % ref_path,
             "  (first measurable request of each; a fresh capture that cannot reproduce",
             "   a stored one to the digit makes everything built on it unbelievable)",
             "  %-26s %12s %12s" % ("field", "this", "reference")]
    ok = True
    for f in FAITHFULNESS_FIELDS:
        x, y = a[f], b[f]
        mark = "MATCH" if x == y else "%+d  <-- DIFFERS" % (x - y)
        if x != y:
            ok = False
        lines.append("  %-26s %12d %12d  %s" % (f, x, y, mark))
    lines.append("  -> %s" % ("every field reproduces to the digit" if ok else
                              "REPRODUCTION FAILED: see CONFOUNDS above before "
                              "concluding the engine changed"))
    return lines, ok


# ---------------------------------------------------------------------------
def flatten_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def check_claims(prose_path, claims):
    """Every measured headline figure must be quoted in the prose. -> (lines, ok).

    WHITESPACE-INSENSITIVE, deliberately. The gate this is ported from failed once purely
    because a claim had wrapped across a markdown line break -- the number was right, the
    prose was right, and the gate said drift. A gate that fires on reflowed text is a gate
    that gets switched off, and then it is not protecting the numbers either.
    """
    if not os.path.exists(prose_path):
        return ["CLAIMS: CANNOT CHECK -- no prose file at %s" % prose_path], False
    text = open(prose_path, encoding="utf-8").read()
    flat = flatten_ws(text)
    lines = ["PUBLISHED-CLAIM CHECK against %s" % prose_path,
             "  (whitespace-insensitive: a claim that wrapped across a line break is",
             "   still the same claim, and failing on reflow is how a gate loses trust)"]
    ok = True
    for label, claim in claims:
        hit = claim in text or flatten_ws(claim) in flat
        if not hit:
            ok = False
        lines.append("  %-34s %s  %s" % (label, "FOUND " if hit else "DRIFTED",
                                         claim))
    lines.append("  -> %s" % ("every figure measured here is quoted in the prose" if ok
                              else "DRIFT: a figure above is not in the prose"))
    return lines, ok


def headline_claims(rep):
    """The figures a reader would quote. Kept small on purpose: a claim list nobody can
    read is a claim list nobody maintains."""
    r = rep["rows"][0]
    out = [("first-request wire bytes", "%d bytes on the wire" % r["wire_bytes"]),
           ("first-request fixed overhead", "fixed %d bytes" % r["fixed_bytes"]),
           ("first-request model input", "model input %d bytes" % r["model_input_bytes"])]
    if r["provider_metadata_bytes"]:
        pct = 100.0 * r["provider_metadata_bytes"] / r["body_bytes"]
        out.append(("provider metadata share",
                    "%d bytes (%.1f%%) of provider metadata"
                    % (r["provider_metadata_bytes"], pct)))
    return out


# ---------------------------------------------------------------------------
def report(rep, code, tokeniser_label, tokeniser_why, extra_lines):
    out = []
    out.append("CONFOUNDS -- printed every run, because each of these makes this number "
               "true about the RUN and false about the ENGINE:")
    for c in CONFOUNDS:
        out.append("  * " + c)
    out.append("")
    if tokeniser_label:
        out.append("tokeniser: %s" % tokeniser_label)
    else:
        out.append("tokens:    %s" % TOKENS_UNMEASURED)
        if tokeniser_why:
            out.append("           (asked for one, and could not: %s)" % tokeniser_why)
    out.append("")
    for e in rep["errors"]:
        out.append("NO-MEASUREMENT: " + e)
    if rep["unknown_fields"]:
        out.append("UNKNOWN BODY FIELD(S): %s" % ", ".join(rep["unknown_fields"]))
        out.append("  A FINDING, not a crash. An unaccounted field means the "
                   "decomposition UNDERSTATES the payload, which reports the client as")
        out.append("  cheaper than it is. Add it to KNOWN_FIELDS only after deciding "
                   "whether it is provider metadata (see PROVIDER_METADATA).")
    for c in rep["coverage_broken"]:
        out.append("COVERAGE BROKEN: " + c)
    if rep["coverage_broken"]:
        out.append("  The per-field split does not account for the whole body, so no "
                   "figure below would be trustworthy. Refusing.")
    if code == 0:
        out.append("requests: %d captured, %d measurable, %d startup probes "
                   "(a probe is not a turn)"
                   % (rep["requests"], rep["measurable"], rep["probes"]))
        out.append("100%% field coverage verified on all %d measurable requests"
                   % rep["measurable"])
        out.append("")
        # One row per ARM, taken from that arm's FIRST measurable request: the first is
        # the only one with no conversation history in it, so it is the fixed per-turn
        # cost rather than a figure that grows with whatever the stub happened to reply.
        first_by_arm = {}
        for r in rep["rows"]:
            first_by_arm.setdefault(r["arm"], r)
        hdr = ("%-22s %10s %10s %9s %9s %9s %7s %6s" %
               ("arm / first request", "wire B", "body B", "fixed B", "convo B",
                "meta B", "tools", "msgs"))
        out.append(hdr)
        out.append("-" * len(hdr))
        for arm, r in first_by_arm.items():
            out.append("%-22s %10d %10d %9d %9d %9d %7d %6d"
                       % (str(arm)[:22], r["wire_bytes"], r["body_bytes"],
                          r["fixed_bytes"], r["conversation_bytes"],
                          r["provider_metadata_bytes"], r["n_tools"], r["n_messages"]))
        out.append("  fixed B = system + tools, the part paid on EVERY turn; convo B = "
                   "everything else in `messages`.")
        out.append("")
        for arm, r in first_by_arm.items():
            out.append("FIRST REQUEST -- arm %r, endpoint %s, model named %r"
                       % (arm, r["endpoint"], r["model_named"]))
            for k, v in sorted(r["fields"].items(), key=lambda kv: -kv[1]):
                mark = ""
                if k in PROVIDER_METADATA:
                    mark = "  <-- provider metadata: ZERO model-input tokens per vendor"
                elif k in ENGINE_SPECIFIC:
                    mark = "  <-- engine-specific, no counterpart in the comparators"
                out.append("    %-22s %10d B%s" % (k, v, mark))
            out.append("    %-22s %10d B  (braces, quoted keys, separators)"
                       % ("(json framing)", r["framing_bytes"]))
            out.append("    %-22s %10d B  = the whole body, with nothing unaccounted"
                       % ("TOTAL", r["body_bytes"]))
            ratio = (r["wire_bytes"] / r["body_bytes"]) if r["body_bytes"] else 0.0
            out.append("    wire %d B vs normalised body %d B (ratio %.3f). The split "
                       "above is of the NORMALISED serialization:"
                       % (r["wire_bytes"], r["body_bytes"], ratio))
            out.append("      a per-field share of the raw wire bytes is not well "
                       "defined without re-parsing, and the two differ by the")
            out.append("      client's own separator and escaping choices. Quote `wire "
                       "B` for what the network paid.")
            if r["provider_metadata_bytes"]:
                out.append("    provider metadata %d B (%.1f%% of the body): HTTP bytes, "
                           "and ZERO model-input tokens per the vendor"
                           % (r["provider_metadata_bytes"],
                              100.0 * r["provider_metadata_bytes"] / r["body_bytes"]))
            else:
                out.append("    provider metadata 0 B: this client sends none, so model "
                           "input and whole body are the SAME number here")
            out.append("    MODEL INPUT %d B -- the quantity a per-token bill is computed "
                       "from" % r["model_input_bytes"])
            if "body_tokens" in r:
                dens = (r["body_bytes"] / r["body_tokens"]) if r["body_tokens"] else 0.0
                out.append("    tokens: body %d, model input %d, provider metadata %d "
                           "(%.2f bytes/token -- quote this before treating bytes as a "
                           "token proxy)"
                           % (r["body_tokens"], r["model_input_tokens"],
                              r["provider_metadata_tokens"], dens))
            else:
                out.append("    tokens: %s" % TOKENS_UNMEASURED)
            out.append("")
        out.append("PER MODEL NAMED IN THE REQUEST ITSELF -- never one assumed model for "
                   "the whole capture:")
        for m, g in sorted(rep["by_model"].items()):
            out.append("  %-26s %3d request(s)  model input %9d B  provider metadata "
                       "%8d B" % (m, g["requests"], g["model_input_bytes"],
                                  g["provider_metadata_bytes"]))
        if "<ABSENT>" in rep["by_model"]:
            out.append("  NOTE: <ABSENT> means the request named no model at all. It is "
                       "printed rather than defaulted, because a default here would")
            out.append("  price a request against a model nobody chose.")
    out.extend(extra_lines)
    return "\n".join(out)


# ---------------------------------------------------------------------------
def _fixture(tmp, name, bodies, arm="fx"):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as fh:
        for b in bodies:
            fh.write(json.dumps({"arm": arm, "endpoint": "/v1/messages",
                                 "ts": 0, "raw_bytes": len(ser(b).encode()),
                                 "header_names": ["content-type"], "body": b}) + "\n")
    return p


def self_test():
    """Each case is a known-answer input this instrument must get right or refuse.

    Every one of these corresponds to a way the figures could be wrong while looking
    fine, which is why they are controls rather than unit tests.
    """
    import tempfile
    passed, failed = [], []

    def ok(label):
        passed.append(label)
        print("  PASS  " + label)

    def no(label):
        failed.append(label)
        print("  FAIL  " + label)

    tmp = tempfile.mkdtemp(prefix="ari-mrc-selftest-")
    plain = {"model": "m", "system": "SYS", "tools": [{"name": "t"}],
             "messages": [{"role": "user", "content": "hello"}], "stream": True}

    print("== 1. nothing to measure must NOT render as a plausible zero ==")
    rep, code = measure([os.path.join(tmp, "nope.jsonl")])
    if code == 2 and any("does not exist" in e for e in rep["errors"]):
        ok("a missing capture -> exit 2 and NO-MEASUREMENT")
    else:
        no("a missing capture -> exit %d" % code)
    empty = os.path.join(tmp, "empty.jsonl")
    open(empty, "w").close()
    rep, code = measure([empty])
    if code == 2 and any("empty" in e for e in rep["errors"]):
        ok("an EMPTY capture -> exit 2 and NO-MEASUREMENT")
    else:
        no("an empty capture -> exit %d" % code)

    print("== 2. a capture of nothing but startup probes is NO MEASUREMENT ==")
    probes = _fixture(tmp, "probes.jsonl", [{"name": "a-model"}, {"name": "a-model"}])
    rep, code = measure([probes])
    if code == 2 and rep["probes"] == 2 and any("NONE is measurable" in e
                                                for e in rep["errors"]):
        ok("2 probes, 0 turns -> exit 2, and the probe COUNT is reported "
           "(counting lines would have called this a measurement)")
    else:
        no("probe-only capture -> exit %d, probes=%d" % (code, rep["probes"]))

    print("== 3. the measurement discriminates: a known delta is reported as that delta ==")
    small = _fixture(tmp, "small.jsonl", [dict(plain, messages=[
        {"role": "user", "content": "x" * 100}])])
    big = _fixture(tmp, "big.jsonl", [dict(plain, messages=[
        {"role": "user", "content": "x" * 600}])])
    rs, _ = measure([small])
    rb, _ = measure([big])
    delta = rb["rows"][0]["body_bytes"] - rs["rows"][0]["body_bytes"]
    if delta == 500:
        ok("+500 content bytes measured as exactly +500 (%d -> %d)"
           % (rs["rows"][0]["body_bytes"], rb["rows"][0]["body_bytes"]))
    else:
        no("expected +500, measured %+d" % delta)

    print("== 4. 100% field coverage, and it must BITE when a field is dropped ==")
    rep, code = measure([small])
    r = rep["rows"][0]
    if code == 0 and r["coverage_ok"] and r["summed_field_bytes"] + r["framing_bytes"] \
            == r["body_bytes"]:
        ok("fields + predicted framing == whole body, exactly")
    else:
        no("coverage invariant did not hold on a clean fixture")
    # The split must NAME every top-level field in the body. This is the leg that bites
    # when the decomposition is narrowed to the familiar system/tools/messages triple --
    # including when framing is ALSO redefined as the remainder, which on its own is
    # arithmetically identical and makes the equality above tautological. Neither revert
    # survives this one.
    body0 = dict(plain)
    if set(r["fields"]) == set(body0):
        ok("the split NAMES every top-level field (%d of %d) -- a narrowed "
           "system/tools/messages decomposition fails here even if its own arithmetic "
           "closes" % (len(r["fields"]), len(body0)))
    else:
        no("the split omits %r" % sorted(set(body0) - set(r["fields"])))

    # The control for the control. Framing must be predicted from the STRUCTURE alone --
    # the keys and how many there are -- and must not depend on the field VALUES at all.
    # Defined the lazy way, as `whole - sum(fields)`, it would absorb any field dropped
    # from the split and the invariant above could never fail. So: replacing every value
    # with a different one must not move the framing figure.
    body = dict(plain)
    keys_only = {k: 0 for k in body}
    if predicted_framing(body) == predicted_framing(keys_only):
        ok("framing is a function of the KEYS alone (%d B either way) -- it cannot "
           "absorb a dropped field, which is what makes the invariant falsifiable"
           % predicted_framing(body))
    else:
        no("framing depends on the field VALUES, so it would absorb a dropped field "
           "and the coverage invariant is definitional")
    # ...and the invariant really does break when a field goes missing from the split.
    starved = {k: nbytes(v) for k, v in body.items() if k != "tools"}
    if sum(starved.values()) + predicted_framing(body) != nbytes(body):
        ok("dropping `tools` from the split BREAKS the invariant (%d + %d != %d)"
           % (sum(starved.values()), predicted_framing(body), nbytes(body)))
    else:
        no("the invariant survives a dropped field: it is checking nothing")

    print("== 5. an UNKNOWN top-level field fails LOUDLY and names itself ==")
    unk = _fixture(tmp, "unknown.jsonl",
                   [dict(plain, totally_new_vendor_field="z" * 4000)])
    rep, code = measure([unk])
    if code == 4 and "totally_new_vendor_field" in rep["unknown_fields"]:
        ok("unknown field -> exit 4 and the field is named")
    else:
        no("unknown field -> exit %d, unknown=%r" % (code, rep["unknown_fields"]))

    print("== 6. provider metadata is EXCLUDED from model input, and the split bites ==")
    withmeta = _fixture(tmp, "meta.jsonl", [dict(plain, dsh_session_log={"e": "q" * 500},
                                                 dsh_plugin_packages=["p"] * 50)])
    rep, code = measure([withmeta])
    r = rep["rows"][0]
    meta = r["fields"]["dsh_session_log"] + r["fields"]["dsh_plugin_packages"]
    if code == 0 and r["provider_metadata_bytes"] == meta > 0 \
            and r["model_input_bytes"] == r["body_bytes"] - meta:
        ok("model input == body - provider metadata, and the metadata is non-zero "
           "(%d B) so the fixture can tell the two apart" % meta)
    else:
        no("provider-metadata split wrong: meta=%d reported=%d"
           % (meta, r["provider_metadata_bytes"]))
    # and the same fields must NOT be excluded when they are absent
    rep2, _ = measure([small])
    if rep2["rows"][0]["provider_metadata_bytes"] == 0 and \
            rep2["rows"][0]["model_input_bytes"] == rep2["rows"][0]["body_bytes"]:
        ok("with no metadata present, model input == the whole body (no phantom "
           "deduction)")
    else:
        no("a capture with no provider metadata still deducted something")

    print("== 7. both protocols normalise into the same columns ==")
    anth = _fixture(tmp, "anth.jsonl", [{"model": "m", "system": "SYSTEM-TEXT",
                                         "tools": [{"name": "t"}],
                                         "messages": [{"role": "user", "content": "hi"}]}],
                    arm="anthropic")
    oai = _fixture(tmp, "oai.jsonl", [{"model": "m", "tools": [{"name": "t"}],
                                       "messages": [
                                           {"role": "system", "content": "SYSTEM-TEXT"},
                                           {"role": "user", "content": "hi"}]}],
                   arm="openai")
    ra, _ = measure([anth])
    ro, _ = measure([oai])
    if ra["rows"][0]["system_bytes"] > 0 and ro["rows"][0]["system_bytes"] > 0 \
            and ra["rows"][0]["n_messages"] == ro["rows"][0]["n_messages"] == 1:
        ok("an Anthropic top-level `system` and an OpenAI role=system message both land "
           "in the system column, and neither is counted as conversation")
    else:
        no("protocol normalisation failed: anth system=%d oai system=%d, msgs %d/%d"
           % (ra["rows"][0]["system_bytes"], ro["rows"][0]["system_bytes"],
              ra["rows"][0]["n_messages"], ro["rows"][0]["n_messages"]))

    print("== 8. the model is read per request, not assumed for the capture ==")
    two = _fixture(tmp, "twomodels.jsonl", [dict(plain, model="model-a"),
                                            dict(plain, model="model-b"),
                                            {k: v for k, v in plain.items()
                                             if k != "model"}])
    rep, code = measure([two])
    if code == 0 and set(rep["by_model"]) == {"model-a", "model-b", "<ABSENT>"}:
        ok("three requests naming two models and no model -> three groups, including "
           "<ABSENT> rather than a default")
    else:
        no("per-model grouping wrong: %r" % sorted(rep.get("by_model", {})))

    print("== 9. faithfulness: reproducing passes, substituting another arm FAILS ==")
    rep_s, _ = measure([small])
    lines, good = faithfulness(rep_s["rows"], small)
    if good:
        ok("a capture checked against ITSELF reproduces every field")
    else:
        no("a capture does not reproduce itself: " + " | ".join(lines))
    lines, good = faithfulness(rep_s["rows"], big)
    if not good and any("DIFFERS" in l for l in lines):
        ok("checked against a DIFFERENT arm's capture it fails and names the field "
           "(so the check can fail, which is the only reason to trust it passing)")
    else:
        no("faithfulness passed against a different capture")
    lines, good = faithfulness(rep_s["rows"], probes)
    if not good and any("no measurable request" in l for l in lines):
        ok("a reference with no measurable request is CANNOT CHECK, not a pass")
    else:
        no("a probe-only reference was treated as checkable")

    print("== 10. claim drift, and it must NOT fire on reflowed prose ==")
    claims = headline_claims(rep_s)
    prose_ok = os.path.join(tmp, "ok.md")
    one = claims[0][1]
    # the same claim, deliberately wrapped across a line break at a space
    wrapped = one.replace(" ", "\n", 1) if " " in one else one
    with open(prose_ok, "w", encoding="utf-8") as fh:
        fh.write("Some prose. " + wrapped + " and more prose.\n")
        for _, c in claims[1:]:
            fh.write("Also " + c + ".\n")
    lines, good = check_claims(prose_ok, claims)
    if good:
        ok("every claim found even though one WRAPPED across a line break -- the gate is "
           "whitespace-insensitive, which is why it does not cry wolf at reflow")
    else:
        no("the gate fired on reflowed prose: " + " | ".join(lines))
    prose_bad = os.path.join(tmp, "bad.md")
    with open(prose_bad, "w", encoding="utf-8") as fh:
        fh.write(("Some prose. " + one + "\n").replace(
            str(rep_s["rows"][0]["wire_bytes"]), "999999"))
    lines, good = check_claims(prose_bad, claims)
    if not good and any("DRIFTED" in l for l in lines):
        ok("a changed digit IS caught -- so the whitespace tolerance did not cost the "
           "gate its teeth")
    else:
        no("a changed digit was not caught")
    lines, good = check_claims(os.path.join(tmp, "absent.md"), claims)
    if not good and any("CANNOT CHECK" in l for l in lines):
        ok("a missing prose file is CANNOT CHECK, not a pass")
    else:
        no("a missing prose file passed")

    print("== 11. a corrupt capture line is refused, not partially measured ==")
    corrupt = os.path.join(tmp, "corrupt.jsonl")
    with open(corrupt, "w", encoding="utf-8") as fh:
        fh.write(open(small, encoding="utf-8").read())
        fh.write("{not json at all\n")
    rep, code = measure([corrupt])
    if code == 2 and any("not JSON" in e for e in rep["errors"]):
        ok("one unreadable line refuses the whole capture (a partial read would "
           "silently shrink every total)")
    else:
        no("a corrupt capture was measured anyway: exit %d" % code)

    print("== 12. an unavailable tokeniser is a reported state, never a crash ==")
    fn, why = load_tokeniser("nonsense")
    if fn is None and why and "wants" in why:
        ok("a malformed --tokeniser spec is refused with a reason")
    else:
        no("malformed --tokeniser spec: %r / %r" % (fn, why))
    fn, why = load_tokeniser("notatokeniser:x")
    if fn is None and why:
        ok("an unsupported tokeniser module is refused with a reason")
    else:
        no("unsupported tokeniser accepted")

    print()
    print("self-test: %d pass, %d fail" % (len(passed), len(failed)))
    if failed:
        print("SELF-TEST FAILED -- do NOT believe any figure this instrument prints.")
        return 1
    return 0


# ---------------------------------------------------------------------------
def main(argv):
    if "--help-blind" in argv:
        print(__doc__)
        return 0
    ap = argparse.ArgumentParser(
        description="Decompose what an agent client CONSTRUCTS and sends, from a "
                    "capture_proxy.py capture file.",
        epilog="Exit 0 measured - 2 cannot measure - 3 coverage broken - 4 unknown "
               "field - 5 faithfulness mismatch - 6 published-claim drift.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="*", help="JSONL capture files")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--tokeniser", metavar="MOD:ENC",
                    help="e.g. tiktoken:cl100k_base. NOT a dependency of this repo; "
                         "without it the token columns say NOT MEASURED")
    ap.add_argument("--reference", metavar="CAPTURE",
                    help="require this capture's first measurable request to reproduce "
                         "the reference's, field by field, to the digit")
    ap.add_argument("--check-claims", metavar="PROSE",
                    help="require every headline figure measured here to be quoted in "
                         "this prose file (whitespace-insensitively)")
    ap.add_argument("--self-test", action="store_true",
                    help="run the known-answer controls and exit non-zero if any fails")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this would say if it measured nothing")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()
    if not a.captures:
        print("REFUSING: pass at least one capture file, or --self-test. Produce one "
              "with:\n  ARI_CAPTURE=/tmp/cap.jsonl ARI_ARM=myarm python3 "
              "instruments/capture_proxy.py 4199", file=sys.stderr)
        return 2

    tok, tok_why = load_tokeniser(a.tokeniser)
    tok_label = ("tiktoken:" + a.tokeniser.split(":", 1)[1]) if tok else None
    if tok:
        tok_why = None
    rep, code = measure(a.captures, tok)
    rep["tokeniser"] = tok_label

    extra = []
    if code == 0 and a.reference:
        lines, good = faithfulness(rep["rows"], a.reference)
        extra += [""] + lines
        rep["faithfulness_ok"] = good
        if not good:
            code = 5
    if code in (0, 5) and a.check_claims:
        claims = headline_claims(rep)
        lines, good = check_claims(a.check_claims, claims)
        extra += [""] + lines
        rep["claims_ok"] = good
        if not good and code == 0:
            code = 6

    if a.json:
        rep["exit_code"] = code
        rep["confounds"] = CONFOUNDS
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        print(report(rep, code, tok_label, tok_why, extra))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
