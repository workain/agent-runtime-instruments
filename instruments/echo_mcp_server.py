#!/usr/bin/env python3
"""A minimal stdio MCP server with one tool, so `bench_mcp_connect.py` is runnable by a
stranger with no access to our fleet's MCP servers.

Deliberately the smallest thing that satisfies the protocol: `initialize`, `tools/list`,
`tools/call` for one tool named `ping`. Under Claude Code's naming that tool is callable
as `mcp__echo__ping` when this server is registered as `echo`.

It does NO work, which is the point: the time `bench_mcp_connect.py` measures should be
connect + handshake + dispatch, not this server's own latency. If you want to measure a
REAL server's cost, point MCP_TOOL at it instead -- that is the normal use.

Speaks JSON-RPC 2.0 over newline-delimited stdin/stdout. No network, no filesystem
writes.
"""
import json
import sys

# Echo back whatever version the client asked for, falling back to a known-real one.
# An INVENTED version string here is rejected by the client and the server shows up as
# `"status": "failed"` in `system/init` -- measured, see docs/method.md.
FALLBACK_PROTOCOL_VERSION = "2025-06-18"


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, rid = req.get("method"), req.get("id")

        # Notifications carry no id and must get NO response.
        if rid is None:
            continue

        if method == "initialize":
            asked = ((req.get("params") or {}).get("protocolVersion")
                     or FALLBACK_PROTOCOL_VERSION)
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": asked,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "1.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
                "name": "ping",
                "description": "Returns the string 'pong'. Takes no arguments.",
                "inputSchema": {"type": "object", "properties": {}}}]}})
        elif method == "tools/call":
            name = (req.get("params") or {}).get("name")
            if name == "ping":
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": "pong"}]}})
            else:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32602, "message": f"unknown tool: {name}"}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
