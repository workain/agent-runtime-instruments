#!/usr/bin/env python3
"""Engine adapters: how to recognise each agent engine's processes and transcripts.

This is the one file shared between instruments, and it exists so that adding an engine
is a single small edit rather than a change to three regexes in three files.

HONESTY RULE FOR THIS FILE, AND IT IS THE WHOLE POINT:
    `verified` is True **only** for an engine whose patterns were exercised against a
    real running process or a real transcript directory by someone who then recorded the
    output. Everything else is `verified=False` -- an **untested adapter**: a pattern we
    believe from reading documentation, not a measurement we made.

    Instruments that use this table PRINT which adapters are verified and which are
    not, every run. An untested adapter that silently matched nothing is
    indistinguishable from an engine that was not running, so the distinction has to be
    visible in the output and not only here.

    If you run an instrument against an engine marked `verified=False` and it works,
    that is the most useful contribution this repository can receive: flip the flag,
    paste the output into `samples/`, and send it. If it does NOT work, that is equally
    useful -- fix the pattern.

WHAT WOULD AN INSTRUMENT USING THIS PRINT IF IT WERE BLIND?
    Zero sessions, which is also the right answer on an idle box. The guard is
    `--list-unmatched` on `measure_session_footprint.py`: it prints the heaviest
    processes NO adapter claimed. If your engine is in that list, the zero is a defect
    in this file, not a measurement.
"""

# entrypoint : a regex matching the process that IS one session of this engine.
#              Keep these specific -- a generic `node` or `python` here would claim
#              every unrelated process on the box.
# transcripts: default on-disk location of per-session transcript files, '~' expanded.
#              None means "this engine's transcript location is unknown to us".
# verified   : see the honesty rule above. True == we ran it and kept the output.
ENGINES = [
    dict(
        name="claude-code",
        entrypoint=r"(^|/)claude(\.exe)?(\s|$)|claude-agent-sdk.*/claude(\s|$)",
        transcripts="~/.claude/projects",
        verified=True,
        note="measured: samples/measure_session_footprint.out, "
             "samples/measure_active_concurrency.out",
    ),
    dict(
        name="claude-code-acp",
        entrypoint=r"claude-agent-acp|npm exec @agentclientprotocol",
        transcripts="~/.claude/projects",
        verified=True,
        note="the Agent Client Protocol host wrapper; measured in "
             "samples/measure_session_footprint.out and "
             "samples/census_background_work.out",
    ),
    dict(
        name="codex",
        entrypoint=r"(^|/)codex(\s|$)",
        transcripts="~/.codex/sessions",
        verified=True,
        note="process + boot measured (samples/bench_cold_start.out). NOTE: that run "
             "was UNAUTHENTICATED (HTTP 401), so its boot figure is boot-to-first-event, "
             "and the transcript path has NOT been exercised with real sessions",
    ),
    dict(
        name="goose",
        entrypoint=r"(^|/)goosed?(\s|$)",
        transcripts="~/.local/share/goose/sessions",
        verified=False,
        note="UNTESTED ADAPTER. goose 2.0 moved to ACP-over-HTTP and stores sessions in "
             "SQLite, so the transcript path here is a guess and the per-session process "
             "model may not be one-process-per-session at all",
    ),
    dict(
        name="aider",
        entrypoint=r"(^|/)aider(\s|$)",
        transcripts=None,
        verified=False,
        note="UNTESTED ADAPTER. aider is per-invocation with no server, so 'resident "
             "session' may not be a meaningful unit for it",
    ),
    dict(
        name="opencode",
        entrypoint=r"(^|/)opencode(\s|$)",
        transcripts=None,
        verified=False,
        note="UNTESTED ADAPTER. opencode is genuinely client/server with state in one "
             "SQLite DB, so sessions are probably NOT separate processes and this "
             "entrypoint likely matches one server, not N sessions",
    ),
    dict(
        name="cursor-agent",
        entrypoint=r"cursor-agent(\s|$)",
        transcripts=None,
        verified=False,
        note="UNTESTED ADAPTER, pattern from documentation only",
    ),
]


def entrypoint_regex(include_unverified=True):
    """Alternation of every adapter's entrypoint pattern."""
    return "|".join(e["entrypoint"] for e in ENGINES
                    if include_unverified or e["verified"])


def transcript_roots(include_unverified=True):
    """Known transcript locations, '~' left unexpanded for the caller to expand."""
    return [e["transcripts"] for e in ENGINES
            if e["transcripts"] and (include_unverified or e["verified"])]


def provenance_banner():
    """One block naming which adapters are measured and which are guesses.

    Instruments print this so a zero can be read correctly: an untested adapter that
    matched nothing looks exactly like an engine that was not running.
    """
    ok = [e["name"] for e in ENGINES if e["verified"]]
    no = [e["name"] for e in ENGINES if not e["verified"]]
    lines = [f"engine adapters VERIFIED (run and output kept): {', '.join(ok) or 'none'}"]
    if no:
        lines.append(f"engine adapters UNTESTED (believed, not measured): {', '.join(no)}")
        lines.append("  -> for an UNTESTED engine, a zero here means 'this adapter found "
                     "nothing', which is NOT evidence the engine was absent. "
                     "See instruments/engines.py.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(provenance_banner())
    print()
    for e in ENGINES:
        print(f"{'[verified]' if e['verified'] else '[UNTESTED ]'} {e['name']}")
        print(f"    entrypoint : {e['entrypoint']}")
        print(f"    transcripts: {e['transcripts']}")
        print(f"    note       : {e['note']}")
