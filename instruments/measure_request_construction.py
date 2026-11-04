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
    `dsh_plugin_packages` -- were `provider metadata 6519 B (75.1% of the body)` of a
    `normalised body 8680 B`. A three-column decomposition would have seen only `fixed 1665
    bytes` and the conversation, reported that as the whole request, and been wrong by
    roughly a factor of four with no symptom at all.

    THOSE ARE THIS REPOSITORY'S OWN FIGURES, from samples/measure_request_construction.
    dsh.out. An earlier draft of this paragraph quoted the SOURCE study's instead --
    79.3%, 6,522 of 8,228 bytes -- which are correct about that study, three bytes away
    from the capture committed here, and present in no artefact in this repository. A
    reviewer found them by grepping for them. `--help-blind` is a published surface, so
    scripts/check_figures.sh now reads every instrument's blind answer as prose and
    requires its figures to exist in a sample, exactly like README's.

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

# The key capture_proxy.py stores a body it could not parse under. Named here, in the
# module that must refuse it, rather than matched as a literal in two files that could
# drift apart.
UNPARSEABLE = "__unparseable__"
UNREAD = "__unread__"


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


def coverage_holds(summed, framing, whole):
    """The 100% coverage invariant, as one named predicate.

    It is a function rather than an inline `==` because `==` is one character from `<=`,
    and `<=` passes every deficit -- which is the ONLY direction the failure this
    instrument exists to catch goes in. A dropped field makes `summed + framing` SMALLER
    than the body, never larger. A review mutated the comparison to `<=` and the whole
    preflight stayed green; the self-test now drives this predicate directly, with a
    deficit and a surplus, so neither inequality survives.
    """
    return summed + framing == whole


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

    return make_counter(e, "tiktoken:" + enc)


def make_counter(enc_obj, label):
    """-> (callable, label) or (None, why-not). Wraps an encoder and GATES it.

    Separate from load_tokeniser so the gate below is reachable with no tiktoken
    installed: the self-test hands this a fake encoder returning one token per four
    bytes -- the exact substitution a review made -- and must get the refusal. Calling
    looks_like_a_fixed_divisor from a test directly would check the predicate while leaving
    its only call site uncovered, which is the half worth protecting.
    """
    def count(obj):
        s = obj if isinstance(obj, str) else ser(obj)
        return len(enc_obj.encode(s, disallowed_special=()))
    bad = looks_like_a_fixed_divisor(count)
    if bad:
        return None, bad
    return count, label


# The probe strings. Chosen so a real BPE's bytes-per-token RATIO differs sharply
# between them: English prose tokenises coarsely (measured 4.93 B/token on cl100k_base),
# JSON punctuation finely (1.51), CJK finely per byte (1.88).
_TOK_PROBES = ("the quick brown fox jumps over the lazy dog, repeatedly and at length",
               '{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6,"g":7,"h":8,"i":9,"j":10}',
               "\u6771\u4eac\u90fd\u5343\u4ee3\u7530\u533a\u6c38\u7530\u753a")


# A real BPE's spread across the probes above, measured on cl100k_base: 3.26. Every
# fixed-divisor estimator tried came in at or below 1.12 -- len//4 1.06, round(len/4)
# 1.08, ceil 1.03, len//4+1 1.03, len//5 1.06, int(len/3.8) 1.12. The threshold sits in
# the middle of that gap (the midpoint of 1.12 and 3.26 is 2.19), so a real tokeniser
# would have to be 2.17x less varied than cl100k_base before it tripped. An earlier
# version of this comment said "three times", which is not what 3.26 / 1.5 is; a review
# measured it. All seven encodings tiktoken 0.14.0 ships -- gpt2, r50k_base, p50k_base,
# p50k_edit, cl100k_base, o200k_base, o200k_harmony -- measure 3.259 on these probes.
_TOK_MIN_SPREAD = 1.5


def looks_like_a_fixed_divisor(count):
    """-> a refusal reason if `count` is an arithmetic byte estimate, not a tokeniser.

    Why this is a gate and not paranoia: the whole promise of the token column is that it
    is NOT bytes/N, and a review demonstrated that replacing the encoder with exactly
    that left every gate in this repository green -- the published bytes/token figures
    come from a path with no executed coverage here, because tiktoken is deliberately not
    a dependency and is not installed. This runs on the real encoder at load time, so the
    one path no fixture can cover is at least checked on the way past.

    THIS TESTS THE CLASS, NOT ONE SPELLING, and it did not always. The first version
    compared against the literal `len(bytes)//4`; a second review wrote `round(len/4)`,
    `(len+3)//4`, `len//4+1`, `len//5` and `int(len/3.8)` and every one of them was
    accepted and labelled `tiktoken:cl100k_base`, while docs/limits.md claimed the guard
    "rejects division". What distinguishes any fixed divisor from a tokeniser is not its
    constant: it is that the bytes-per-token RATIO comes out the same for every input,
    whereas a BPE's ratio depends entirely on what the text is.

    Deliberately NOT a tolerance check against expected counts: those are tiktoken's to
    change between versions, and a gate that pins another project's numbers breaks on its
    upgrades. All this asserts is that the thing varies.
    """
    ratios = []
    for p in _TOK_PROBES:
        n = count(p)
        if n <= 0:
            return ("the tokeniser returned %r tokens for a %d-byte probe string. A "
                    "count of zero or less is not a count"
                    % (n, len(p.encode("utf-8"))))
        ratios.append(len(p.encode("utf-8")) / float(n))
    spread = max(ratios) / min(ratios)
    if spread < _TOK_MIN_SPREAD:
        return ("the tokeniser's bytes-per-token ratio barely varies across %d very "
                "different probe strings (spread %.3f, under %.1f): English prose, JSON "
                "punctuation and CJK all came out alike, which is what arithmetic on the "
                "byte count does and not what tokenisation does. Refusing to label a "
                "byte estimate `tiktoken:<enc>`; a real cl100k_base measures 3.26 here."
                % (len(_TOK_PROBES), spread, _TOK_MIN_SPREAD))
    return None


# ---------------------------------------------------------------------------
def is_probe(rec):
    """True for a startup probe rather than a turn.

    A client's model-list / capability probes land in the same capture as its real
    requests. Counting capture LINES would therefore report a measurement from a run in
    which the agent never constructed a turn at all -- which is precisely the
    no-measurement case this instrument exists to refuse.
    """
    body = rec.get("body") or {}
    if UNPARSEABLE in body:
        # NOT a probe, and this distinction is the whole finding. capture_proxy stores a
        # body it could not parse under this key rather than dropping the record, with
        # the stated reason that "a silently skipped request would show up as a smaller
        # payload instead" -- but such a record carries neither `tools` nor `messages`,
        # so the plain test below called it a startup probe. A review fed three 48 KB
        # records in and got "3 startup probes" with a 100%-coverage banner and exit 0:
        # 144 KB of real traffic not omitted but MISLABELLED, which is worse, because
        # the count looked accounted for. measure() refuses the capture instead.
        return False
    return not (body.get("tools") or body.get("messages"))


# Roles that carry the INSTRUCTIONS rather than the conversation, on the OpenAI side.
# `developer` is not a variant spelling: it is what OpenAI renamed `system` to, and both
# are live on the endpoint. Recognising only `system` put a 12 KB instruction block in the
# conversation column and reported `fixed B = 49` -- found by a review, which is exactly
# the defect this instrument's own control C6 pins for the OLDER spelling. The fixed
# per-turn part is the headline figure here, so the column it lands in is the whole answer.
INSTRUCTION_ROLES = ("system", "developer")


def normalise_scopes(body):
    """-> (system_parts, tools_obj, conversation_list), one shape for both protocols.

    `system_parts` is a LIST of objects whose byte counts are summed, not one object.
    That is what lets the two protocols be handled by the same code without moving any
    published figure: a plain Anthropic request yields `[body["system"]]` and a plain
    OpenAI one yields `[[instruction messages]]`, each serialising to exactly what the
    single-object version produced before.

    Why it had to change at all: the Anthropic branch used to `return body["system"],
    tools, list(msgs)` and never look at the message roles. A review sent a body with
    BOTH a top-level `system` and a `role: system` message of 5000 bytes, and got
    `fixed B = 33` with 5070 bytes filed as conversation -- the same defect as the
    `developer` role, reached through the other branch. Nothing else could have caught
    it: the coverage invariant is over top-level fields and cannot see inside `messages`.
    """
    tools = body.get("tools") or []
    msgs = body.get("messages") or []
    instr, convo = [], []
    for m in msgs:
        is_instr = isinstance(m, dict) and m.get("role") in INSTRUCTION_ROLES
        (instr if is_instr else convo).append(m)
    parts = []
    if body.get("system"):                      # Anthropic Messages
        parts.append(body["system"])
    if instr:                                   # OpenAI chat completions, or both at once
        parts.append(instr)
    return parts, tools, convo


def decompose(rec, tok=None):
    """One captured request -> its full accounting. Raises nothing; errors are fields."""
    body = rec.get("body") or {}
    fields = {k: nbytes(v) for k, v in body.items()}
    whole = nbytes(body)
    framing = predicted_framing(body)
    summed = sum(fields.values())
    meta = sum(v for k, v in fields.items() if k in PROVIDER_METADATA)
    system_parts, tools, convo = normalise_scopes(body)
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
        "coverage_ok": coverage_holds(summed, framing, whole),
        "coverage_residual": whole - (summed + framing),
        # The most a NORMALISED serialisation can legitimately exceed the compact bytes
        # a client sent: one byte per separator. `json.dumps` writes `", "`/`": "` where
        # a compact client writes `,`/`:`.
        #
        # That is not the same as "nothing else can differ", which is what an earlier
        # version of this comment claimed and is false about JSON. Two other things
        # differ, and the direction is what matters: ESCAPING choices (`\uXXXX` for
        # non-ASCII, an escaped solidus, long escapes for backspace and form feed) make
        # the CLIENT's bytes larger, which is safe here -- the refusal only fires when
        # the normalised side is larger. NUMBER spelling can go the other way (`1E5`
        # re-serialised as `100000.0`), and the refusal's own text names it, because this
        # bound cannot distinguish that from a mis-decoded body.
        #
        # Counted over the whole string, so string CONTENTS inflate it too --
        # deliberately generous, because this bound is used to REFUSE.
        "separator_slack": ser(body).count(", ") + ser(body).count(": "),
        "provider_metadata_bytes": meta,
        "model_input_bytes": whole - meta,
        "engine_specific_bytes": sum(v for k, v in fields.items() if k in ENGINE_SPECIFIC),
        "system_bytes": sum(nbytes(p) for p in system_parts),
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
        row["system_tokens"] = sum(tok(p) for p in system_parts)
        row["tools_tokens"] = tok(tools) if tools else 0
        row["conversation_tokens"] = tok(convo) if convo else 0
        row["fixed_tokens"] = row["system_tokens"] + row["tools_tokens"]
    return row


def structural_fault(rec):
    """-> a reason string if `rec` is not a record this reader can read, else None.

    Only the SHAPE every reader below assumes is checked: a mapping, whose `body` is a
    mapping. WHICH fields a body carries is the unknown-field report's job, not this
    one's -- a door-check that also policed contents would refuse the next engine's
    first capture instead of naming its new field, which is the opposite of the point.
    """
    if not isinstance(rec, dict):
        return ("is JSON but not an object (it is a %s). A capture record is a mapping; "
                "every reader below asks it for named keys"
                % type(rec).__name__)
    body = rec.get("body")
    if body is not None and not isinstance(body, dict):
        return ("has a `body` that is JSON but not an object (it is a %s). A body is "
                "decomposed field by named field, which a %s has none of"
                % (type(body).__name__, type(body).__name__))
    # A body that cannot be encoded as UTF-8 -- a lone surrogate, from a client that
    # truncated a string mid-character. It is valid JSON, so it parses and reaches here;
    # every byte count below is taken over a UTF-8 encoding, so there is no honest length
    # to report for it. Refused by name, because the alternative was a UnicodeEncodeError
    # inside nbytes() and exit 1 -- against decompose()'s own "Raises nothing; errors are
    # fields" -- which a review reproduced.
    if body is not None:
        try:
            ser(body).encode("utf-8")
        except UnicodeEncodeError as e:  # noqa: PERF203
            return ("has a body that cannot be encoded as UTF-8 (%s). A client that "
                    "truncates a string mid-character emits a lone surrogate, which is "
                    "valid JSON and unmeasurable in bytes: there is no UTF-8 length to "
                    "report. The recorder keeps such a request deliberately; this reader "
                    "refuses it rather than inventing a length for it" % e.reason)
    # A body the RECORDER never read. Refused here, ahead of the `raw_bytes` legs,
    # because its `raw_bytes` is deliberately null -- an unframeable request has a body
    # of UNKNOWN length, and recording that as 0 is what turned 40391 chunked bytes into
    # a startup probe in a review's demonstration. "has no `raw_bytes`" would be a true
    # but useless reason for it; this one names the request.
    if isinstance(body, dict) and UNREAD in body:
        return ("has a body capture_proxy.py never READ (%s), so its length is unknown "
                "rather than zero. Measuring around it would file real traffic under "
                "`startup probes` -- the omission that looks like good news"
                % body[UNREAD])
    # `raw_bytes` -- the field the report calls its ONLY direct observation. Checked here
    # because the first version of this function validated `rec` and `body` and stopped
    # exactly short of it, and a review found four record shapes that still reached a
    # traceback: absent or 0 divided by zero inside the wire-overrun note added in the
    # same round, and null or a string hit `int += None` in the per-model totals. All
    # four exited 1, which is not in this instrument's documented set -- the precise
    # class this door-check exists to end, reintroduced by a fix for something else.
    raw = rec.get("raw_bytes")
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
    if body is not None:
        # `tools` and `messages` are COUNTED and the counts are published. A review
        # passed a dict for `tools` and got `2 tool schema(s) exposed` from its keys, and
        # a string for `messages` and got `5 message(s)` from its characters -- both at
        # exit 0, because len() obliges on anything. The coverage invariant is over
        # TOP-LEVEL fields and cannot see inside one, so nothing else would catch it.
        for k in ("tools", "messages"):
            v = body.get(k)
            if v is not None and not isinstance(v, list):
                return ("has a `%s` that is %s rather than a list. Its length is "
                        "published as a count, and len() of the wrong container counts "
                        "the wrong thing (a dict's keys, a string's characters)"
                        % (k, type(v).__name__))
    return None


def duplicate_keys(pairs, into):
    """A json object_pairs_hook that RECORDS repeated keys instead of losing them.

    `json.loads` keeps the last value for a repeated key, silently. That is not an
    academic case for this instrument: `{"system": <10 KB>, "system": "x"}` parsed to a
    3-byte system prompt, the coverage invariant held (it is checked against the PARSED
    body, which really is self-consistent), and the published fixed-cost figure was
    wrong by 10 KB with no symptom anywhere. Found by a review. It is the same class as
    the unknown-field case this file exits 4 for -- bytes that were on the wire and are
    in no column -- so it gets the same treatment: named, and refused.
    """
    seen = set()
    for k, _ in pairs:
        if k in seen:
            into.append(k)
        seen.add(k)
    return dict(pairs)


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
            dups = []
            try:
                rec = json.loads(line,
                                 object_pairs_hook=lambda p: duplicate_keys(p, dups))
            except Exception as e:  # noqa: BLE001
                return None, ("capture line %d of %s is not JSON (%r). A partially "
                              "readable capture is refused rather than measured: the "
                              "missing lines would silently shrink every total."
                              % (n, path, e))
            if dups:
                return None, ("capture line %d of %s repeats the JSON key(s) %s. "
                              "json.loads keeps the LAST value and drops the rest "
                              "silently, so those bytes were on the wire and would "
                              "appear in no column -- the same defect an unknown field "
                              "exits 4 for. Refusing."
                              % (n, path, ", ".join(sorted(set(dups)))))
            # Parsing is not the same as being readable, and the gap between the two used
            # to be an AttributeError 100 lines downstream rather than a refusal here:
            # every reader below does `rec.get(...)` / `body.get(...)`, so a line that
            # parses to a LIST or a STRING died inside is_probe with a traceback naming a
            # dict method, no capture file, no line number, and an exit code (1) that is
            # not in this instrument's documented set. Found while probing this
            # instrument's own edges, and independently by a review of the same branch.
            # Refusing the WHOLE capture rather than skipping the line, for the same
            # reason the unparseable case does: a dropped record shrinks every total
            # silently, which is the one failure that looks like good news.
            fault = structural_fault(rec)
            if fault:
                return None, ("capture line %d of %s %s. The whole capture is refused "
                              "rather than measured around it: skipping the line would "
                              "shrink every total silently."
                              % (n, path, fault))
            recs.append(rec)
    if not recs:
        return None, "capture file is empty (0 requests): %s" % path
    return recs, None


def measure(paths, tok=None):
    """-> (report, exit_code). Every refusal carries its reason in the report."""
    rep = {"captures": list(paths), "requests": 0, "probes": 0, "measurable": 0,
           "rows": [], "errors": [], "unknown_fields": [], "coverage_broken": []}
    arm_source = {}
    for p in paths:
        recs, err = read_capture(p)
        if err:
            rep["errors"].append(err)
            continue
        rep["requests"] += len(recs)
        # Records the RECORDER could not parse. Refused here rather than in
        # read_capture, because this is not a malformed capture file -- it is a
        # perfectly good record of traffic this instrument cannot decompose, and the
        # distinction belongs in the reason the operator reads.
        unp = [(i, r) for i, r in enumerate(recs, 1)
               if UNPARSEABLE in (r.get("body") or {})]
        if unp:
            rep["errors"].append(
                "%s has %d request(s) whose body capture_proxy.py could not parse "
                "(line%s %s, %d raw bytes between them). The recorder kept them "
                "deliberately, because a dropped request would show up as a smaller "
                "payload -- but they carry neither `tools` nor `messages`, so measuring "
                "the rest would file real traffic under `startup probes` and print a "
                "100%%-coverage banner over a total that is missing it. Refusing the "
                "capture: fix the recorder or the client, do not average around this."
                % (p, len(unp), "" if len(unp) == 1 else "s",
                   ", ".join(str(i) for i, _ in unp),
                   sum(int(r.get("raw_bytes") or 0) for _, r in unp)))
            continue
        for r in recs:
            if is_probe(r):
                rep["probes"] += 1
                continue
            row = decompose(r, tok)
            row["capture"] = p
            rep["rows"].append(row)
            rep["measurable"] += 1
            # One arm label, two captures -- see this function's own note above.
            # Keyed on the ARM ALONE. A review keyed it on `(arm, endpoint)` and
            # survived every gate: two captures of the same engine hitting different
            # endpoints would collapse into one row again, which is the whole defect
            # C41 exists to catch. The table is keyed by arm and nothing else.
            first_seen = arm_source.setdefault(row["arm"], p)
            if first_seen != p:
                dupe = ("captures %s and %s are both labelled arm %r. The table and the "
                        "published claims are keyed by arm and keep the FIRST request "
                        "per arm, so the second capture's figures are counted in "
                        "`measurable` and appear in no row and in no claim -- a whole "
                        "capture that no gate reads. Relabel one at capture time "
                        "(capture_proxy.py --arm), or compare a rerun against its "
                        "reference with --reference, which is what that flag is for."
                        % (first_seen, p, row["arm"]))
                if dupe not in rep["errors"]:
                    rep["errors"].append(dupe)
            for f in row["unknown_fields"]:
                if f not in rep["unknown_fields"]:
                    rep["unknown_fields"].append(f)
            # An overrun BEYOND what separators can explain. The note above says an
            # overrun is normal, and it is -- up to one byte per separator. Past that the
            # normalised split is not a re-serialisation of these wire bytes at all, and
            # printing it under a reassuring note is how a review's one-word recorder
            # mutation published a 2.9x overstatement at exit 0 with coverage green.
            #
            # Two causes produce this, and this instrument cannot tell them apart: a
            # recorder that mis-decoded the body (the mutation: utf-8 -> ascii, whose
            # replacement characters tripled a CJK payload), or a client whose NUMBER
            # spelling differs from json.dumps' (`1E5` re-serialised as `100000.0`).
            # Both mean the split does not describe the bytes in `wire B`, so both are
            # refused and the reason names both. Headroom on the real captures here is
            # 18 B on dsh and 693 B on Hermes -- see docs/limits.md.
            if row["body_bytes"] - row["wire_bytes"] > row["separator_slack"] > 0:
                rep["errors"].append(
                    "%s request %d: the normalised body is %d B against %d B observed on "
                    "the wire, an overrun of %d B where this body's separators explain at "
                    "most %d. A normalised split larger than the wire figure is normal "
                    "(json.dumps writes `\", \"` where a compact client writes `,`), but "
                    "not by this much: either the recorder mis-decoded the body, or this "
                    "client spells numbers differently from json.dumps. Either way the "
                    "split is not a re-serialisation of those bytes, and the table would "
                    "publish it as one."
                    % (p, len(rep["rows"]), row["body_bytes"], row["wire_bytes"],
                       row["body_bytes"] - row["wire_bytes"], row["separator_slack"]))
            if not row["coverage_ok"]:
                rep["coverage_broken"].append(
                    "%s request %d: fields(%d) + framing(%d) != body(%d), residual %d"
                    % (p, len(rep["rows"]) - 1, row["summed_field_bytes"],
                       row["framing_bytes"], row["body_bytes"], row["coverage_residual"]))
    # ANY refusal is fatal, even when another capture on the same command line produced
    # a perfectly good row. This used to read `if rep["errors"] and not rep["rows"]`,
    # and a review broke every refusal in this file with one extra argument:
    #
    #   measure_request_construction.py fixtures/<any-bad>.jsonl fixtures/dsh.jsonl
    #     -> the refusal printed, then a complete table, then exit 0
    #
    # which is the module docstring's own headline danger -- "a complete, plausible,
    # internally consistent table that is simply too small" -- in the one invocation
    # shape a two-engine comparison actually uses (samples/...both.out). The
    # `requests: 5 captured, 1 measurable, 0 startup probes` line did not even add up,
    # because the refused capture's records were counted and never classified, and
    # nothing said so. A capture this instrument refused is a hole in the total; whether
    # some OTHER capture was fine has no bearing on that.
    if rep["errors"]:
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
    # One claim set per ARM, from that arm's FIRST measurable request. It used to read
    # `rep["rows"][0]` and so pinned the dsh column only: a review pointed out that
    # nothing anywhere pinned a single Hermes figure, so the whole right-hand column of
    # README's table could be rewritten -- or the two columns swapped -- with every gate
    # green. A bare table cell is also unpinnable in practice: a one-digit figure like
    # `9` matches all but a handful of this repository's artefacts by plain substring, so
    # no exclusion list can protect it. A per-arm CLAUSE can be, which is why these
    # exist. (No count here on purpose: an earlier version said 62, measured over a
    # corpus the gate does not search, and nothing read it to notice.)
    out = []
    first_by_arm = {}
    for row in rep["rows"]:
        first_by_arm.setdefault(row["arm"], row)
    for arm, r in first_by_arm.items():
        out.extend(_claims_for_arm(arm, r))
    return out


def _claims_for_arm(arm, r):
    # Each claim carries the DIRECTION of its figure, not just the digits. A review kept
    # every number in this README byte-identical and inverted the sentence around them --
    # "`6519 bytes (75.1%) of provider metadata` is BILLED AS MODEL INPUT on every turn",
    # the exact error that flipped the source study's headline -- and every gate in this
    # repository stayed green, because a figure-substring check reads digits and not
    # meaning. Pinning the whole clause means the prose has to still contain the correct
    # statement rather than only its digits.
    #
    # THAT IS ALL IT DOES, and the first version of this comment overclaimed it. It said
    # an inversion "has to DELETE the claim to make room for itself"; a second review
    # showed it only has to REFRAME it. Every clause left byte-identical and still in the
    # prose, relabelled as "the draft this branch has now RETRACTED", with the opposite
    # asserted in words around it -- every gate green. Quoting a wrong figure beside its
    # correction is this repository's own established idiom (scripts/check_figures.sh has
    # six EXEMPT entries for exactly that), which is what makes the reframing read as
    # normal.
    #
    # So: a claim is checked for PRESENCE, and presence cannot distinguish a statement
    # from a quotation of it. Nothing in this repository reads meaning. What pinning
    # whole clauses buys is that the digits can no longer be kept while the verb is
    # flipped IN PLACE -- a real narrowing, and a much smaller one than it first claimed.
    lbl = str(arm)
    out = [("%s: wire bytes" % lbl, "%d bytes on the wire" % r["wire_bytes"]),
           ("%s: fixed part" % lbl,
            "fixed %d bytes, paid again on every later turn" % r["fixed_bytes"]),
           ("%s: tool schemas" % lbl, "%d tool schema(s) exposed" % r["n_tools"]),
           ("%s: model input" % lbl,
            "model input %d bytes of a %d-byte body, which is what a per-token bill is "
            "computed from" % (r["model_input_bytes"], r["body_bytes"]))]
    if r["provider_metadata_bytes"]:
        # Share OF THE BODY, and the claim says so. The first published version of this
        # sentence divided by `wire_bytes` in the prose while the figure was computed
        # over `body_bytes`: 6519/8225 is 79.3% and 6519/8680 is 75.1%, so the sentence
        # named one denominator and quoted the other's answer. Naming the denominator
        # inside the claim is what stops that being re-derivable.
        pct = 100.0 * r["provider_metadata_bytes"] / r["body_bytes"]
        out.append(("%s: provider metadata" % lbl,
                    "%d bytes (%.1f%% of the body) of provider metadata, which the "
                    "vendor documents at ZERO model-input tokens"
                    % (r["provider_metadata_bytes"], pct)))
    else:
        # An arm with NO provider metadata needs its own claim too, or "none at all" is a
        # sentence no gate reads and the two arms are not comparably pinned.
        out.append(("%s: provider metadata" % lbl,
                    "no provider metadata at all, so its model input and its whole body "
                    "are the same %d bytes" % r["body_bytes"]))
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
            over = r["body_bytes"] - r["wire_bytes"]
            if over > 0:
                out.append("    normalised-vs-wire: this body is %d B over the wire, of "
                           "%d B its separators explain (%d B of headroom before the "
                           "refusal)"
                           % (over, r["separator_slack"], r["separator_slack"] - over))
            out.append("    %-22s %10d B  = the whole body, with nothing unaccounted"
                       % ("TOTAL", r["body_bytes"]))
            out.append("    %d tool schema(s) exposed, %d message(s) in the conversation"
                       % (r["n_tools"], r["n_messages"]))
            ratio = (r["wire_bytes"] / r["body_bytes"]) if r["body_bytes"] else 0.0
            out.append("    wire %d B vs normalised body %d B (ratio %.3f). The split "
                       "above is of the NORMALISED serialization:"
                       % (r["wire_bytes"], r["body_bytes"], ratio))
            out.append("      a per-field share of the raw wire bytes is not well "
                       "defined without re-parsing, and the two differ by the")
            out.append("      client's own separator and escaping choices. Quote `wire "
                       "B` for what the network paid.")
            if r["wire_bytes"] and r["model_input_bytes"] > r["wire_bytes"]:
                # Not a defect, and printed because it LOOKS like one: a client that
                # serialises compactly (no space after `:` or `,`) sends fewer bytes than
                # `json.dumps` default separators produce, so a normalised column can
                # legitimately exceed the observed wire figure. On the Hermes capture
                # here `fixed B` is 101.2%% of `wire B` and model input 101.7%%. Said
                # out loud rather than left for a reader to find and distrust the table.
                out.append("    NOTE: model input %d B and fixed %d B EXCEED wire %d B "
                           "(%.1f%%), within the %d B this body's separators explain. "
                           "Not an error: this client serialises more"
                           % (r["model_input_bytes"], r["fixed_bytes"], r["wire_bytes"],
                              100.0 * r["model_input_bytes"] / r["wire_bytes"],
                              r["separator_slack"]))
                out.append("      compactly than json.dumps' default separators, so the "
                           "normalised split is larger than what was sent. `wire B` is "
                           "the observation; the rest is a re-serialisation.")
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
                mi_dens = ((r["model_input_bytes"] / r["model_input_tokens"])
                           if r["model_input_tokens"] else 0.0)
                out.append("    tokens: body %d, model input %d, provider metadata %d"
                           % (r["body_tokens"], r["model_input_tokens"],
                              r["provider_metadata_tokens"]))
                # BOTH densities, because they answer different questions and a review
                # showed the difference is the whole claim. Over the WHOLE body the two
                # engines measured here differ by ~30%% -- but one body is three quarters
                # provider metadata (dense JSON punctuation) and the other has none, so
                # that figure is mostly the metadata's own density, not the engines'. On
                # MODEL INPUT, the quantity a bill is computed from, they are within ~3%%.
                # Quoting the first as if it were about billing is the error here.
                out.append("      %.2f bytes/token over the whole body, %.2f bytes/token "
                           "over MODEL INPUT." % (dens, mi_dens))
                out.append("      The second is the one a cost argument needs; the first "
                           "includes provider metadata, which is billed at zero tokens "
                           "and tokenises")
                out.append("      at its own density. Comparing whole-body densities "
                           "across engines with different metadata compares the "
                           "metadata, not the engines.")
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
def _fixture(tmp, name, bodies, arm="fx", raw_bytes=None):
    """`raw_bytes=None` records the body's own normalised length, which is the common
    case. Pass a number to make the recorded WIRE length differ from it -- without that,
    `wire_bytes == body_bytes` by construction and no leg here can tell the two apart,
    which is how a mutation substituting one for the other stayed invisible."""
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as fh:
        for b in bodies:
            fh.write(json.dumps({"arm": arm, "endpoint": "/v1/messages",
                                 "ts": 0,
                                 "raw_bytes": (len(ser(b).encode())
                                               if raw_bytes is None else raw_bytes),
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
        no("provider-metadata split wrong: metadata in the fixture %d B, reported as "
           "%d B, MODEL INPUT %d B where body(%d) - metadata(%d) = %d"
           % (meta, r["provider_metadata_bytes"], r["model_input_bytes"],
              r["body_bytes"], meta, r["body_bytes"] - meta))
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

    # OpenAI RENAMED `system` to `developer`, and both are live on the endpoint.
    # Recognising only `system` put a 12 KB instruction block in the conversation column
    # and published `fixed B = 49` -- found by a review, and it is case C6's defect
    # exactly, for the spelling the provider documents today rather than the older one
    # the control happened to use.
    dev = _fixture(tmp, "oai-developer.jsonl", [{"model": "m", "tools": [{"name": "t"}],
                                                 "messages": [
                                                     {"role": "developer",
                                                      "content": "SYSTEM-TEXT"},
                                                     {"role": "user", "content": "hi"}]}],
                   arm="openai-developer")
    rd, _ = measure([dev])
    # The two system columns differ by exactly 3 bytes and must: "developer" is three
    # characters longer than "system", and the column is the serialized MESSAGE, role
    # field included. Asserting the exact delta rather than equality is the point -- it
    # says the block moved column without pretending the two payloads are identical.
    d_sys, o_sys = rd["rows"][0]["system_bytes"], ro["rows"][0]["system_bytes"]
    if d_sys == o_sys + 3 and rd["rows"][0]["n_messages"] == 1 \
            and rd["rows"][0]["conversation_bytes"] \
            == ro["rows"][0]["conversation_bytes"]:
        ok("a `role: developer` message lands in the SAME column as `role: system` "
           "(%d B vs %d B, the 3-byte difference being the role NAME) and the "
           "conversation column is identical -- the spelling OpenAI documents today"
           % (d_sys, o_sys))
    else:
        no("role=developer treated as conversation: system=%d (vs %d for role=system), "
           "msgs=%d" % (d_sys, o_sys, rd["rows"][0]["n_messages"]))

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

    print("== 11. a capture line this reader cannot READ is refused, not measured "
          "around ==")
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
    # Valid JSON of the wrong SHAPE. All four of these raised AttributeError before the
    # door-check existed, exiting 1 -- which is not in this instrument's documented set.
    # The leg asserts a NAMED refusal and not merely non-zero: a traceback is also
    # non-zero, and it tells the operator nothing about which file or line to look at.
    shapes = [("a record that is a list", [1, 2, 3], "not an object"),
              ("a record that is a string", "oops", "not an object"),
              ("a `body` that is a list", {"arm": "fx", "body": [1, 2]},
               "`body` that is JSON but not an object"),
              ("a `body` that is a string", {"arm": "fx", "body": "oops"},
               "`body` that is JSON but not an object")]
    for label, rec, want in shapes:
        bad = os.path.join(tmp, "shape-%d.jsonl" % len(passed))
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        try:
            rep, code = measure([bad])
        except Exception as e:  # noqa: BLE001
            no("%s raised %s instead of refusing" % (label, type(e).__name__))
            continue
        if code == 2 and any(want in e and "line 1" in e for e in rep["errors"]):
            ok("%s -> exit 2, naming the file and the LINE (it used to raise "
               "AttributeError and exit 1)" % label)
        else:
            no("%s -> exit %d, errors=%r" % (label, code, rep["errors"]))
    # ...and the door-check must NOT refuse the shapes that are legitimately not bodies.
    # `body: null` is what a record with no parsed payload looks like, and that is a
    # probe, not a fault -- a shape-check firing here would refuse real captures.
    nullbody = os.path.join(tmp, "nullbody.jsonl")
    with open(nullbody, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"arm": "fx", "endpoint": "/v1/messages", "ts": 0,
                             "raw_bytes": 0, "header_names": [], "body": None}) + "\n")
    rep, code = measure([nullbody])
    if code == 2 and rep["probes"] == 1 and not rep["errors"][0].startswith("capture"):
        ok("`body: null` is still a PROBE, not a structural fault -- the check refuses "
           "the wrong shape without refusing a legitimate record")
    else:
        no("`body: null` -> exit %d, probes=%d, errors=%r"
           % (code, rep["probes"], rep["errors"]))
    # A REPEATED top-level key. json.loads keeps the last value, so the 10 KB first one
    # vanishes and the parsed body is self-consistent -- coverage holds, and the fixed
    # cost is published 10 KB too low. Bytes on the wire that are in no column, same
    # class as the unknown-field case, so the same answer: named and refused.
    dup = os.path.join(tmp, "dup.jsonl")
    with open(dup, "w", encoding="utf-8") as fh:
        # raw_bytes is the ~10 KB really on the wire, not a token value: the parsed
        # body is 60-odd bytes once json.loads has dropped the first `system`, and a
        # wire figure BELOW the body would trip the separator-bound refusal first --
        # sending this case RED for a reason it does not name.
        fh.write('{"arm": "fx", "endpoint": "/v1/messages", "ts": 0, '
                 '"raw_bytes": 10093, '
                 '"header_names": [], "body": {"model": "m", "messages": [], '
                 '"system": "%s", "system": "x", "tools": [{"name": "t"}]}}\n'
                 % ("S" * 10000))
    rep, code = measure([dup])
    if code == 2 and any("repeats the JSON key(s) system" in e for e in rep["errors"]):
        ok("a REPEATED top-level key is named and refused (json.loads keeps the last "
           "value, so a 10 KB system prompt silently became 3 bytes with coverage green)")
    else:
        no("duplicate key -> exit %d, errors=%r" % (code, rep["errors"]))

    print("== 11b. the four columns the README publishes, against hand arithmetic ==")
    # A review's strongest structural point: the self-test asserted nothing whatever
    # about `fixed_bytes`, `wire_bytes`, `n_tools` or the published percentage -- four of
    # the five figures in the README table. `wire_bytes` was specifically untestable,
    # because the fixture helper set raw_bytes to the body's own length, so substituting
    # one for the other changed nothing any leg could see.
    known = {"model": "m", "system": "0123456789",       # ser '"0123456789"'    = 12 B
             "tools": [{"name": "t"}],                    # ser '[{"name": "t"}]' = 15 B
             "messages": [{"role": "user", "content": "hi"}]}
    kf = _fixture(tmp, "known-columns.jsonl", [known], raw_bytes=12345)
    rk, _ = measure([kf])
    r = rk["rows"][0]
    want = {"system_bytes": 12, "tools_bytes": 15, "fixed_bytes": 27,
            "n_tools": 1, "n_messages": 1, "wire_bytes": 12345}
    got = {k: r[k] for k in want}
    if got == want:
        ok("hand-computed columns all match: system 12 + tools 15 = fixed 27 B, 1 tool, "
           "1 message, and wire 12345 B read from the RECORD (not re-derived from the "
           "body, which is %d B here -- so the two are now distinguishable)"
           % r["body_bytes"])
    else:
        no("published columns disagree with hand arithmetic: got %r want %r"
           % (got, want))
    # And the overrun note, which exists because a normalised split CAN exceed the wire
    # figure legitimately and looks like a defect when it does.
    text = report(rk, 0, None, None, [])
    if "EXCEED wire" not in text and r["model_input_bytes"] < r["wire_bytes"]:
        ok("no overrun note when model input is below the wire figure")
    else:
        no("overrun note fired on a capture where model input < wire")
    # body 113 B, separators explain 11 -> 103 observed is an overrun of 10, inside it
    over = _fixture(tmp, "overrun.jsonl", [known], raw_bytes=103)
    ro2, _ = measure([over])
    if "EXCEED wire 103 B" in report(ro2, 0, None, None, []):
        ok("when the normalised split EXCEEDS the wire figure, the report says so "
           "(unflagged, `fixed B` larger than `wire B` reads as a broken instrument)")
    else:
        no("a normalised split larger than the wire figure was printed unflagged")

    print("== 11c. the claims pin EVERY arm, and the percentage names its denominator ==")
    # Two review findings in one leg. headline_claims() read rep["rows"][0], so nothing
    # anywhere pinned a single Hermes figure and the whole right-hand column of README's
    # table -- or a swap of the two columns -- was publishable with every gate green. And
    # the metadata percentage's denominator had no control: changing `/ body_bytes` to
    # `/ wire_bytes` left the self-test at exit 0, which is exactly the error the
    # published sentence had made in prose.
    meta_body = {"model": "m", "tools": [{"name": "t"}],
                 "messages": [{"role": "user", "content": "hi"}],
                 "dsh_session_log": {"pad": "L" * 3000}}
    mf = _fixture(tmp, "two-arms-a.jsonl", [meta_body], arm="arm-one", raw_bytes=99)
    mf2 = _fixture(tmp, "two-arms-b.jsonl", [dict(plain)], arm="arm-two", raw_bytes=77)
    rm2, _ = measure([mf, mf2])
    claims = headline_claims(rm2)
    labels = {lab.split(":")[0] for lab, _ in claims}
    if labels == {"arm-one", "arm-two"}:
        ok("a two-arm capture yields claims for BOTH arms (%d claims over %s) -- it used "
           "to pin only rows[0], leaving one whole engine unpinned"
           % (len(claims), ", ".join(sorted(labels))))
    else:
        no("claims cover only %r" % sorted(labels))
    r0 = rm2["rows"][0]
    want_pct = 100.0 * r0["provider_metadata_bytes"] / r0["body_bytes"]
    wrong_pct = 100.0 * r0["provider_metadata_bytes"] / r0["wire_bytes"]
    got = [c for lab, c in claims if lab == "arm-one: provider metadata"][0]
    if ("(%.1f%% of the body)" % want_pct) in got and want_pct != wrong_pct:
        ok("the metadata share is computed over the BODY (%.1f%%), and this fixture's "
           "wire figure would give %.1f%% -- so the denominator is pinned, not assumed"
           % (want_pct, wrong_pct))
    else:
        no("metadata share %r: body %.1f%% wire %.1f%%" % (got, want_pct, wrong_pct))

    print("== 11d. a request with tools but no messages is a TURN, not a probe ==")
    # A client may send a tools-only request; `is_probe` keyed on `messages` alone would
    # file it as a startup probe and omit its bytes from every total. Nothing else here
    # would notice, because an omitted request shrinks the totals silently.
    toolsonly = _fixture(tmp, "tools-only.jsonl",
                         [{"model": "m", "tools": [{"name": "t"}]}], raw_bytes=50)
    rt, ct = measure([toolsonly])
    if ct == 0 and rt["measurable"] == 1 and rt["probes"] == 0:
        ok("a tools-only request is measured, not filed as a startup probe")
    else:
        no("tools-only request -> exit %d, measurable=%d probes=%d"
           % (ct, rt["measurable"], rt["probes"]))

    print("== 11e. a refused capture is fatal even beside a good one ==")
    # The bypass a review used to defeat every refusal in this file at once: add a second,
    # valid capture to the command line. measure() appended the error and carried on, and
    # the only hard stop fired when there were NO rows at all -- so the refusal printed,
    # and then a complete table, and then exit 0. Every other measure() call in this
    # self-test passes a single capture, which is why nothing looked here.
    good = _fixture(tmp, "good-beside-bad.jsonl", [dict(plain)], raw_bytes=120)
    for label, bad_path in (("a corrupt line", corrupt),
                            ("a probe-only capture", probes)):
        rep2, code2 = measure([bad_path, good])
        if label == "a probe-only capture":
            continue            # probe-only is not an error on its own; checked above
        if code2 == 2 and not rep2.get("by_model"):
            ok("%s beside a GOOD capture still exits 2 and prints no table -- a refused "
               "capture is a hole in the total whether or not another one was fine"
               % label)
        else:
            no("%s beside a good capture -> exit %d" % (label, code2))

    print("== 11f. the fields whose LENGTH is published must be the right container ==")
    # `n_tools` and `n_messages` are printed as counts, and len() obliges on anything: a
    # review passed a dict for `tools` and got `2 tool schema(s) exposed` from its keys,
    # and a string for `messages` and got `5 message(s)` from its characters, both at
    # exit 0. The coverage invariant is over TOP-LEVEL fields and cannot see inside one,
    # so this is the only place it can be caught.
    for k, v, shape in (("tools", {"a": 1, "b": 2}, "a dict"),
                        ("messages", "hello", "a string")):
        bad = os.path.join(tmp, "container-%s.jsonl" % k)
        body = {"model": "m", "tools": [{"name": "t"}],
                "messages": [{"role": "user", "content": "hi"}]}
        body[k] = v
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"arm": "fx", "endpoint": "/v1/messages", "ts": 0,
                                 "raw_bytes": 100, "header_names": [],
                                 "body": body}) + "\n")
        rep, code = measure([bad])
        if code == 2 and any("`%s` that is" % k in e for e in rep["errors"]):
            ok("a `%s` that is %s is refused by name, not counted (len() of the wrong "
               "container counts the wrong thing and prints it as a schema count)"
               % (k, shape))
        else:
            no("`%s` as %s -> exit %d, errors=%r" % (k, shape, code, rep["errors"]))

    print("== 11g. raw_bytes -- the only direct observation -- is validated ==")
    # Four record shapes a review found reaching a traceback and exit 1, which is not in
    # this instrument's documented exit set. Two of them arrived via the wire-overrun
    # NOTE added in the same round as the door-check, which is the uncomfortable part:
    # the door-check validated `rec` and `body` and stopped exactly short of the field
    # the report calls its only observation.
    shapes = [("absent", None, "has no `raw_bytes`"),
              ("null", "NULL", "has no `raw_bytes`"),
              ("a string", "8225", "not an integer"),
              ("negative", -1, "negative `raw_bytes`"),
              ("0 beside a body that parsed", 0, "`raw_bytes: 0` but a body")]
    for label, val, want in shapes:
        bad = os.path.join(tmp, "rawbytes-%s.jsonl" % label.split()[0])
        rec = {"arm": "fx", "endpoint": "/v1/messages", "ts": 0, "header_names": [],
               "body": {"model": "m", "tools": [{"name": "t"}],
                        "messages": [{"role": "user", "content": "hi"}]}}
        if val == "NULL":
            rec["raw_bytes"] = None
        elif val is not None:
            rec["raw_bytes"] = val
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        try:
            rep, code = measure([bad])
        except Exception as e:  # noqa: BLE001
            no("raw_bytes %s raised %s instead of refusing" % (label, type(e).__name__))
            continue
        if code == 2 and any(want in e for e in rep["errors"]):
            ok("raw_bytes %s -> exit 2, named (it used to reach a ZeroDivisionError or a "
               "`int += None` and exit 1)" % label)
        else:
            no("raw_bytes %s -> exit %d, errors=%r" % (label, code, rep["errors"]))

    print("== 11h. instruction roles are read in BOTH protocol branches ==")
    # The Anthropic branch used to return the whole `messages` list as conversation
    # without looking at any role. A review sent a body carrying BOTH a top-level
    # `system` and a 5000-byte `role: system` message and got `fixed B = 33`, with 5070
    # bytes filed as conversation -- the developer-role defect through the other branch,
    # landing on the headline figure.
    # THREE instruction-role shapes at once, and the arithmetic is hand-computed:
    #   top-level "TOP-LEVEL-SYSTEM"                       ser  18 B
    #   [{role: system, 100xS}, {role: developer, 10xD}]   ser 183 B
    #                                              system_bytes 201 B
    #   tools [{"name": "t"}]                              ser  15 B
    #                                               fixed_bytes 216 B
    #   [{role: user, "hi"}]                   conversation_bytes 35 B, 1 message
    #
    # TWO instruction messages of DIFFERENT lengths is the point: with only one, keeping
    # just the first is indistinguishable from keeping all of them, which is how a
    # six-character mutation walked through 51 passing checks. Truncating to the first
    # here reports system_bytes 153 (the list is 135 B alone), and the exact-equality
    # assertion below sees a 48-byte difference that `> 5000` could not.
    mixed_body = {"model": "m", "system": "TOP-LEVEL-SYSTEM",
                  "tools": [{"name": "t"}],
                  "messages": [{"role": "system", "content": "S" * 100},
                               {"role": "developer", "content": "D" * 10},
                               {"role": "user", "content": "hi"}]}
    mx = _fixture(tmp, "both-scope-shapes.jsonl", [mixed_body], raw_bytes=400)
    rx, cx = measure([mx])
    rr = rx["rows"][0]
    want_mixed = {"system_bytes": 201, "tools_bytes": 15, "fixed_bytes": 216,
                  "conversation_bytes": 35, "n_messages": 1}
    got_mixed = {k: rr[k] for k in want_mixed}
    if cx == 0 and got_mixed == want_mixed:
        ok("a top-level `system` AND a role:system AND a role:developer message all "
           "land in the system column (18 + 183 = 201 B, +15 B of tools = 216 B fixed), "
           "leaving 1 conversation message of 35 B -- the Anthropic branch reads roles "
           "too, which it did not, and it reads ALL of them, which a six-character "
           "mutation showed nothing was checking")
    else:
        no("mixed scope shapes: exit %d, got %r want %r" % (cx, got_mixed, want_mixed))

    print("== 11i. an overrun BEYOND what separators explain is refused, not noted ==")
    # The note above says an overrun is normal, and up to one byte per separator it is.
    # Past that the normalised split is not a re-serialisation of the observed bytes at
    # all -- and the note, which carried no ceiling, told the reader it was fine. A
    # review changed one word in the recorder (utf-8 -> ascii), whose replacement
    # characters tripled a CJK payload from 6303 to 18303 B, and got a 290.4% overrun
    # printed under "Not an error" at exit 0 with coverage green.
    #
    # These two fixtures differ by ONE byte of wire: at 101 the overrun is 12 and is
    # refused, at 102 it is exactly the 11 the separators explain and is noted. The
    # boundary is the bound itself, not a threshold somebody picked.
    for wire, want_refusal in ((101, True), (102, False)):
        ob = _fixture(tmp, "overrun-%d.jsonl" % wire, [known], raw_bytes=wire)
        ro, oc = measure([ob])
        refused = oc == 2 and any("separators explain at most 11" in e
                                  for e in ro["errors"])
        if refused == want_refusal:
            ok("wire %d B against a 113 B body (overrun %d, separators explain 11) -> %s"
               % (wire, 113 - wire,
                  "refused by name" if refused else "measured, with the note"))
        else:
            no("wire %d B -> exit %d, errors=%r" % (wire, oc, ro["errors"]))

    print("== 11j. two captures under ONE arm label are refused, not merged ==")
    # Both the table and the claim set keep the first request per arm, so a second
    # capture sharing a label contributes figures to `measurable` that no row shows and
    # no claim pins. A review reproduced round-2 finding 3 exactly this way, using the
    # two committed dsh fixtures.
    same_a = _fixture(tmp, "arm-collision-a.jsonl", [known], raw_bytes=400)
    same_b = _fixture(tmp, "arm-collision-b.jsonl", [known], raw_bytes=401)
    rc, cc = measure([same_a, same_b])
    if cc == 2 and any("both labelled arm" in e for e in rc["errors"]):
        ok("two captures sharing one arm label -> exit 2, naming both files (they used "
           "to collapse into one row, with the second capture pinned by nothing)")
    else:
        no("arm collision -> exit %d, rows=%d, errors=%r"
           % (cc, len(rc["rows"]), rc["errors"]))
    # ...including when the two captures hit DIFFERENT endpoints. A review keyed the
    # collision on `(arm, endpoint)` and survived every gate -- which puts the same
    # engine's two captures back in one row whenever they differ that way, and one
    # engine's figures back outside every gate's reach. The table is keyed by arm and by
    # nothing else, so this is the shape that has to be refused too.
    other_ep = dict(known)
    oe_a = _fixture(tmp, "arm-ep-a.jsonl", [known], raw_bytes=400)
    oe_b = os.path.join(tmp, "arm-ep-b.jsonl")
    with open(oe_b, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"arm": "fx", "endpoint": "/v1/chat/completions", "ts": 0,
                             "raw_bytes": 401, "header_names": [],
                             "body": other_ep}) + "\n")
    rg, cg = measure([oe_a, oe_b])
    if cg == 2 and any("both labelled arm" in e for e in rg["errors"]):
        ok("two captures sharing an arm but hitting DIFFERENT endpoints are refused too "
           "-- the key is the arm alone, and keying it on (arm, endpoint) silently "
           "merged them again")
    else:
        no("arm collision across endpoints -> exit %d, rows=%d, errors=%r"
           % (cg, len(rg["rows"]), rg["errors"]))
    # ...and the same two records in ONE file are not a collision: that is an ordinary
    # multi-request capture, and refusing it would refuse every real one.
    twin = _fixture(tmp, "two-in-one-capture.jsonl", [known, known], raw_bytes=400)
    rt, ct = measure([twin])
    if ct == 0 and rt["measurable"] == 2 and len(rt["rows"]) == 2:
        ok("two requests in ONE capture are still measured (2 rows) -- the refusal is "
           "about two FILES claiming one label, not about a capture with history")
    else:
        no("two records in one capture -> exit %d, measurable=%d rows=%d errors=%r"
           % (ct, rt["measurable"], len(rt["rows"]), rt["errors"]))

    print("== 11k. what the RECORDER could not read or encode is refused BY NAME ==")
    # Two record shapes capture_proxy.py writes deliberately, neither of which this
    # reader could refuse informatively until a review pointed it out:
    #
    #  * `__unread__` -- a body whose framing the recorder could not follow, so its
    #    length is UNKNOWN. Deleting this reader's branch for it still exits 2, but on
    #    "has no `raw_bytes`" -- true, useless, and it survived every gate because
    #    nothing asserted WHICH refusal fires. A refusal that cannot name the request is
    #    how an operator ends up re-capturing instead of fixing the client.
    #  * a lone surrogate -- valid JSON, unencodable as UTF-8, and every byte count here
    #    is taken over a UTF-8 encoding. It used to raise inside nbytes() and exit 1,
    #    against decompose()'s own "Raises nothing; errors are fields".
    for name, rec, want in (
        ("a body the recorder could not read",
         {"arm": "fx", "endpoint": "/v1/messages", "ts": 0, "raw_bytes": None,
          "header_names": [], "body": {"__unread__": "`Transfer-Encoding: gzip`"}},
         "never READ"),
        ("a body that cannot be encoded as UTF-8",
         {"arm": "fx", "endpoint": "/v1/messages", "ts": 0, "raw_bytes": 70,
          "header_names": [],
          "body": {"model": "m",
                   "messages": [{"role": "user", "content": "hi \ud83d"}]}},
         "cannot be encoded as UTF-8")):
        bad = os.path.join(tmp, "recorder-%s.jsonl" % want.split()[0])
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
        try:
            rr, rc = measure([bad])
        except Exception as e:  # noqa: BLE001
            no("%s raised %s instead of refusing" % (name, type(e).__name__))
            continue
        if rc == 2 and any(want in e for e in rr["errors"]):
            ok("%s -> exit 2, refused on %r (not on a true-but-useless reason, and not "
               "with a traceback)" % (name, want))
        else:
            no("%s -> exit %d, errors=%r" % (name, rc, rr["errors"]))

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
    # The token path cannot be exercised here at all -- tiktoken is deliberately not a
    # dependency and is not installed on the box this was written on -- so a review
    # replaced the encoder with `len(bytes)//4` and every gate in the repository stayed
    # green, while the output still said `tiktoken:cl100k_base`. These two legs test the
    # one thing that CAN be tested without the dependency: the guard that runs on the
    # real encoder at load time and refuses to label division as tokenisation.
    class _BytesOver4:
        """An encoder with tiktoken's interface that is really integer division."""
        def encode(self, s, disallowed_special=()):      # noqa: D102
            return [0] * (len(s.encode("utf-8")) // 4)

    fn, why = make_counter(_BytesOver4(), "tiktoken:cl100k_base")
    if fn is None and why and "ratio barely varies" in why:
        ok("an encoder that is really len(bytes)//4 is REFUSED by the LOAD PATH rather "
           "than labelled tiktoken:cl100k_base -- the guard and its call site, not just "
           "the predicate")
    else:
        no("len(bytes)//4 passed as a tokeniser: fn=%r why=%r" % (fn, why))
    # ...and the CLASS, not one spelling. The first version of this guard compared
    # against the literal `len(bytes)//4`; a review wrote five other fixed divisors and
    # every one was accepted and labelled tiktoken:cl100k_base.
    variants = [("len//4", lambda s: len(s.encode("utf-8")) // 4),
                ("round(len/4)", lambda s: round(len(s.encode("utf-8")) / 4)),
                ("ceil(len/4)", lambda s: (len(s.encode("utf-8")) + 3) // 4),
                ("len//4 + 1", lambda s: len(s.encode("utf-8")) // 4 + 1),
                ("len//5", lambda s: len(s.encode("utf-8")) // 5),
                ("int(len/3.8)", lambda s: int(len(s.encode("utf-8")) / 3.8))]
    slipped = [n for n, f in variants if looks_like_a_fixed_divisor(f) is None]
    if not slipped:
        ok("all %d fixed-divisor spellings are refused, not just the one literal "
           "expression (%s) -- the guard tests the CLASS, which is what "
           "docs/limits.md claims" % (len(variants), ", ".join(n for n, _ in variants)))
    else:
        no("these byte estimates passed as tokenisers: %s" % ", ".join(slipped))
    # and the other direction: something that actually varies must pass, or the guard is
    # a refusal of everything wearing a gate's name.
    varied = looks_like_a_fixed_divisor(
        lambda s: max(1, len(s.split()) + s.count("{") * 3
                      + sum(ord(c) > 0x3000 for c in s)))
    if varied is None:
        ok("a count whose bytes-per-token ratio DOES vary across the probes passes")
    else:
        no("the spread guard rejected a non-division token count: %s" % varied)

    print("== 13. an UNPARSEABLE recorded body is refused, never filed as a probe ==")
    # The recorder stores a body it could not parse under __unparseable__ rather than
    # dropping the record, because a dropped request shows up as a smaller payload. But
    # such a record has no `tools` and no `messages`, so the plain probe test called it a
    # startup probe: a review fed in three 48 KB records and got "3 startup probes", a
    # 100% coverage banner and exit 0. Mislabelled is worse than omitted -- the count
    # looks accounted for.
    mixed = os.path.join(tmp, "mixed.jsonl")
    with open(mixed, "w", encoding="utf-8") as fh:
        fh.write(open(small, encoding="utf-8").read())
        for _ in range(3):
            fh.write(json.dumps({"arm": "fx", "endpoint": "/v1/messages", "ts": 0,
                                 "raw_bytes": 48000, "header_names": [],
                                 "body": {UNPARSEABLE: "<48000 bytes>"}}) + "\n")
    rep, code = measure([mixed])
    if code == 2 and any("could not parse" in e and "144000 raw bytes" in e
                         for e in rep["errors"]):
        ok("3 unparseable records refuse the capture and their 144000 raw bytes are "
           "NAMED (they used to be reported as startup probes, with exit 0)")
    else:
        no("unparseable records -> exit %d, probes=%d, errors=%r"
           % (code, rep["probes"], rep["errors"]))
    if is_probe({"body": {UNPARSEABLE: "x"}}) is False:
        ok("is_probe() says an unparseable body is NOT a probe, which is the one-line "
           "distinction the whole refusal rests on")
    else:
        no("is_probe() still calls an unparseable body a startup probe")

    print("== 14. the coverage invariant's own COMPARISON, pinned ==")
    # One character from `<=`, and `<=` passes every deficit -- the only direction a
    # dropped field can go. A review mutated it and the whole preflight stayed green,
    # because every other leg here feeds a COMPLETE split, where == and <= agree.
    if coverage_holds(100, 20, 120) and not coverage_holds(100, 20, 130) \
            and not coverage_holds(100, 20, 110):
        ok("coverage_holds is exact equality: a 10-byte DEFICIT fails and a 10-byte "
           "SURPLUS fails (`<=` would pass the deficit, which is the dropped-field case)")
    else:
        no("coverage_holds admits an inequality: deficit=%r surplus=%r"
           % (coverage_holds(100, 20, 130), coverage_holds(100, 20, 110)))

    print("== 15. framing is right for keys that are not plain ASCII words ==")
    # predicted_framing measures the SERIALIZED key, not len(key)+2. A review mutated it
    # to the latter and nothing noticed, because every fixture above uses short ASCII
    # keys where the two agree. These do not: a multibyte key costs more bytes than
    # characters, and a key needing escapes costs more serialized than raw.
    awkward = {"\u6771\u4eac\u306e\u30c4\u30fc\u30eb": [{"name": "t"}],
               'a"quote': "x", "back\\slash": "y", "new\nline": "z",
               "messages": [{"role": "user", "content": "hi"}]}
    aw = _fixture(tmp, "awkward.jsonl", [awkward])
    rep, code = measure([aw])
    r = rep["rows"][0]
    naive = 2 + sum(len(k) + 4 for k in awkward) + (len(awkward) - 1) * 2
    if r["coverage_ok"] and r["coverage_residual"] == 0 \
            and r["framing_bytes"] != naive:
        ok("multibyte and escape-needing keys: coverage still exact, and framing %d B "
           "differs from the len(key)+2 shortcut's %d B -- so the shortcut is a "
           "DETECTABLE revert" % (r["framing_bytes"], naive))
    else:
        no("awkward keys: coverage_ok=%r residual=%r framing=%d naive=%d"
           % (r["coverage_ok"], r["coverage_residual"], r["framing_bytes"], naive))

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
