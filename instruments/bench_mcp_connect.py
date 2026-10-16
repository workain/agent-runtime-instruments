#!/usr/bin/env python3
"""Time to a USABLE MCP tool -- the figure `system/init` hides.

`bench_cold_start.py`'s `boot` arm stops at `system/init`, where MCP servers are still
listed `pending`; its `turn` arm runs to `result` but on a prompt that calls no tool, so
the MCP connect may never happen inside that figure either. Neither bounds what a
turn-scoped worker actually pays when the turn uses tools.

This measures to the first `tool_result` for a named MCP tool: connect + handshake +
dispatch + one real call. Spends one cheap model request per rep.

Three clocks are printed per rep, and the gaps between them are the point:
    init            -- first stream event; MCP servers `pending`
    mcp_tool_use    -- the model has decided to call the tool
    mcp_tool_result -- the tool has answered; only now is the server USABLE

Runs out of the box against the bundled `echo_mcp_server.py` (one tool, no work), which
measures the protocol floor. Point MCP_TOOL/MCP_CONFIG at a real server to measure what
that server costs -- that is the normal use, and the two numbers are not interchangeable.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    `NEVER-CALLED`, explicitly, when the run reaches `result` without the tool ever
    being invoked. That case is the whole reason this instrument has a distinct sentinel
    instead of printing `none`: a model that declines to call the tool and a server that
    failed to connect both produce no `tool_result`, and they are different findings.
    Distinguish them by the `init` line, which the run prints with each server's status
    and any `mcp_server_errors`.

    `none` in all three columns means no stream event was recognised at all -- the CLI
    did not start, or did not speak stream-json.

    Two ways to get a NUMBER that is wrong:

      1. Measuring the bundled echo server and reporting it as a real server's cost.
         The echo server does nothing; a real server's connect may dominate. The run
         labels which config it used, every time.
      2. Treating n=2 on a loaded box as a constant. It is not. Where this was first
         run, the same arm read 3.53 s and 3.74 s, while a separately-measured boot arm
         moved 1.16 s -> 3.16 s minutes apart on a box at load ~8. Report the spread.

    Version note, because it changes what the number MEANS: Claude Code's documented
    behaviour of waiting for still-pending MCP servers before the first turn (bounded by
    `MCP_TIMEOUT`, 30 s by default) is documented as requiring **v2.1.221 or later**.
    On an older CLI there is no such wait, and the connect cost lands on the first tool
    call -- which is exactly what this instrument measures. Print your CLI version
    alongside the figure; the run does it for you.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TOOL = "mcp__echo__ping"


def cli_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def main():
    ap = argparse.ArgumentParser(
        description="Time from process start to a usable MCP tool (first tool_result).",
        epilog=f"Env: MCP_TOOL (default {DEFAULT_TOOL!r}), MCP_CONFIG (path to an "
               f"--mcp-config JSON; default: a temp config for the bundled echo server), "
               f"REPS (2), RUN_TIMEOUT (120). Run --help-blind for failure modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anonymise", action="store_true",
                    help="redact the names of MCP servers other than the one under test "
                         "(for published output; their STATUS is kept, since that is "
                         "what explains the timing)")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args()
    if args.help_blind:
        print(__doc__)
        return 0

    if not shutil.which("claude"):
        print("SKIPPED: 'claude' is not on PATH. This arm did not run -- not a zero.",
              file=sys.stderr)
        return 2

    reps = int(os.environ.get("REPS", "2"))
    cap = float(os.environ.get("RUN_TIMEOUT", "120"))
    tool = os.environ.get("MCP_TOOL", DEFAULT_TOOL)
    cfg = os.environ.get("MCP_CONFIG", "")

    tmp = None
    if not cfg:
        # self-contained default: the bundled echo server, so this is runnable by anyone
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"mcpServers": {"echo": {
            "command": sys.executable,
            "args": [os.path.join(HERE, "echo_mcp_server.py")]}}}, tmp)
        tmp.close()
        cfg = tmp.name
        which = f"bundled echo_mcp_server.py (protocol floor, does no work)"
    else:
        which = f"MCP_CONFIG={cfg}"

    # Claude Code names MCP tools `mcp__<server>__<tool>`; we need <server> to read the
    # target server's own status out of `system/init`.
    server_name = tool[len("mcp__"):].rsplit("__", 1)[0] if tool.startswith("mcp__") else ""

    print(f"CLI: {cli_version()}")
    print(f"tool: {tool}")
    print(f"config: {which}")
    print("(the documented pre-turn wait for pending MCP servers requires CLI v2.1.221+; "
          "on an older CLI the connect cost lands on the first tool call)")

    argv = ["claude", "-p", f"Call the {tool} tool exactly once, then reply only: done.",
            "--output-format", "stream-json", "--verbose", "--max-turns", "3",
            "--mcp-config", cfg, "--allowedTools", tool]

    results = []
    for i in range(reps):
        t0 = time.monotonic()
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1)
        t_init = t_use = t_res = None
        init_servers = ""
        server_status = None
        use_id = None
        res_is_error = None
        deadline = t0 + cap
        for line in p.stdout:
            if time.monotonic() > deadline:
                break
            try:
                d = json.loads(line)
            except Exception:
                continue
            kind = d.get("type")

            if t_init is None and kind == "system" and d.get("subtype") == "init":
                t_init = time.monotonic() - t0
                servers = d.get("mcp_servers") or []
                shown = servers
                if args.anonymise:
                    shown = [(sv if sv.get("name") == server_name
                              else {**sv, "name": f"<other-server-{i+1}>"})
                             for i, sv in enumerate(servers)]
                init_servers = json.dumps(shown)
                errs = d.get("mcp_server_errors")
                if errs:
                    init_servers += f"  errors={json.dumps(errs)}"
                for sv in servers:
                    if sv.get("name") == server_name:
                        server_status = sv.get("status")

            # Match the tool STRUCTURALLY, by block type and exact name -- never by
            # substring over the whole JSON line. Measured defect: the model called
            # `ToolSearch` with `query: "select:mcp__echo__ping"`, so the target tool's
            # NAME appeared inside ANOTHER tool's arguments. A substring match timed that
            # failed search and reported it as "time to a usable MCP tool", with the
            # target server showing `"status": "failed"` in the same output.
            for blk in ((d.get("message") or {}).get("content") or []):
                if not isinstance(blk, dict):
                    continue
                if (t_use is None and blk.get("type") == "tool_use"
                        and blk.get("name") == tool):
                    t_use = time.monotonic() - t0
                    use_id = blk.get("id")
                elif (blk.get("type") == "tool_result" and use_id is not None
                        and blk.get("tool_use_id") == use_id):
                    t_res = time.monotonic() - t0
                    res_is_error = bool(blk.get("is_error"))
            if t_res is not None:
                break
            if kind == "result":
                # reached the end without the tool ever being called -- a DIFFERENT
                # finding from a failed connect, so it gets its own sentinel
                t_res = -1.0
                break
        p.kill()
        fmt = lambda v: ("none" if v is None else
                         ("NEVER-CALLED" if v == -1.0 else f"{v:.2f}s"))
        print(f"rep{i+1}: init={fmt(t_init)}  mcp_tool_use={fmt(t_use)}  "
              f"mcp_tool_result={fmt(t_res)}")
        if init_servers:
            print(f"       init mcp_servers: {init_servers}")
        err = (p.stderr.read() or "").strip().replace("\n", " ")[:200]
        if err:
            print(f"       stderr: {err}")
        if server_status == "failed":
            print(f"       REFUSING this rep: server {server_name!r} reported "
                  f'"status": "failed" in init -- nothing was connected, so any time '
                  f"below would be timing something else.")
        elif res_is_error:
            print(f"       REFUSING this rep: the tool answered with is_error=true. "
                  f"An error IS a tool_result, so timing it would report a working "
                  f"server that is not working.")
        elif t_res is not None and t_res > 0:
            results.append(t_res)
        time.sleep(1)

    if tmp:
        os.unlink(tmp.name)

    if results:
        print(f"\ntime-to-usable-MCP-tool: n={len(results)}/{reps}  "
              f"min={min(results):.2f}s  max={max(results):.2f}s  "
              f"spread={max(results)-min(results):.2f}s")
        print("Report the spread, not just a midpoint: n is small and the box's load is "
              "a variable this instrument does not control.")
    else:
        print(f"\nNO REP PRODUCED A tool_result for {tool}. See --help-blind: check the "
              f"init mcp_servers line above to tell a failed connect from a model that "
              f"simply declined to call the tool.")
        return 3     # a run that measured nothing must not exit 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
