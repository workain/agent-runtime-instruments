#!/usr/bin/env python3
"""Drive ONE DeepSeek Harness (`dsh`) turn headlessly, over its SDK stdio JSON-RPC rail.

Why this file has to exist at all. Every other engine in `engines.py` can be driven from a
shell with one flag (`claude -p`, `codex exec`, `hermes -z`). `dsh` cannot: its SDK profile
is a LONG-LIVED stdio server that reads newline-delimited JSON-RPC 2.0 on stdin and exits
at EOF. Without something holding stdin open and speaking the protocol there is no way to
make it construct a single request, and therefore no way to VERIFY its adapter against a
real running process -- which is what `engines.py`'s honesty rule demands before
`verified=True`.

So this is the `echo_mcp_server.py` of the dsh adapter: a bundled driver, not an
instrument. It makes `dsh` reachable; `measure_request_construction.py` measures what it
then sends.

HARD-WON MECHANICS, each of which silently produces nothing if got wrong:

  * **Node >= 24.9.0 is required and a wrong one fails SILENTLY.** On Node 20 `dsh`
    produces zero output and exits 0 -- no error, no warning, nothing to grep for. This
    driver therefore REFUSES up front if it cannot find a new enough node, rather than
    launching one and reporting an empty capture.
  * **It is not `dsh-headless`.** That is a different, one-shot entrypoint; the SDK
    profile is `dsh --profile sdk-minimal`.
  * **stdin must stay open.** This driver owns it as a pipe, so no shell-level FIFO is
    needed.
  * **A `--patch` config REPLACES the whole entry it patches** (dsh's own
    `--dump-config-schema` says so), so a patch that sets only `baseURL` silently drops
    the API-key env name and the timeouts beside it. Pass a patch that restates the whole
    entry.
  * **Sessions PERSIST to disk, and the session id is a key.** Reusing one either fails
    outright (`session "<id>" already exists`) or, worse, carries the previous turn's
    history into this turn's measured payload -- because dsh sends its session log inside
    the request body. Use a fresh `--dsh-home` and a unique `--session-id` per run.

NO CREDENTIAL IS NEEDED and none should be supplied. Point `--patch` at
`capture_proxy.py`; it answers every request with a valid stub and never calls a provider.
dsh refuses to initialize a route with no credential at all, so a non-secret placeholder
is assembled at runtime -- it is never a real key and never committed.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    `{"verdict": "drove a turn", "notifications": 0}` -- which is exactly what a run that
    launched a dsh that did nothing would print, and exactly what a Node-20 silent no-op
    produces. That is the failure this file is shaped around:

      1. A node that is too old, or a missing `dsh` entrypoint, is refused BEFORE launch
         with exit 2 and `CANNOT DRIVE`. It is never a zero-notification success.
      2. Every server notification is written verbatim to `--events`, so "it ran" is an
         artefact a reader can count rather than a verdict this file asserts.
      3. A turn that produced NO notifications at all exits 3 and says
         `NO TURN OBSERVED`, because a protocol handshake that completes while the agent
         does nothing is not a driven turn.
      4. The thing a reader actually wants verified -- that requests were constructed --
         is not this file's claim to make. It is in the capture file, and
         `measure_request_construction.py` refuses an empty one.
"""
import argparse
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

MIN_NODE = (24, 9, 0)


def find_node():
    """-> (path, version_tuple, note) or (None, None, why-not).

    Looks at ARI_DSH_NODE, then PATH, then any node >= MIN_NODE reachable from the
    directories a self-contained runtime commonly unpacks into. Returns the REASON when
    nothing qualifies, because "no suitable node" and "node not consulted" must not read
    the same.
    """
    env = os.environ.get("ARI_DSH_NODE")
    if env and not os.path.isfile(env):
        # An EXPLICIT choice that is wrong is refused, not quietly replaced by whatever
        # is on PATH. Falling back would run dsh under an interpreter the operator did
        # not pick, and the whole hazard here is that the wrong node produces zero
        # output and exits 0 -- so "it ran, and found nothing" would be the symptom.
        return None, None, ("ARI_DSH_NODE=%s is not a file. Refusing rather than falling "
                            "back to the node on PATH: an explicitly chosen interpreter "
                            "that is wrong must not be silently replaced, because dsh on "
                            "the wrong node produces ZERO output and exits 0." % env)
    cands = []
    if env:
        cands.append(env)
    onpath = shutil.which("node")
    if onpath:
        cands.append(onpath)
    seen, rejected = set(), []
    for c in cands:
        if c in seen or not os.path.isfile(c):
            continue
        seen.add(c)
        try:
            out = subprocess.run([c, "--version"], capture_output=True, text=True,
                                 timeout=20).stdout.strip()
        except Exception as e:  # noqa: BLE001
            rejected.append("%s: %r" % (c, e))
            continue
        m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", out)
        if not m:
            rejected.append("%s: unparseable version %r" % (c, out))
            continue
        v = tuple(int(x) for x in m.groups())
        if v >= MIN_NODE:
            return c, v, out
        rejected.append("%s: %s is older than v%s" %
                        (c, out, ".".join(str(x) for x in MIN_NODE)))
    why = ("no node >= v%s found. Candidates tried: %s. Set ARI_DSH_NODE to a new enough "
           "node binary. This is refused rather than attempted because dsh on an older "
           "node produces ZERO output and exits 0."
           % (".".join(str(x) for x in MIN_NODE), "; ".join(rejected) or "none"))
    return None, None, why


def find_dsh():
    """-> (argv-prefix, note) or (None, why-not). ARI_DSH_BIN wins if set."""
    env = os.environ.get("ARI_DSH_BIN")
    if env:
        if os.path.isfile(env):
            return [env], env
        return None, "ARI_DSH_BIN=%s is not a file" % env
    onpath = shutil.which("dsh")
    if onpath:
        return [onpath], onpath
    return None, ("no dsh entrypoint: set ARI_DSH_BIN to the package's own bin.js (an "
                  "npm install of @deepseek-ai/dsh puts it under "
                  "node_modules/@deepseek-ai/dsh/lib/bin.js), or put `dsh` on PATH")


def drive(a):
    node, nver, nnote = find_node()
    if not node:
        print("CANNOT DRIVE: " + nnote, file=sys.stderr)
        return 2
    dsh_argv, dnote = find_dsh()
    if not dsh_argv:
        print("CANNOT DRIVE: " + dnote, file=sys.stderr)
        return 2
    if not os.path.isfile(a.prompt_file):
        print("CANNOT DRIVE: no prompt file at %s" % a.prompt_file, file=sys.stderr)
        return 2
    if not os.path.isfile(a.patch):
        print("CANNOT DRIVE: no --patch config at %s" % a.patch, file=sys.stderr)
        return 2
    prompt = open(a.prompt_file, encoding="utf-8").read()

    env = dict(os.environ)
    env["DSH_HOME"] = a.dsh_home
    os.makedirs(a.dsh_home, exist_ok=True)
    # Non-secret placeholder. dsh refuses to initialize a route with no credential at
    # all, and the capture proxy ignores auth entirely. Assembled at runtime so no
    # token-shaped literal is ever committed.
    env.setdefault("DEEPSEEK_API_KEY", "not-a-credential-%d" % os.getpid())

    # If the entrypoint is a .js file it needs the node we vetted; if it is an
    # executable shim it may pick its own node, so the version gate above would not
    # bind. Refuse the shim case rather than run it with an unknown interpreter.
    if dsh_argv[0].endswith(".js"):
        argv = [node] + dsh_argv
    else:
        print("CANNOT DRIVE: %s is a launcher shim, not a .js entrypoint, so the node "
              "it runs under is not the one vetted here (%s). dsh on an older node "
              "produces zero output and exits 0, so this is refused rather than "
              "attempted. Set ARI_DSH_BIN to the package's own lib/bin.js."
              % (dsh_argv[0], nnote), file=sys.stderr)
        return 2
    argv += ["--profile", a.profile, "--patch", a.patch]

    errf = open(a.stderr, "w", encoding="utf-8")
    evf = open(a.events, "w", encoding="utf-8")
    proc = subprocess.Popen(argv, cwd=a.cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=errf, text=True, bufsize=1)
    responses, notes = queue.Queue(), []

    def reader():
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    evf.write(json.dumps({"__unparseable__": line[:2000]}) + "\n")
                    evf.flush()
                    continue
                if "id" in m and "method" not in m:
                    responses.put(m)
                else:
                    notes.append(m)
                    evf.write(json.dumps(m, ensure_ascii=False) + "\n")
                    evf.flush()
        except Exception as e:  # noqa: BLE001
            evf.write(json.dumps({"__reader_error__": repr(e)}) + "\n")
            evf.flush()

    threading.Thread(target=reader, daemon=True).start()
    nid = [0]

    def call(method, params, timeout):
        nid[0] += 1
        rid = nid[0]
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                     "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return {"__dead__": proc.returncode}
            try:
                m = responses.get(timeout=1.0)
            except queue.Empty:
                continue
            if m.get("id") == rid:
                return m
        return {"__timeout__": timeout}

    t0 = time.time()
    out = {"node": nnote, "dsh": dnote, "profile": a.profile, "model": a.model,
           "provider": a.provider, "cwd": a.cwd, "dsh_home": a.dsh_home,
           "session_id": a.session_id}
    rc = 0
    try:
        init = call("initialize", {"cwd": a.cwd, "provider": a.provider,
                                   "model": a.model}, a.handshake_timeout)
        out["initialize"] = init
        if "result" not in init:
            out["verdict"] = "CANNOT DRIVE: initialize failed"
            rc = 2
        else:
            pr = call("session/prompt",
                      {"sessionId": a.session_id,
                       "contentBlocks": [{"type": "text", "text": prompt}]},
                      a.handshake_timeout)
            out["prompt"] = pr
            if "result" not in pr:
                out["verdict"] = "CANNOT DRIVE: session/prompt rejected"
                rc = 2
            else:
                out["wait"] = call("session/wait", {"sessionId": a.session_id}, a.timeout)
                if a.hold_seconds:
                    # Keep the server up after the turn so an outside instrument can
                    # observe a LIVE dsh process. engines.py's honesty rule needs the
                    # entrypoint pattern exercised against a real running process, and a
                    # dsh turn against a stub endpoint is over in about two seconds --
                    # far too fast to catch by hand. This is the only reason the flag
                    # exists; it changes nothing about the turn or the payload.
                    out["held_seconds"] = a.hold_seconds
                    time.sleep(a.hold_seconds)
    finally:
        for fn in (lambda: call("shutdown", {}, 20),
                   proc.stdin.close,
                   lambda: proc.wait(timeout=20)):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        if proc.poll() is None:
            proc.kill()
        evf.close()
        errf.close()

    out["elapsed_s"] = round(time.time() - t0, 1)
    out["notifications"] = len(notes)
    out["methods"] = sorted({m.get("method", "?") for m in notes})
    if rc == 0 and not notes:
        # The blind case, caught rather than reported as a success: a completed
        # handshake with no server notification at all is not a driven turn.
        out["verdict"] = ("NO TURN OBSERVED: the handshake completed but the server sent "
                          "zero notifications. This is NO MEASUREMENT, not a quiet turn.")
        rc = 3
    elif rc == 0:
        out["verdict"] = "drove a turn"
    print(json.dumps(out, indent=2))
    return rc


def self_test():
    """Known-answer controls for the DISCOVERY logic, which is where the traps are.

    Why this file needed one at all: scripts/preflight.sh runs `--self-test` for every
    instrument that offers the flag, picking them up automatically -- so a file without
    the flag is silently not covered, and a review found this was the only instrument in
    that position. It drives a real external process, which no gate here can require, so
    what IS testable is the set of refusals that stand between a reader and the failure
    this whole file exists for: dsh on an older node emits ZERO output and exits 0, which
    is indistinguishable from "it ran and the agent did nothing".

    WHAT THIS DOES NOT COVER, stated because the gap is the interesting part: no leg below
    starts a dsh server, completes a handshake, or exercises the notification reader or
    the exit-3 no-turn path. Those need a node >= MIN_NODE and the real package, neither
    of which is a dependency of this repository. The live evidence for them is
    samples/drive_dsh.live-redacted.out and the event log beside it,
    samples/drive_dsh.live-events-redacted.out -- an artefact a reader can count, which
    is the point: `"notifications": 16` in the summary is this file's own verdict about
    itself, and the event log is what makes it checkable.
    """
    import contextlib
    import tempfile
    passed, failed = [], []

    def ok(label):
        passed.append(label)
        print("  PASS  " + label)

    def no(label):
        failed.append(label)
        print("  FAIL  " + label)

    def drive_quietly(args):
        """-> (exit_code, stderr_text). Captured rather than printed, for two reasons:
        the refusal text is what these legs are really asserting, and letting it through
        would scatter CANNOT DRIVE lines through scripts/preflight.sh's output where they
        read as failures."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = drive(args)
        return rc, buf.getvalue()

    tmp = tempfile.mkdtemp(prefix="ari-drive-dsh-selftest-")
    saved = {k: os.environ.get(k) for k in ("ARI_DSH_NODE", "ARI_DSH_BIN")}

    def fake_node(name, version_line):
        """A `node` whose only job is to answer --version. Discovery reads nothing else."""
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho '%s'\n" % version_line)
        os.chmod(path, 0o755)
        return path

    try:
        print("== 1. an EXPLICIT node that is wrong is refused, never replaced ==")
        os.environ["ARI_DSH_NODE"] = os.path.join(tmp, "does-not-exist")
        n, v, why = find_node()
        if n is None and why and "Refusing rather than falling back" in why:
            ok("ARI_DSH_NODE naming a missing file is refused, and the reason says it "
               "did NOT fall back to the node on PATH (a silent fallback would run dsh "
               "under an interpreter nobody chose, and the symptom is an empty result)")
        else:
            no("missing ARI_DSH_NODE -> node=%r why=%r" % (n, why))

        print("== 2. the version gate: too old is refused, new enough is accepted ==")
        os.environ["ARI_DSH_NODE"] = fake_node("node-old", "v20.19.0")
        n, v, why = find_node()
        if n is None and why and "older than v24.9.0" in why:
            ok("a node older than MIN_NODE is refused and its VERSION is named -- this "
               "is the whole hazard: on v20 dsh writes nothing and exits 0")
        else:
            no("v20.19.0 -> node=%r v=%r why=%r" % (n, v, why))
        os.environ["ARI_DSH_NODE"] = fake_node("node-new", "v24.21.0")
        n, v, why = find_node()
        if n is not None and v == (24, 21, 0):
            ok("a node at or above MIN_NODE is accepted, with its version parsed "
               "(%s) -- so the gate above is a gate and not a refusal of everything"
               % ".".join(str(x) for x in v))
        else:
            no("v24.21.0 -> node=%r v=%r why=%r" % (n, v, why))
        # Exactly MIN_NODE must pass: a `>` where `>=` belongs would refuse the only
        # version the vendor's own floor names.
        os.environ["ARI_DSH_NODE"] = fake_node("node-floor", "v24.9.0")
        n, v, why = find_node()
        if n is not None and v == MIN_NODE:
            ok("the FLOOR version itself (v24.9.0) is accepted, not refused by an "
               "off-by-one comparison")
        else:
            no("the floor version was refused: node=%r why=%r" % (n, why))

        print("== 3. an unreadable version is refused, not assumed ==")
        os.environ["ARI_DSH_NODE"] = fake_node("node-garbage", "banana")
        n, v, why = find_node()
        if n is None and why and "unparseable version" in why:
            ok("a node whose --version cannot be parsed is refused and the output is "
               "quoted (assuming it new enough is how the zero-output failure arrives)")
        else:
            no("unparseable version -> node=%r why=%r" % (n, why))

        print("== 4. the entrypoint: a shim is refused because the node gate would not "
              "bind ==")
        os.environ["ARI_DSH_NODE"] = fake_node("node-ok", "v24.21.0")
        os.environ["ARI_DSH_BIN"] = os.path.join(tmp, "no-such-dsh")
        argvp, why = find_dsh()
        if argvp is None and why and "is not a file" in why:
            ok("ARI_DSH_BIN naming a missing file is refused with a reason")
        else:
            no("missing ARI_DSH_BIN -> %r / %r" % (argvp, why))
        shim = os.path.join(tmp, "dsh-shim")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexec node \"$@\"\n")
        os.chmod(shim, 0o755)
        os.environ["ARI_DSH_BIN"] = shim
        args = argparse.Namespace(
            prompt_file=os.path.join(tmp, "p.txt"), patch=os.path.join(tmp, "patch.yml"),
            cwd=tmp, events=os.path.join(tmp, "ev.jsonl"),
            stderr=os.path.join(tmp, "err.txt"), dsh_home=os.path.join(tmp, "home"),
            session_id="s", profile="sdk-minimal", model="m", provider="p",
            timeout=5, handshake_timeout=5, hold_seconds=0)
        open(args.prompt_file, "w").write("hi")
        open(args.patch, "w").write("llm: {}\n")
        rc, err = drive_quietly(args)
        if rc == 2 and "is a launcher shim" in err:
            ok("an executable SHIM entrypoint is refused (exit 2) and the reason says "
               "WHY -- it picks its own node, so the version gate above decides nothing")
        else:
            no("a shim entrypoint was driven anyway: exit %r, stderr %r" % (rc, err))

        print("== 5. a missing prompt or patch file is refused before anything starts ==")
        os.environ["ARI_DSH_BIN"] = os.path.join(tmp, "fake-dsh.js")
        open(os.environ["ARI_DSH_BIN"], "w").write("// not run\n")
        for field, label in (("prompt_file", "prompt"), ("patch", "--patch config")):
            bad = argparse.Namespace(**vars(args))
            setattr(bad, field, os.path.join(tmp, "absent"))
            rc, err = drive_quietly(bad)
            if rc == 2 and "CANNOT DRIVE" in err:
                ok("a missing %s is refused (exit 2) before a dsh process is started, "
                   "and the refusal names the path it looked for" % label)
            else:
                no("a missing %s did not refuse: exit %r, stderr %r" % (label, rc, err))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("self-test: %d pass, %d fail" % (len(passed), len(failed)))
    print("NOT covered here, and deliberately: the handshake, the notification reader, "
          "and the exit-3 no-turn path, all of which need a real node >= %s and the real "
          "package. See this function's docstring and "
          "samples/drive_dsh.live-events-redacted.out."
          % ".".join(str(x) for x in MIN_NODE))
    if failed:
        print("SELF-TEST FAILED -- do NOT trust this file to refuse a bad node.")
        return 1
    return 0


def main(argv):
    if "--help-blind" in argv:
        print(__doc__)
        return 0
    if "--self-test" in argv:
        return self_test()
    ap = argparse.ArgumentParser(
        description="Drive one dsh turn over its SDK stdio JSON-RPC rail.",
        epilog="ARI_DSH_NODE / ARI_DSH_BIN override node and entrypoint discovery.")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--cwd", required=True,
                    help="working directory the session is rooted in")
    ap.add_argument("--patch", required=True,
                    help="dsh --patch config; must restate the whole llm entry")
    ap.add_argument("--events", required=True, help="JSONL of every server notification")
    ap.add_argument("--stderr", required=True)
    ap.add_argument("--dsh-home", required=True,
                    help="a FRESH directory; sessions persist, and a reused one leaks "
                         "the previous turn's history into this turn's payload")
    ap.add_argument("--session-id", default="ari-dsh-1")
    ap.add_argument("--profile", default="sdk-minimal")
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--provider", default="deepseek-official")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--handshake-timeout", type=int, default=120)
    ap.add_argument("--hold-seconds", type=int, default=0,
                    help="keep the dsh server alive this long AFTER the turn, so an "
                         "outside instrument can observe a live dsh process")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this would say if it measured nothing")
    ap.add_argument("--self-test", action="store_true",
                    help="run the known-answer discovery controls and exit non-zero if "
                         "any fails")
    return drive(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
