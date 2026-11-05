#!/usr/bin/env python3
"""Did this agent run obtain text it was supposed to have lost -- and by which route?

THE SETUP THIS ANSWERS. You strip a repository's instruction files (`CLAUDE.md`, `AGENTS.md`,
`.hermes.md`, `.mcp.json`, `.claude/`) to measure what an agent does WITHOUT them, and you want
to know whether the bare run actually stayed bare. Deleting the files does not settle it: an
agent with a shell can read the same text out of the clone's own git history, out of a sibling
worktree, out of a session transcript store, or out of any committed archive that happens to
quote it. This reads the run's own tool record and reports, per run, whether that happened.

WHY A SCAN OF TOOL ARGUMENTS IS NOT ENOUGH, which is the whole reason this exists:

  1. REACH and OBTAIN are separate questions. Scanning arguments for an instruction-file path
     answers "did it reach". It does not answer "did it get it" -- a `cat` of a path that does
     not exist reaches and obtains nothing, and a command whose arguments name no instruction
     file at all can still return the rules in its OUTPUT (`grep -rl '<a rule>' .` is the
     obvious one). This reads the tool RESULT, not just the request.
  2. Every obtain is PAIRED back to its own call by call id, so a hit's route is established
     rather than inferred. Without pairing, "it read something off-clone" and "something
     off-clone came back" are one blurred claim.
  3. One uniform finding shape across clients, so a per-client quirk cannot make a real finding
     score zero.

THE THREE CLASSES A VERDICT IS BUILT FROM. Collapsing them into one number is the mistake:

  AUTO-LOADED        -- the harness was injected by the client itself. NOT MEASURABLE from a
                        tool log in either direction, because a client that injects instructions
                        into its system prompt leaves nothing in its tool record. This therefore
                        does NOT report on it, and says so rather than printing a reassuring
                        zero. The valid instrument for that is a behavioural probe.
  OFF-CLONE          -- text obtained from outside the run's own tree: a sibling worktree, the
                        clone's git history, a transcript store. This is the route a strip is
                        supposed to close, so a hit here is BLOCKING.
  IN-CORPUS          -- text obtained from inside the tree the run was given, because some
                        committed file there quotes the rules. Not fixable by deleting
                        instruction files, and SYMMETRIC: the WITH run reads the same file. It
                        narrows what a with/without difference MEANS rather than biasing it.
                        Reported, never dismissed, never counted as blocking.

HONEST LIMITS, because the gap is part of the finding:
  * A clean result is EVIDENCE, NOT ENFORCEMENT. A run could obtain the text through a command
    whose arguments and output this does not recognise. If you need enforcement, you need
    isolation the agent cannot reach around, which is a property of the box and not of a script.
  * It audits the record each client keeps. A run whose record is missing reports NO-RECORD,
    which is not the same as a clean run and is never printed as one.
  * Your needle set is not exhaustive. A run that obtained a paraphrase, or a section no needle
    covers, is invisible here. `needle_power.py` quantifies the other end of this: how many
    places in the corpus could have produced each needle, and how many are unique to the file
    you care about. Run it on your needles before reading a clean verdict as an absence.
  * The ONE exception to needle-gating is deliberate: on an asymmetric route, PROVENANCE
    establishes the obtain and no needle is required. See the comment at that branch -- it was
    a false negative on the single most important run in the study this came from.

WHAT IS NOT HERE. The discovery half is yours. This ships two generic modes -- a directory of
`capture_proxy.py` captures, and explicit `--arm` specs -- because a study's own
directory layout, run-naming scheme and client-vs-state bookkeeping are not portable and
pretending otherwise would hide a wrong run-to-record mapping inside a tool that looks generic.
Write a discovery function for your layout and call `audit_arm` yourself if neither mode fits.
"""
import argparse
import collections
import glob
import json
import os
import re
import sqlite3
import sys
import tempfile

# ---------------------------------------------------------------------------------------------
# Readers. Each returns a list of Call records, which is the normalisation the audit core needs:
#   Call(call_id, tool, args_text, result_text)
# `args_text` is what the run ASKED FOR, `result_text` is what came BACK into its context. A
# reader that cannot pair the two must say so by leaving result_text None -- never "".
# ---------------------------------------------------------------------------------------------
Call = collections.namedtuple("Call", "call_id tool args result")


def read_cc(transcript):
    """Claude Code's on-disk transcript: tool_use blocks paired to tool_result by tool_use_id."""
    uses, results = [], {}
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    uses.append((b.get("id"), b.get("name"), json.dumps(b.get("input") or {})))
                elif b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = _flatten(b.get("content"))
    return [Call(i, n, a, results.get(i)) for i, n, a in uses]


def read_dsh(events):
    """dsh's ACP event log: tool/call {callId,name,arguments} + tool/result {toolCallId,message}."""
    calls, results = [], {}
    with open(events, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = (d.get("params") or {}).get("event")
            if not isinstance(ev, dict):
                continue
            data = ev.get("data") or {}
            if ev.get("type") == "tool/call":
                calls.append((data.get("callId"), data.get("name"),
                              data.get("arguments") if isinstance(data.get("arguments"), str)
                              else json.dumps(data.get("arguments") or {})))
            elif ev.get("type") == "tool/result":
                cid = data.get("toolCallId") or ((data.get("message") or {})
                                                 .get("toolCallId"))
                results[cid] = _flatten(((data.get("message") or {}).get("content")))
    return [Call(i, n, a, results.get(i)) for i, n, a in calls]


def read_hermes(db, session_id):
    """Hermes keeps no transcript; its history is state.db.

    `messages.tool_calls` on an assistant row is what was asked for; the following role='tool'
    row with the matching `tool_call_id` is what came back. Returns None -- NOT [] -- when the
    session is absent, because "no record" and "no calls" are different answers.
    """
    if not os.path.isfile(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        if not con.execute("select 1 from sessions where id = ?", (session_id,)).fetchone():
            return None
        rows = con.execute(
            "select role, coalesce(tool_name,''), coalesce(tool_calls,''), "
            "coalesce(tool_call_id,''), coalesce(content,'') "
            "from messages where session_id = ? order by id", (session_id,)).fetchall()
    except sqlite3.Error as e:
        raise SystemExit(f"REFUSING: state.db unreadable for {session_id}: {e}")
    results = {r[3]: r[4] for r in rows if r[0] == "tool" and r[3]}
    out = []
    for role, tname, tcalls, tcid, _content in rows:
        if role != "assistant" or not tcalls:
            continue
        # tool_calls is a JSON array; EACH element carries its OWN function.arguments.
        #
        # Taking per-element arguments matters and this script's first version did not: it gave
        # every id in the row the WHOLE row's serialised text as its args. 15 of 40 assistant
        # rows in one real arm carry more than one call, so a benign call paired to a real
        # result inherited a sibling call's off-clone path -- and an obtain was then credited to
        # a route it did not come from. It produced exactly that on hermes_without_S_d1: a 126 B
        # `find` output attributed to the `git show HEAD~1:CLAUDE.md` route. The arm's verdict was
        # right for an independent reason (its own 3,066 B git-show result), which is precisely
        # why the mis-attribution would have survived a glance at the verdict column.
        parsed = None
        try:
            parsed = json.loads(tcalls)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list) and parsed:
            for c in parsed:
                if not isinstance(c, dict):
                    continue
                cid = c.get("id") or c.get("call_id") or c.get("tool_call_id")
                fn = c.get("function") if isinstance(c.get("function"), dict) else {}
                args = fn.get("arguments")
                name = fn.get("name") or tname
                if not isinstance(args, str):
                    # No per-element arguments available. Fall back to the whole row, and SAY so
                    # on the call, so a finding built on it can be discounted rather than
                    # silently trusted.
                    args = json.dumps(c) if c else tcalls
                    name = (name or "") + " [row-level args]"
                out.append(Call(cid, name, args, results.get(cid)))
        else:
            # Unparseable tool_calls: keep the raw text rather than drop the call, since a
            # dropped call is an unaudited one.
            out.append(Call(None, tname, tcalls, None))
    # Any tool result we could not pair to an assistant call still entered the arm's context, so
    # it is carried as an unpaired call rather than dropped.
    paired = {c.call_id for c in out}
    for role, tname, _tc, tcid, content in rows:
        if role == "tool" and tcid and tcid not in paired:
            out.append(Call(tcid, tname, "", content))
    return out


def read_capture(cap):
    """`capture_proxy.py` format: one JSON object per captured HTTP request, `body` = the wire body.

    There is no separate tool log here -- the tool trace, if any, is INSIDE each request's
    `messages` array, because that is how an agent loop feeds a tool result back to the model.
    So a capture with no `tool_use`/`tool_result` block anywhere is positive evidence that the
    arm never executed a tool, which is a stronger statement than "no instruction read found".
    """
    uses, results = [], {}
    with open(cap, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            body = d.get("body")
            if not isinstance(body, dict):
                continue
            for m in body.get("messages") or []:
                c = m.get("content") if isinstance(m, dict) else None
                if not isinstance(c, list):
                    continue
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        uses.append((b.get("id"), b.get("name"),
                                     json.dumps(b.get("input") or {})))
                    elif b.get("type") == "tool_result":
                        results[b.get("tool_use_id")] = _flatten(b.get("content"))
    # A later request repeats the whole history, so the same tool_use appears many times. Dedupe
    # by id: without this an arm's call count would scale with its request count, not its work.
    seen, out = set(), []
    for i, n, ar in uses:
        if i in seen:
            continue
        seen.add(i)
        out.append(Call(i, n, ar, results.get(i)))
    return out


def _flatten(content):
    """Tool-result content -> one string. None stays None; a list of blocks is joined."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or json.dumps(b))
            else:
                parts.append(str(b))
        return "\n".join(p for p in parts if p)
    return json.dumps(content)


# ---------------------------------------------------------------------------------------------
# Audit core
# ---------------------------------------------------------------------------------------------

Patterns = collections.namedtuple(
    "Patterns", "instr_file instr_any path clipped githist githist_instr failed")

# The instruction files a coding agent auto-loads. Override with --instr-files when the agent
# you are measuring loads something else: the whole audit is keyed off this one string, so a
# client whose instruction file is not in it would read as clean everywhere, which is the
# failure mode this parameter exists to prevent.
DEFAULT_INSTR_FILE = r"CLAUDE\.md|AGENTS\.md|\.hermes\.md|\.mcp\.json"
DEFAULT_INSTR_DIR = r"\.claude/"


def build_patterns(instr_file=DEFAULT_INSTR_FILE, instr_dir=DEFAULT_INSTR_DIR):
    """Compile the matchers. Separated from module scope so --instr-files is real, not cosmetic."""
    instr_any = instr_file + "|" + instr_dir
    # The capture needs a LEFT BOUNDARY, and this was found by running the audit rather than by
    # reading it. Without one, `cat knowledge/harnesses/templates/.../.claude/settings.json` -- a
    # RELATIVE path inside the arm's own clone -- was clipped at its first slash to
    # `/harnesses/templates/.../.claude/settings.json`, which does not start with the arm's tree and
    # so scored as an off-clone instruction read. One arm (dsh_without_M_d1) was reported
    # CONTAMINATED on exactly that, and the real path was in-tree. A clipped token is now its own
    # class rather than silently becoming an absolute path.
    pathpat = re.compile(r"(?:(?<=^)|(?<=[\s\"'`=(,;:]))(/[^\s\"'`,;)]*(?:" + instr_any +
                         r")[^\s\"'`,;)]*)")
    clipped = re.compile(r"(?<=[\w$~.*\\-])(/[^\s\"'`,;)]*(?:" + instr_any + r")[^\s\"'`,;)]*)")
    # `git show <rev>:<path>` / `git cat-file` / `git archive` are off-clone in TIME rather than in
    # space: the path is relative and inside the tree, so no absolute-path check can see them. This
    # is a route a real run in the originating study used, so it is matched explicitly.
    #
    # BUT the revision qualifier alone is not contamination, and treating it as such is a false
    # positive this audit shipped and then had to fix: five arms were reported CONTAMINATED for
    # `git show HEAD:scripts/pre-commit-checks.sh` and `git show --stat ... -- notes/incident_stats/`
    # -- a code-change task reading its own SCRIPT out of history, and a diagnosis task reading commit
    # subjects. Neither recovers an instruction file. A git-history call is therefore only blocking
    # when the command also names an instruction ARTEFACT, which is what `git show HEAD~1:CLAUDE.md`
    # does and what these do not.
    githist = re.compile(r"git\s+(?:-C\s+\S+\s+)?(?:show|cat-file|archive)\b[^\n]*"
                         r"(?:HEAD|[0-9a-f]{7,40})[~^:][^\n]*")
    githist_instr = re.compile(instr_any)
    # A result that is an error has obtained nothing. Matched on the failure vocabulary of the three
    # verbs that actually appear on these routes (git, cat/sed, the shell).
    failed = re.compile(r"No such file or directory|does not exist|fatal:|command not found|"
                        r"Permission denied|exists on disk, but not in|unknown revision|"
                        r"ambiguous argument", re.I)
    return Patterns(instr_file=instr_file, instr_any=instr_any, path=pathpat,
                    clipped=clipped, githist=githist, githist_instr=githist_instr,
                    failed=failed)


DEFAULT_PATTERNS = build_patterns()


def classify_path(path, pat):
    """Which surface did this path name? Decided by WHICH TOKEN matched, not by wildcard count.

    That choice is load-bearing: the tempting fix -- downgrade
    anything containing a glob -- would also hide `cat /other/*/CLAUDE.md`, a glob that really
    does re-acquire the instructions. So a named instruction FILE is blocking whether globbed or
    not, and only a bare `.claude/` DIRECTORY reference is downgraded.

    The ORDER below is a correction to the first version, which tested `.claude/projects`
    first: that made the grep PATTERN
    `/claude\\b|\\.claude/projects|CLAUDE_CODE_|credentials\\.json` -- an alternation in a
    command, not a path -- classify as a transcript-store READ. Pattern detection now runs after
    the named-file test and before the directory tests, so a real file stays blocking and a
    regex alternation stops counting as an access.
    """
    if re.search(pat.instr_file, path):
        return "instruction_file"
    if "|" in path or "\\b" in path or "\\." in path:
        return "pattern_string"
    if ".claude/projects" in path:
        return "transcript_store"
    if "*" in path:
        return "pattern_string"
    return "instruction_file"


def audit_arm(arm, client, tree, calls, needles, state, pat=None):
    """-> a dict verdict for one run. `calls` None means NO RECORD, which is not 'clean'."""
    pat = pat or DEFAULT_PATTERNS
    row = {"arm": arm, "client": client, "harness_state": state, "tree": tree}
    if calls is None:
        row.update(record="NO-RECORD", verdict="NOT-AUDITABLE", calls=0,
                   note="no tool-call record for this arm; absent evidence is not a clean verdict")
        return row
    row["calls"] = len(calls)
    row["results_paired"] = sum(1 for c in calls if c.result is not None)
    if not calls:
        # A record that exists and contains ZERO tool executions is a different and stronger
        # answer than "no instruction read was found in its tool calls": no side channel of any
        # kind was available to this arm, because it never ran a command. Kept distinct from
        # CLEAN-ON-RECORD so a payload probe is never tallied beside an agent that did work.
        row.update(verdict="NO-TOOL-EXECUTION",
                   counts={"reach_instruction_file": 0, "reach_transcript_store": 0,
                           "reach_pattern_string": 0, "obtain_asymmetric": 0,
                           "obtain_symmetric": 0, "git_history_cmds": 0,
                           "git_history_naming_instruction": 0, "clipped_fragments": 0},
                   reach_offclone=[], obtain=[], git_history_cmds=[], clipped_fragments=0,
                   note="record present, zero tool executions: no side channel was reachable")
        return row

    reach, obtain, githist, clipped, asym_failed = [], [], [], [], []
    for c in calls:
        args = c.args or ""
        offclone_classes = set()
        seen_spans = []
        for m in pat.path.finditer(args):
            p = m.group(1)
            seen_spans.append(m.span(1))
            if p.startswith(tree):
                continue
            cls = classify_path(p, pat)
            reach.append({"tool": c.tool, "path": p[:200], "class": cls})
            offclone_classes.add(cls)
        for m in pat.clipped.finditer(args):
            # A capture whose left neighbour is a word character is a FRAGMENT of a longer token,
            # so whether it is off-clone is unknown rather than true. Counted, never blocking.
            #
            # UNLESS it sits inside a path the loop above already resolved. `/w/other/CLAUDE.md`
            # is matched whole by pat.path, and its tail `/CLAUDE.md` is then matched again here
            # because `r` is a word character -- so one off-clone read was reported BOTH as a
            # reach and as an unresolvable fragment. The clipped column exists for genuinely
            # ambiguous tokens; a path already classified is not ambiguous.
            lo, hi = m.span(1)
            if any(lo >= s and hi <= e for s, e in seen_spans):
                continue
            clipped.append({"tool": c.tool, "fragment": m.group(1)[:120]})
        gh_instr = False
        for m in pat.githist.finditer(args):
            cmd = m.group(0)
            names_instr = bool(pat.githist_instr.search(cmd))
            gh_instr = gh_instr or names_instr
            githist.append({"tool": c.tool, "cmd": cmd[:200],
                            "names_instruction_artefact": names_instr})
        # Route is decided by THIS CALL's own arguments -- the point of pairing -- and the
        # off-clone route is further split by the CLASS of the path it used, because a sibling
        # worktree's CLAUDE.md and a session transcript store are not the same finding: the
        # first is asymmetric and the strip is supposed to close it, the second is read by the
        # WITH arm just as much.
        asym = gh_instr or ("instruction_file" in offclone_classes)
        if gh_instr:
            route = "git_history_instruction"
        elif "instruction_file" in offclone_classes:
            route = "offclone_instruction_file"
        elif "transcript_store" in offclone_classes:
            route = "offclone_transcript_store"
        else:
            route = "in_corpus_or_unattributed"

        if asym:
            # ON AN ASYMMETRIC ROUTE, PROVENANCE ESTABLISHES THE OBTAIN AND A NEEDLE IS NOT
            # NEEDED. This was a false negative in the first version of this audit, on the single
            # most important arm in the dataset: hermes_without_S_d1 ran
            # `git show HEAD~1:CLAUDE.md > /tmp/CLAUDE.md && wc -c && grep -n "917|native rail|
            # git_branch|branch" /tmp/CLAUDE.md`, and the 3,066 B that came back are the file's
            # size plus ~60 numbered lines of its content -- two of which are lines
            # `needle_power.py --auto` independently identifies as unique to CLAUDE.md. None of
            # the eleven needles in needles.json happened to be among the lines the ARM chose to
            # grep, so a needle-gated rule scored the known-contaminated arm as not-obtained.
            # An arm that greps for its own needles gets text our needle list does not cover; the
            # fix is to stop asking the needle list a question provenance already answers.
            if c.result is None:
                state_ = "unpaired"
            elif not c.result.strip():
                state_ = "returned_nothing"
            elif pat.failed.search(c.result[:400]):
                state_ = "returned_error"
            else:
                state_ = "obtained"
            if state_ == "obtained":
                obtain.append({"tool": c.tool, "route": route, "evidence": "provenance",
                               "needles": sorted(n for n in needles if n in c.result)[:4],
                               "args": args[:240], "result_bytes": len(c.result),
                               "result_head": c.result[:300]})
            else:
                # Its OWN list, not `reach`. Appending it to `reach` as another
                # `instruction_file` double-counted the same call -- the off-clone path was
                # already recorded there by PATHPAT -- so one failed read reported
                # reach_instruction_file = 2. That is the inflated-count class this task is
                # about, in my own counter, and the self-test leg that found it asserts the
                # number rather than the verdict.
                asym_failed.append({"tool": c.tool, "route": route, "outcome": state_,
                                    "args": args[:200]})
        elif c.result:
            # Off an asymmetric route, provenance proves nothing -- the arm is reading its own
            # given corpus, which it is entitled to do -- so here a NEEDLE is the evidence that
            # harness text specifically came back.
            hit = [n for n in needles if n in c.result]
            if hit:
                obtain.append({"tool": c.tool, "route": route, "evidence": "needle",
                               "needles": sorted(hit)[:4], "args": args[:240],
                               "result_bytes": len(c.result)})
    row["reach_offclone"] = reach
    row["obtain"] = obtain
    row["git_history_cmds"] = githist
    row["clipped_fragments"] = len(clipped)
    row["asym_failed"] = asym_failed

    reach_instr = [r for r in reach if r["class"] == "instruction_file"]
    reach_tsc = [r for r in reach if r["class"] == "transcript_store"]
    gh_instr_cmds = [g for g in githist if g["names_instruction_artefact"]]
    # ASYMMETRIC: a route the strip is meant to close, available to the bare arm as a way back to
    # text it was supposed to have lost.
    obtain_asym = [o for o in obtain
                   if o["route"] in ("git_history_instruction", "offclone_instruction_file")]
    # SYMMETRIC: harness text that really did arrive, by a route the WITH arm uses identically --
    # the fleet's transcript store, or this repository's own committed Tasks/ archive. It narrows
    # what a with/without difference MEANS; it does not bias the difference either way.
    obtain_sym = [o for o in obtain
                  if o["route"] in ("offclone_transcript_store", "in_corpus_or_unattributed")]

    row["counts"] = {"reach_instruction_file": len(reach_instr),
                     "reach_transcript_store": len(reach_tsc),
                     "reach_pattern_string": len(reach) - len(reach_instr) - len(reach_tsc),
                     "obtain_asymmetric": len(obtain_asym),
                     "obtain_symmetric": len(obtain_sym),
                     "git_history_cmds": len(githist),
                     "git_history_naming_instruction": len(gh_instr_cmds),
                     "clipped_fragments": len(clipped),
                     "asym_reach_no_return": len(asym_failed)}
    if obtain_asym:
        row["verdict"] = "CONTAMINATED-OFFCLONE"
    elif reach_instr or gh_instr_cmds or asym_failed:
        row["verdict"] = "REACHED-ASYMMETRIC-NO-RETURN"
    elif obtain_sym:
        row["verdict"] = "OBTAINED-VIA-SYMMETRIC-ROUTE"
    elif reach_tsc:
        # Touched the fleet's transcript store but no harness text came back on any paired
        # result. Distinguished from CLEAN because the store demonstrably CAN return our rules
        # verbatim -- it did for three other arms -- so "it reached in and got nothing of ours"
        # is a different and weaker statement than "it never reached in".
        row["verdict"] = "SYMMETRIC-REACH-NO-TEXT"
    else:
        row["verdict"] = "CLEAN-ON-RECORD"
    return row


# ---------------------------------------------------------------------------------------------
# Discovery. Deliberately thin -- see "WHAT IS NOT HERE" in the module docstring.
# ---------------------------------------------------------------------------------------------
READERS = {"cc": "read_cc", "dsh": "read_dsh", "hermes": "read_hermes", "capture": "read_capture"}


def refuse(msg):
    """Cannot-measure exits 2, NEVER 1.

    1 is "it audited, and something is contaminated". A refusal that also exited 1 would reach a
    caller as the same signal as a real finding -- and the one thing this audit must never do is
    let "never ran" be read as a verdict.
    """
    print(f"REFUSING (exit 2): {msg}")
    sys.exit(2)


def load_needles(path):
    """A JSON list of strings, or an object whose list values are unioned (keys starting with
    `_` ignored, so a `_comment` array is documentation rather than data)."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        refuse(f"needle file unreadable: {path}: {e}")
    if isinstance(d, list):
        vals = d
    elif isinstance(d, dict):
        vals = [x for k, v in d.items() if not k.startswith("_") and isinstance(v, list)
                for x in v]
    else:
        refuse(f"{path} must be a JSON list or object, not {type(d).__name__}.")
    needles = sorted({s for s in vals if isinstance(s, str) and s.strip()})
    if not needles:
        refuse(f"{path} yields an empty needle set; every run would score a clean 0.")
    return needles


def read_arm_record(client, record):
    """Dispatch to the right reader. For hermes, `record` is `<db path>#<session id>`."""
    if client == "cc":
        return read_cc(record)
    if client == "dsh":
        return read_dsh(record)
    if client == "capture":
        return read_capture(record)
    if client == "hermes":
        if "#" not in record:
            refuse(f"a hermes record is '<state.db>#<session id>'; got {record!r}")
        db, sid = record.rsplit("#", 1)
        return read_hermes(db, sid)
    refuse(f"unknown client {client!r}; expected one of {', '.join(sorted(READERS))}")


def discover_captures(capdir, needles, label_re, trees, pat):
    """Every `capture_proxy.py` capture in a directory, audited as one run each."""
    out = []
    p_re = re.compile(label_re) if label_re else None
    for p in sorted(glob.glob(os.path.join(capdir, "*.jsonl"))):
        base = os.path.basename(p)[:-len(".jsonl")]
        if base.endswith(".events"):
            continue                      # an ACP event stream, not a request capture
        if p_re and not p_re.match(base):
            continue
        row = audit_arm(base, base.split("_", 1)[0], os.path.join(trees, base),
                        read_capture(p), needles, "unknown", pat)
        row["source_set"] = os.path.basename(capdir.rstrip("/")) or capdir
        out.append(row)
    return out


def discover_explicit(specs, needles, pat):
    """--arm NAME CLIENT RECORD TREE [STATE], repeatable. The honest portable mode: you say
    which record belongs to which run, because only you know."""
    out = []
    for spec in specs:
        if len(spec) not in (4, 5):
            refuse("--arm takes NAME CLIENT RECORD TREE [STATE]; got "
                   f"{len(spec)} value(s): {spec!r}")
        name, client, record, tree = spec[:4]
        state = spec[4] if len(spec) == 5 else "unknown"
        if client not in READERS:
            refuse(f"--arm {name}: unknown client {client!r}; expected one of "
                   f"{', '.join(sorted(READERS))}")
        row = audit_arm(name, client, tree, read_arm_record(client, record), needles, state, pat)
        row["source_set"] = "explicit"
        out.append(row)
    return out


# ---------------------------------------------------------------------------------------------
def _mk_calls(*triples):
    return [Call(f"c{i}", t, a, r) for i, (t, a, r) in enumerate(triples)]


def _selftest():
    """Every leg pins one discrimination this audit would be worthless without.

    Three of them are regression tests for defects this code actually shipped: a relative
    in-tree path clipped into a fake absolute one (leg 6), a benign `git show` of a SCRIPT
    scored as contamination (leg 4), and an asymmetric obtain missed because none of the
    needles happened to be in the text the run chose to grep (leg 5). Each is asserted on the
    COUNT as well as the verdict, because two of them were originally found by a count being
    wrong while the verdict happened to be right.
    """
    import shutil
    import subprocess
    fails, ran = [], []

    def check(label, got, want):
        ran.append(label)
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: got {got!r}, want {want!r}")
        if not ok:
            fails.append(label)

    pat = DEFAULT_PATTERNS
    nd = ["ONE SESSION, ONE WORKTREE", "a rule the harness states verbatim"]
    tree = "/w/arm"
    me = os.path.abspath(__file__)
    d = tempfile.mkdtemp(prefix="arm_sidechannel_selftest_")
    try:
        # 1. CLEAN: an in-tree read of an ordinary file that returns none of the needles.
        r = audit_arm("a1", "cc", tree, _mk_calls(("Bash", '{"command":"cat /w/arm/src/main.py"}',
                                                   "def main(): pass")), nd, "bare", pat)
        check("leg1 in-tree ordinary read is CLEAN-ON-RECORD", r["verdict"], "CLEAN-ON-RECORD")

        # 2. REACH, NO OBTAIN -- the distinction a scan of arguments alone cannot make.
        r = audit_arm("a2", "cc", tree,
                      _mk_calls(("Bash", '{"command":"cat /w/other/CLAUDE.md"}',
                                 "cat: /w/other/CLAUDE.md: No such file or directory")),
                      nd, "bare", pat)
        check("leg2 failed off-clone read is REACHED-ASYMMETRIC-NO-RETURN",
              r["verdict"], "REACHED-ASYMMETRIC-NO-RETURN")
        check("leg2 nothing was obtained", r["counts"]["obtain_asymmetric"], 0)
        # The double-count bug: one failed read must appear ONCE, not once per detector.
        check("leg2 one failed read counts once", r["counts"]["reach_instruction_file"], 1)

        # 3. OBTAIN on an asymmetric route.
        r = audit_arm("a3", "cc", tree,
                      _mk_calls(("Bash", '{"command":"cat /w/other/CLAUDE.md"}',
                                 "# rules\na rule the harness states verbatim\n")),
                      nd, "bare", pat)
        check("leg3 successful off-clone read is CONTAMINATED-OFFCLONE",
              r["verdict"], "CONTAMINATED-OFFCLONE")
        check("leg3 the obtain is counted", r["counts"]["obtain_asymmetric"], 1)

        # 4. GIT HISTORY is only blocking when it names an instruction ARTEFACT. A code-change
        #    task reading its own script out of history was reported CONTAMINATED by the first
        #    version of this audit; five runs were wrongly flagged that way.
        r = audit_arm("a4", "cc", tree,
                      _mk_calls(("Bash", '{"command":"git show HEAD:scripts/build.sh"}',
                                 "#!/bin/sh\nmake all\n")), nd, "bare", pat)
        check("leg4 git show of a SCRIPT is not contamination", r["verdict"], "CLEAN-ON-RECORD")
        check("leg4 the git-history call is still counted", r["counts"]["git_history_cmds"], 1)
        check("leg4 but not as naming an instruction artefact",
              r["counts"]["git_history_naming_instruction"], 0)
        r = audit_arm("a4b", "cc", tree,
                      _mk_calls(("Bash", '{"command":"git show HEAD~1:CLAUDE.md"}',
                                 "# rules\na rule the harness states verbatim\n")),
                      nd, "bare", pat)
        check("leg4b git show of CLAUDE.md IS contamination",
              r["verdict"], "CONTAMINATED-OFFCLONE")

        # 5. PROVENANCE BEATS NEEDLES. The most important leg: an asymmetric route whose result
        #    contains NO needle is still an obtain, because the route itself establishes it. A
        #    needle-gated rule scored a known-contaminated run as clean.
        r = audit_arm("a5", "cc", tree,
                      _mk_calls(("Bash", '{"command":"git show HEAD~1:CLAUDE.md > /tmp/c.md '
                                 '&& grep -n \\"branch\\" /tmp/c.md"}',
                                 "14: branch naming is issue-NNN-description\n"
                                 "88: branch off main, never commit to it\n")),
                      nd, "bare", pat)
        check("leg5 asymmetric obtain with ZERO needle hits is still contamination",
              r["verdict"], "CONTAMINATED-OFFCLONE")
        # Indexed defensively ON PURPOSE: under the mutation this leg exists to catch, `obtain`
        # is EMPTY, and a bare [0] turned a caught mutation into an IndexError traceback that
        # aborted legs 6-15. A self-test must stay legible precisely when it is failing.
        check("leg5 and it is recorded as provenance-evidenced",
              r["obtain"][0]["evidence"] if r["obtain"] else "NO-OBTAIN", "provenance")

        # 6. A CLIPPED FRAGMENT IS NOT AN ABSOLUTE PATH. A relative in-tree path was clipped at
        #    its first slash into something that did not start with the tree, and one real run
        #    was reported CONTAMINATED on exactly that.
        r = audit_arm("a6", "cc", tree,
                      _mk_calls(("Bash", '{"command":"cat knowledge/t/.claude/settings.json"}',
                                 '{"permissions":{}}')), nd, "bare", pat)
        check("leg6 clipped relative path is not an off-clone read",
              r["counts"]["reach_instruction_file"], 0)
        check("leg6 it is counted as a clipped fragment",
              r["counts"]["clipped_fragments"], 1)
        check("leg6 and the run stays clean", r["verdict"], "CLEAN-ON-RECORD")

        # 6b. AND IT IS NOT DOUBLE-COUNTED. An absolute off-clone path is matched whole by one
        #     detector and its tail re-matched by the other, so one read was reported twice --
        #     once in rI and once in clip. Asserted on both counts, since the verdict was right.
        r = audit_arm("a6b", "cc", tree,
                      _mk_calls(("Bash", '{"command":"cat /w/other/CLAUDE.md"}',
                                 "cat: /w/other/CLAUDE.md: No such file or directory")),
                      nd, "bare", pat)
        check("leg6b a resolved path is not ALSO an unresolvable fragment",
              r["counts"]["clipped_fragments"], 0)
        check("leg6b and it is still counted once as a reach",
              r["counts"]["reach_instruction_file"], 1)

        # 7. A REGEX ALTERNATION IS NOT A PATH. `.claude/projects` inside a grep PATTERN used to
        #    classify as a transcript-store access.
        check("leg7 an alternation is a pattern_string, not a transcript_store",
              classify_path("/claude\\b|\\.claude/projects|CLAUDE_CODE_", pat), "pattern_string")
        check("leg7 a real instruction file is still an instruction_file",
              classify_path("/w/other/CLAUDE.md", pat), "instruction_file")
        check("leg7 a globbed instruction file is NOT downgraded",
              classify_path("/other/*/CLAUDE.md", pat), "instruction_file")

        # 8. SYMMETRIC: a needle arriving from inside the run's OWN tree. Reported, not blocking.
        r = audit_arm("a8", "cc", tree,
                      _mk_calls(("Bash", '{"command":"grep -r rule /w/arm/Tasks"}',
                                 "Tasks/x.md: a rule the harness states verbatim")),
                      nd, "bare", pat)
        check("leg8 in-tree needle hit is symmetric", r["verdict"],
              "OBTAINED-VIA-SYMMETRIC-ROUTE")
        check("leg8 and is not counted as asymmetric", r["counts"]["obtain_asymmetric"], 0)

        # 9/10. ABSENT EVIDENCE IS NOT A CLEAN VERDICT, and neither is a run that never ran a
        #       tool -- which is a STRONGER statement and must not be tallied beside leg 1.
        check("leg9 no record is NOT-AUDITABLE",
              audit_arm("a9", "cc", tree, None, nd, "bare", pat)["verdict"], "NOT-AUDITABLE")
        check("leg10 zero tool executions is its own verdict",
              audit_arm("a10", "cc", tree, [], nd, "bare", pat)["verdict"], "NO-TOOL-EXECUTION")

        # 11. --instr-files IS REAL. An agent whose rules live in a file the default pattern does
        #     not name would read as clean everywhere; this is the parameter that fixes it, so it
        #     is asserted to CHANGE the verdict rather than merely to be accepted.
        calls = _mk_calls(("Bash", '{"command":"cat /w/other/MYRULES.md"}',
                           "a rule the harness states verbatim"))
        check("leg11 a custom rules file is invisible by default",
              audit_arm("a11", "cc", tree, calls, nd, "bare", pat)["verdict"],
              "OBTAINED-VIA-SYMMETRIC-ROUTE")
        check("leg11 and IS contamination once --instr-files names it",
              audit_arm("a11b", "cc", tree, calls, nd, "bare",
                        build_patterns(r"MYRULES\.md"))["verdict"], "CONTAMINATED-OFFCLONE")

        # 12. READERS pair args to result. A reader that silently fails to pair would make every
        #     run look like it obtained nothing.
        cc_t = os.path.join(d, "t.jsonl")
        with open(cc_t, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"message": {"content": [
                {"type": "tool_use", "id": "u1", "name": "Bash",
                 "input": {"command": "cat /w/other/CLAUDE.md"}}]}}) + "\n")
            fh.write(json.dumps({"message": {"content": [
                {"type": "tool_result", "tool_use_id": "u1",
                 "content": [{"type": "text", "text": "a rule the harness states verbatim"}]}]}}) + "\n")
        cc_calls = read_cc(cc_t)
        check("leg12 cc reader pairs result to call", len(cc_calls), 1)
        check("leg12 cc reader carries the result text",
              "verbatim" in (cc_calls[0].result or ""), True)

        dsh_t = os.path.join(d, "e.jsonl")
        with open(dsh_t, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"params": {"event": {"type": "tool/call", "data": {
                "callId": "k1", "name": "bash",
                "arguments": {"command": "cat /w/other/CLAUDE.md"}}}}}) + "\n")
            fh.write(json.dumps({"params": {"event": {"type": "tool/result", "data": {
                "toolCallId": "k1",
                "message": {"content": [{"type": "text", "text": "a rule the harness states "
                                                                 "verbatim"}]}}}}}) + "\n")
        dsh_calls = read_dsh(dsh_t)
        check("leg12 dsh reader pairs result to call", len(dsh_calls), 1)
        check("leg12 dsh reader carries the result text",
              "verbatim" in (dsh_calls[0].result or ""), True)

        # 13. CAPTURE DEDUPE. A later request repeats the whole history, so without dedupe a
        #     run's call count would scale with its request count rather than its work.
        cap = os.path.join(d, "c.jsonl")
        msg = [{"role": "assistant", "content": [
                    {"type": "tool_use", "id": "x1", "name": "Bash",
                     "input": {"command": "ls"}}]},
               {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "x1", "content": "a b c"}]}]
        with open(cap, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"body": {"messages": msg}}) + "\n")
            fh.write(json.dumps({"body": {"messages": msg}}) + "\n")
        check("leg13 a repeated tool_use counts once", len(read_capture(cap)), 1)

        # 14. HERMES PER-ELEMENT ARGS. One assistant row carrying two calls must not give both
        #     the whole row's text: a benign call then inherits a sibling's off-clone path and an
        #     obtain is credited to a route it did not come from.
        db = os.path.join(d, "state.db")
        con = sqlite3.connect(db)
        con.execute("create table sessions (id text primary key)")
        con.execute("create table messages (id integer primary key, session_id text, role text, "
                    "tool_name text, tool_calls text, tool_call_id text, content text)")
        con.execute("insert into sessions values ('s1')")
        con.execute("insert into messages (session_id, role, tool_calls) values ('s1','assistant',?)",
                    (json.dumps([
                        {"id": "h1", "function": {"name": "bash",
                                                  "arguments": '{"command":"find . -name x"}'}},
                        {"id": "h2", "function": {"name": "bash",
                                                  "arguments": '{"command":"cat /w/other/CLAUDE.md"}'}},
                    ]),))
        con.execute("insert into messages (session_id, role, tool_call_id, content) "
                    "values ('s1','tool','h1','./x')")
        con.execute("insert into messages (session_id, role, tool_call_id, content) "
                    "values ('s1','tool','h2','a rule the harness states verbatim')")
        con.commit()
        con.close()
        hc = {c.call_id: c for c in read_hermes(db, "s1")}
        check("leg14 hermes reader splits a two-call row", len(hc), 2)
        check("leg14 the benign call does NOT inherit the sibling's path",
              "CLAUDE.md" in hc["h1"].args, False)
        check("leg14 an absent hermes session is NO RECORD, not no calls",
              read_hermes(db, "nope"), None)

        # 15. REFUSALS are exit 2 and never look like a result.
        n_ok = os.path.join(d, "n.json")
        open(n_ok, "w", encoding="utf-8").write(json.dumps({"_comment": ["ignored"],
                                                            "rules": ["x"]}))
        check("leg15 a _comment key is documentation, not a needle", load_needles(n_ok), ["x"])
        n_empty = os.path.join(d, "e.json")
        open(n_empty, "w", encoding="utf-8").write("{\"_comment\": [\"only a comment\"]}")
        r_e = subprocess.run([sys.executable, me, "--needles", n_empty,
                              "--captures", d], capture_output=True, text=True)
        check("leg15 an empty needle set is refused with exit 2", r_e.returncode, 2)
        r_n = subprocess.run([sys.executable, me, "--needles", n_ok,
                              "--captures", os.path.join(d, "nope")],
                             capture_output=True, text=True)
        check("leg15 zero discovered runs is refused with exit 2", r_n.returncode, 2)
        check("leg15 a refusal says REFUSING", "REFUSING" in (r_n.stdout + r_n.stderr), True)
        r_b = subprocess.run([sys.executable, me, "--needles", os.path.join(d, "no.json"),
                              "--captures", d], capture_output=True, text=True)
        check("leg15 an unreadable needle file is refused with exit 2", r_b.returncode, 2)
        # And the end-to-end exit code must separate "audited, contaminated" from "never ran".
        cap2 = os.path.join(d, "runs", "cc_bare.jsonl")
        os.makedirs(os.path.dirname(cap2), exist_ok=True)
        with open(cap2, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"body": {"messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "z1",
                 "name": "Bash", "input": {"command": "cat /w/other/CLAUDE.md"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "z1",
                 "content": "a rule the harness states verbatim"}]}]}}) + "\n")
        n2 = os.path.join(d, "n2.json")
        open(n2, "w", encoding="utf-8").write(json.dumps(nd))
        r_c = subprocess.run([sys.executable, me, "--needles", n2, "--captures",
                              os.path.dirname(cap2), "--trees", "/w"],
                             capture_output=True, text=True)
        check("leg15 a contaminated run exits 1, not 2", r_c.returncode, 1)
        check("leg15 and names the verdict", "CONTAMINATED-OFFCLONE" in r_c.stdout, True)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print()
    if fails:
        print(f"SELF-TEST FAILED: {len(fails)} leg(s): {', '.join(fails)}")
        return 1
    print(f"SELF-TEST PASSED -- {len(ran)} checks.")
    print("What it does NOT cover, so its silence is not read as a pass: AUTO-LOADED injection.")
    print("No fixture here can establish whether a client that injects your rules into its")
    print("system prompt leaves any trace in its tool record -- it does not, which is why that")
    print("class is excluded from every verdict above rather than scored as clean.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--needles", help="JSON list of fixed strings, or an object of such lists")
    ap.add_argument("--arm", nargs="+", action="append", metavar="V",
                    help="NAME CLIENT RECORD TREE [STATE]; CLIENT is cc|dsh|hermes|capture. "
                         "A hermes RECORD is '<state.db>#<session id>'. Repeatable.")
    ap.add_argument("--captures", help="a directory of capture_proxy.py captures, one run each")
    ap.add_argument("--capture-label", help="regex a capture basename must match to be audited")
    ap.add_argument("--trees", default="",
                    help="parent of each run's own tree, for --captures: a path is off-clone "
                         "when it does not start with <trees>/<run>. Leaving this empty makes "
                         "EVERY absolute path off-clone, which over-reports rather than under-")
    ap.add_argument("--instr-files", default=DEFAULT_INSTR_FILE,
                    help="regex of instruction FILENAMES (default: %(default)s)")
    ap.add_argument("--instr-dir", default=DEFAULT_INSTR_DIR,
                    help="regex of the instruction DIRECTORY (default: %(default)s)")
    ap.add_argument("--json", help="write the full per-run record here")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this measures and what it cannot, then exit")
    a = ap.parse_args()

    if a.help_blind:
        print(__doc__.strip())
        return 0
    if a.self_test:
        return _selftest()
    if not a.needles:
        refuse("--needles is required (or use --self-test / --help-blind).")
    if not (a.arm or a.captures):
        refuse("nothing to audit. Give --captures DIR and/or --arm NAME CLIENT RECORD TREE.")

    pat = build_patterns(a.instr_files, a.instr_dir)
    needles = load_needles(a.needles)

    rows = []
    if a.captures:
        rows += discover_captures(a.captures, needles, a.capture_label, a.trees, pat)
    if a.arm:
        rows += discover_explicit(a.arm, needles, pat)
    if not rows:
        refuse("no runs discovered. An empty audit is not a clean audit.")

    print("PER-RUN SIDE-CHANNEL AUDIT -- did this run obtain instruction text it should not have?")
    print(f"needles: {len(needles)} fixed strings from {os.path.basename(a.needles)}")
    print(f"instruction files: {pat.instr_file}")
    print("NOT reported here, deliberately: whether the harness was AUTO-LOADED. No tool log can\n"
          "answer that in either direction; a behavioural probe can.\n")
    hdr = (f"{'run':26s} {'set':10s} {'calls':>6s} {'pair':>5s} {'rI':>3s} {'rT':>3s} "
           f"{'oASY':>5s} {'oSYM':>5s} {'gh':>3s} {'ghI':>4s} {'clip':>5s}  verdict")
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(rows, key=lambda x: (x["source_set"], x["arm"])):
        if r["verdict"] in ("NOT-AUDITABLE", "NO-TOOL-EXECUTION", "RECORD-NOT-FINAL"):
            print(f"{r['arm']:26s} {r['source_set'][:10]:10s} {'--':>6s} {'--':>5s} {'--':>3s} "
                  f"{'--':>3s} {'--':>5s} {'--':>5s} {'--':>3s} {'--':>4s} {'--':>5s}  "
                  f"{r['verdict']}" + (" (NO-RECORD)" if r["verdict"] == "NOT-AUDITABLE"
                                       else f" ({r.get('calls', 0)} tool calls)"))
            continue
        c = r["counts"]
        print(f"{r['arm']:26s} {r['source_set'][:10]:10s} {r['calls']:>6d} "
              f"{r['results_paired']:>5d} {c['reach_instruction_file']:>3d} "
              f"{c['reach_transcript_store']:>3d} {c['obtain_asymmetric']:>5d} "
              f"{c['obtain_symmetric']:>5d} {c['git_history_cmds']:>3d} "
              f"{c['git_history_naming_instruction']:>4d} {c['clipped_fragments']:>5d}  "
              f"{r['verdict']}")

    print("\ncolumns -- rI: off-clone reach naming an instruction FILE. rT: off-clone reach of a\n"
          "session transcript store. oASY: instruction text OBTAINED by an ASYMMETRIC route (git\n"
          "history of an instruction file, or an off-clone instruction file) -- the routes a strip\n"
          "is meant to close. oSYM: text obtained by a route the WITH run uses identically.\n"
          "gh: git-history calls; ghI: those naming an instruction artefact -- only these can\n"
          "contaminate. clip: path fragments whose absoluteness could not be resolved, counted\n"
          "and never blocking.")

    tally = collections.Counter(r["verdict"] for r in rows)
    print("\nVERDICTS")
    for k in ("CONTAMINATED-OFFCLONE", "REACHED-ASYMMETRIC-NO-RETURN",
              "OBTAINED-VIA-SYMMETRIC-ROUTE", "SYMMETRIC-REACH-NO-TEXT",
              "CLEAN-ON-RECORD", "NO-TOOL-EXECUTION", "RECORD-NOT-FINAL", "NOT-AUDITABLE"):
        print(f"  {tally.get(k, 0):4d}  {k}")

    bad = [r for r in rows if r["verdict"] == "CONTAMINATED-OFFCLONE"]
    interesting = bad + [r for r in rows if r["verdict"] == "REACHED-ASYMMETRIC-NO-RETURN"]
    if interesting:
        print("\nEVIDENCE for every run that reached or crossed an asymmetric route:")
        for r in interesting:
            print(f"\n  {'!!' if r['verdict'] == 'CONTAMINATED-OFFCLONE' else '~ '} {r['arm']} "
                  f"({r['source_set']}) -- {r['verdict']}")
            for g in r["git_history_cmds"]:
                if g["names_instruction_artefact"]:
                    print(f"       git-history call: {g['cmd'][:150]}")
            for x in r["reach_offclone"][:4]:
                if x["class"] == "instruction_file":
                    print(f"       off-clone read  : [{x['tool']}] {x['path'][:150]}")
            for o in r["obtain"][:4]:
                if o["route"] in ("git_history_instruction", "offclone_instruction_file"):
                    what = (f"{o['needles']}" if o["needles"] else
                            "instruction text (NO needle matched -- the route itself "
                            "establishes it)")
                    print(f"       OBTAINED {what} via {o['route']} "
                          f"({o['result_bytes']} B, evidence: {o['evidence']})\n"
                          f"         <- {o['args'][:140]}")

    sym = [r for r in rows if r["verdict"] == "OBTAINED-VIA-SYMMETRIC-ROUTE"]
    if sym:
        print(f"\nOBTAINED VIA A SYMMETRIC ROUTE ({len(sym)} run(s)) -- instruction text really\n"
              "did arrive, by a route the WITH run uses identically. This does not bias a\n"
              "with/without comparison; it narrows what a difference can be said to MEAN.")
        for r in sym:
            o = r["obtain"][0]
            print(f"    {r['arm']:26s} {o['needles']} ({o['route']}, {o['result_bytes']} B)")
            print(f"       <- {o['args'][:130]}")

    na = [r for r in rows if r["verdict"] == "NOT-AUDITABLE"]
    if na:
        print(f"\nNOT AUDITABLE ({len(na)} run(s)) -- no tool record was found. This is neither a\n"
              "pass nor a fail, and it is listed so it cannot be read as either.")
        for r in na:
            print(f"    {r['arm']}")

    if a.json:
        json.dump(rows, open(a.json, "w"), indent=2)
        print(f"\n-> {a.json}")
    # 1 when a run is contaminated in practice; 0 otherwise; 2 only when nothing could be
    # measured. NOT-AUDITABLE neither passes nor fails -- the count above is what a reader weighs.
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
