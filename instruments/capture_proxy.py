#!/usr/bin/env python3
"""A loopback HTTP endpoint that RECORDS what an agent client sends, and bills nothing.

This is to `measure_request_construction.py` what `echo_mcp_server.py` is to
`bench_mcp_connect.py`: the helper process the instrument needs in order to have anything
to measure, bundled so the instrument runs out of the box rather than against somebody's
private endpoint.

It speaks the two request shapes coding agents actually use, on one port, so both arms of
a comparison hit one instrument:

    POST /v1/messages          -- Anthropic Messages      (Claude Code, DeepSeek Harness)
    POST /v1/chat/completions  -- OpenAI chat completions  (Hermes's custom-provider rail)

Every request body is appended VERBATIM to a JSONL capture file, tagged with an arm label
and the endpoint it arrived on. A minimal valid response goes back so the client ends its
turn cleanly instead of retrying -- a retry would append the same payload twice and
double-count it.

**No upstream request is ever made.** A capture costs $0.00 by construction and not by
discipline: there is no code path here that talks to a provider, so there is nothing to
misconfigure. The flip side is the limit: the model never makes progress, so nothing that
needs real model output -- compaction, context growth across turns, tool-use loops -- is
measurable this way. What IS measurable is exactly the first-request construction, which
is the fixed cost every turn pays.

HEADER VALUES ARE NEVER RECORDED, only header NAMES. The `Authorization` /
`x-api-key` header of a misconfigured run would otherwise land in a capture file that is
meant to be publishable.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    Nothing, and that is the whole danger: a recorder that binds its port, answers every
    request with a valid stub, and writes an EMPTY capture file is indistinguishable from
    a client that was never pointed at it. Both leave a 0-line file and exit 0.

    Three things separate the two cases here, and all three are load-bearing:
      1. `--self-test` drives this server over real loopback HTTP with both request
         shapes and asserts the capture file contains them, so "the recorder records" is
         a checked claim rather than an assumption. It exits non-zero if not.
      2. The capture file is created at startup and the startup line names it, so a
         missing file means the recorder never started -- a different fault from an
         empty one.
      3. The instrument that READS the capture refuses a 0-request file with exit 2 and
         the word NO-MEASUREMENT. An empty capture can therefore never reach a figure.

    What it still cannot tell you: whether the client you launched sent its requests
    somewhere ELSE. Only a non-empty capture proves it came here.
"""
import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Env rather than argv so the launcher that starts this in the background and the arm
# runner that reads the file afterwards cannot disagree about the path.
CAPTURE = os.environ.get("ARI_CAPTURE", "/tmp/ari-capture/capture.jsonl")
ARM = os.environ.get("ARI_ARM", "unlabelled")
REPLY = "Acknowledged."


UNREAD = "__unread__"


def record(capture, arm, endpoint, headers, raw, unread=None):
    """Append one captured request as a single JSONL line.

    `raw_bytes` is the exact serialized length of what arrived on the wire -- the one
    figure in the whole chain that involves no tokeniser, no estimate and no assumption.

    `headers` is the whole header mapping and only its KEYS are kept. That is the one
    line in this file whose mistake is irreversible: a capture is meant to be
    publishable, and `.items()` here instead of `.keys()` would put the Authorization /
    x-api-key value of a misconfigured run into it.
    """
    if unread is not None:
        # The body was never read, so its length is UNKNOWN -- not zero. `raw_bytes` is
        # null rather than 0 for exactly that reason: 0 is an observation, and this is
        # the absence of one. The measuring instrument refuses the capture by name.
        return _write(capture, {
            "arm": arm, "endpoint": endpoint, "ts": time.time(),
            "raw_bytes": None, "header_names": sorted(headers.keys()),
            "body": {UNREAD: unread},
        })
    if not raw:
        # NOTHING SENT is not the same as COULD NOT PARSE, and conflating them cost a
        # whole capture. A `Content-Length: 0` POST -- a health check, a probe -- used to
        # be stored under the unparseable marker, which the measuring instrument refuses
        # the entire capture over. The refusal's own evidence then read "0 raw bytes
        # between them", i.e. it refused a good capture over nothing at all, and the only
        # remedy was to capture again. A body of None reads as a request that carried no
        # payload, which is what happened.
        body = None
    else:
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            # An unparseable body is recorded as such rather than dropped: a client
            # sending something this does not understand is a finding, and a silently
            # skipped request would show up as a smaller payload instead.
            body = {"__unparseable__": raw.decode("utf-8", "replace")[:2000]}
    return _write(capture, {
        "arm": arm,
        "endpoint": endpoint,
        "ts": time.time(),
        "raw_bytes": len(raw),
        "header_names": sorted(headers.keys()),
        "body": body,
    })


def _write(capture, rec):
    """Append one record. `ensure_ascii=False` is load-bearing: see record()'s own note."""
    with open(capture, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
    return rec


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    capture = CAPTURE
    arm = ARM

    def log_message(self, *a):  # the capture file is the record; keep stderr clean
        pass

    def _send(self, payload, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _sse(self, chunks):
        # Close-delimited rather than keep-alive. An event stream carries no
        # Content-Length, so under keep-alive the client has no way to know the body
        # ended and blocks until its own timeout -- which is exactly what this file's
        # own --self-test hit on the streaming leg, and a hang in a recorder reads as a
        # client that never sent anything. Real SSE consumers handle either framing;
        # only one of them lets the recorder prove it answered.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for c in chunks:
            self.wfile.write(c.encode())
            self.wfile.flush()
        self.close_connection = True

    def _read_chunked(self):
        """-> (raw, unread_reason). The de-chunked body, or why it was not read."""
        out = []
        while True:
            line = self.rfile.readline(65537)
            if not line:
                return b"".join(out), ("a chunked body that ended mid-stream after "
                                       "%d decoded bytes" % sum(len(c) for c in out))
            field = line.split(b";")[0].strip()
            if not field:
                continue
            try:
                size = int(field, 16)
            except ValueError:
                return b"".join(out), ("a chunked body whose chunk size %r is not "
                                       "hexadecimal" % field[:40])
            if size == 0:
                while True:                       # trailers, then the terminating blank
                    t = self.rfile.readline(65537)
                    if t in (b"\r\n", b"\n", b""):
                        break
                return b"".join(out), None
            out.append(self.rfile.read(size))
            self.rfile.read(2)                    # the CRLF that follows each chunk

    def _read_body(self):
        """-> (raw, unread_reason). `unread_reason` is None when the bytes were read.

        A chunked POST is DE-CHUNKED rather than skipped: framing every request by
        `int(Content-Length or 0)` read zero bytes from one, which then became a startup
        probe downstream -- 40391 real bytes reported as `1 measurable, 1 startup
        probes` at exit 0 in a review's demonstration.
        """
        te = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if te:
            if "chunked" in te:
                return self._read_chunked()
            return b"", ("`Transfer-Encoding: %s`, which this recorder cannot decode"
                         % te[:40])
        cl = self.headers.get("Content-Length")
        if cl is None:
            # RFC 7230 s3.3.3: no Content-Length and no Transfer-Encoding is a request
            # with NO BODY. That is nothing sent, which record() already has a state for
            # -- not a body of unknown length.
            return b"", None
        try:
            n = int(cl)
        except ValueError:
            return b"", ("a `Content-Length` of %r, which is not a byte count" % cl[:40])
        if n < 0:
            return b"", "a negative `Content-Length` (%d)" % n
        return (self.rfile.read(n) if n else b""), None

    def do_POST(self):
        raw, unread = self._read_body()
        path = self.path.split("?")[0]
        record(self.capture, self.arm, path, dict(self.headers), raw, unread)
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            body = {}
        stream = bool(body.get("stream"))
        model = body.get("model") or "capture-proxy-stub"
        if "chat/completions" in path:
            return self._openai(stream, model)
        if "messages" in path or "count_tokens" in path:
            return self._anthropic(stream, model, path)
        return self._send(b'{"ok":true}')

    def _anthropic(self, stream, model, path):
        if "count_tokens" in path:
            return self._send(json.dumps({"input_tokens": 1}).encode())
        msg = {
            "id": "msg_capture_proxy", "type": "message", "role": "assistant",
            "model": model, "content": [{"type": "text", "text": REPLY}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }
        if not stream:
            return self._send(json.dumps(msg).encode())

        def ev(name, data):
            return "event: %s\ndata: %s\n\n" % (name, json.dumps(data))
        start = dict(msg, content=[], stop_reason=None)
        self._sse([
            ev("message_start", {"type": "message_start", "message": start}),
            ev("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}}),
            ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "text_delta", "text": REPLY}}),
            ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ev("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": "end_turn",
                                           "stop_sequence": None},
                                 "usage": {"output_tokens": 1}}),
            ev("message_stop", {"type": "message_stop"}),
        ])

    def _openai(self, stream, model):
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if not stream:
            return self._send(json.dumps({
                "id": "chatcmpl-capture-proxy", "object": "chat.completion",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": REPLY},
                             "finish_reason": "stop"}],
                "usage": usage,
            }).encode())
        base = {"id": "chatcmpl-capture-proxy", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model}
        self._sse([
            "data: " + json.dumps(dict(base, choices=[
                {"index": 0, "delta": {"role": "assistant", "content": REPLY},
                 "finish_reason": None}])) + "\n\n",
            "data: " + json.dumps(dict(base, choices=[
                {"index": 0, "delta": {}, "finish_reason": "stop"}], usage=usage)) + "\n\n",
            "data: [DONE]\n\n",
        ])

    def do_GET(self):
        # Model-list / health probes some clients make before their first real turn.
        # These land in the capture too, which is why the instrument that reads it has to
        # be able to tell a probe from a measurable request rather than counting lines.
        self._send(json.dumps({"object": "list", "data": [
            {"id": "capture-proxy-stub", "object": "model"}]}).encode())


def serve(port, capture, arm):
    os.makedirs(os.path.dirname(capture) or ".", exist_ok=True)
    # Create it NOW, not on first request: a missing file then means "the recorder never
    # started", which is a different fault from "the client never arrived".
    open(capture, "a", encoding="utf-8").close()
    Handler.capture, Handler.arm = capture, arm
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return srv


def self_test():
    """Drive this server over real loopback HTTP and assert it recorded what was sent.

    A recorder is the one component whose failure mode is a clean-looking empty file, so
    "it records" has to be a checked claim. Both request shapes, both response modes.
    """
    import socket
    import tempfile
    import threading

    checks, fails = [], []

    def ok(label):
        checks.append(label)
        print("  PASS  " + label)

    def no(label):
        fails.append(label)
        print("  FAIL  " + label)

    tmp = tempfile.mkdtemp(prefix="ari-capture-selftest-")
    cap = os.path.join(tmp, "capture.jsonl")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = serve(port, cap, "selftest-arm")
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if os.path.exists(cap) and os.path.getsize(cap) == 0:
        ok("capture file exists and is EMPTY before any request "
           "(a missing file would mean the recorder never started)")
    else:
        no("capture file not created empty at startup")

    def post_bytes(path, data):
        """Send EXACT bytes, so a leg can know the wire length independently."""
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path), data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer not-a-credential"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()

    def post(path, payload):
        return post_bytes(path, json.dumps(payload).encode())

    anthropic_body = {"model": "m", "system": "SYS", "tools": [{"name": "t"}],
                      "messages": [{"role": "user", "content": "hello"}], "stream": False}
    openai_body = {"model": "m", "tools": [{"type": "function"}],
                   "messages": [{"role": "system", "content": "SYS"},
                                {"role": "user", "content": "hello"}], "stream": False}
    r1 = post("/v1/messages", anthropic_body)
    r2 = post("/v1/chat/completions", openai_body)
    try:
        r3 = post("/v1/messages", dict(anthropic_body, stream=True))
    except Exception as e:  # noqa: BLE001
        # A stub that never terminates its own event stream HANGS the client, and a hung
        # recorder reads as a client that never sent anything. Caught rather than left to
        # raise, so the failure arrives as a named FAIL line instead of a traceback.
        r3 = b"<the streaming reply never terminated: %r>" % (e,)

    if json.loads(r1).get("type") == "message":
        ok("Anthropic /v1/messages answered with a valid message object")
    else:
        no("Anthropic reply is not a message object")
    if json.loads(r2).get("object") == "chat.completion":
        ok("OpenAI /v1/chat/completions answered with a valid completion object")
    else:
        no("OpenAI reply is not a completion object")
    if b"event: message_stop" in r3:
        ok("the STREAMING reply terminates -- a stub that never ends its own event "
           "stream hangs the client and reads as a client that never arrived")
    else:
        no("streaming reply did not terminate with message_stop")

    recs = [json.loads(l) for l in open(cap, encoding="utf-8") if l.strip()]

    if len(recs) == 3:
        ok("3 requests sent -> 3 captured (a recorder that drops one reports a cheaper "
           "client than the real one)")
    else:
        no("3 requests sent -> %d captured" % len(recs))

    if recs and recs[0]["body"] == anthropic_body:
        ok("the captured body is VERBATIM equal to what was sent")
    else:
        no("captured body differs from what was sent")

    if recs and recs[0]["raw_bytes"] == len(json.dumps(anthropic_body).encode()):
        ok("raw_bytes equals the exact serialized length on the wire")
    else:
        no("raw_bytes is not the wire length")

    # ...and that leg ALONE is circular, which a review proved: it compares raw_bytes
    # against `len(json.dumps(body).encode())`, which is exactly what a mutant computing
    # raw_bytes by RE-SERIALISING the parsed body also produces. The two only disagree
    # when the client's own serialization differs from json.dumps' defaults -- which is
    # the normal case for a real client, and the reason the measuring instrument prints a
    # wire-vs-normalised ratio at all. So: send compact bytes, and compare against the
    # length the TEST knows it sent.
    compact = json.dumps(anthropic_body, separators=(",", ":")).encode()
    normalised = json.dumps(anthropic_body).encode()
    assert len(compact) != len(normalised)      # the fixture must distinguish the two
    post_bytes("/v1/messages", compact)
    # A body this recorder CANNOT parse must still be recorded, with the marker the
    # measuring instrument refuses on and the real wire length. This is the leg a review
    # showed was missing entirely: two one-line mutations here -- storing `{}` instead of
    # the marker, or returning before writing at all -- survived the WHOLE preflight and
    # reproduced the original finding word for word, 144 KB of real traffic reported as
    # startup probes at exit 0. A refusal downstream cannot see a line that was never
    # written, so the only place this can be checked is here.
    garbage = b'{"model": "m", "messages": [ TRUNCATED'
    post_bytes("/v1/messages", garbage)
    # ...and NOTHING sent is a third case, distinct from both.
    post_bytes("/v1/messages", b"")
    # ----------------------------------------------------------------------------------
    # Three requests added after a review found two defects that needed a request urllib
    # cannot construct, and a third that needed a body that is not ASCII. Everything sent
    # above is ASCII and framed by Content-Length, which is exactly why neither defect was
    # visible: one word of this file (`"utf-8"` -> `"ascii"`) and one of do_POST's framing
    # survived both self-tests and the whole preflight.
    #
    # They are SENT here, before the shutdown, and asserted at the end with the rest.
    def post_raw(request_bytes):
        """Send a hand-built HTTP/1.1 request, so framing itself can be the subject."""
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            c.sendall(request_bytes)
            c.settimeout(10)
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(4096)
                if not d:
                    break
                buf += d
            return buf
        finally:
            c.close()

    # 7. A CHUNKED body -- no Content-Length at all. do_POST used to frame every request
    #    by `int(Content-Length or 0)`, so this read ZERO bytes and was recorded as
    #    `raw_bytes: 0, body: null`, which the measuring instrument then counted as a
    #    startup probe: a review put 40391 real bytes through it and got `1 measurable,
    #    1 startup probes` with a 100% coverage banner at exit 0. node's own `fetch` with
    #    a stream body sends exactly this.
    chunked_body = json.dumps({"model": "m", "tools": [{"name": "t"}],
                               "messages": [{"role": "user",
                                             "content": "C" * 300}]}).encode()
    half = len(chunked_body) // 2
    creq = (b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n")
    for part in (chunked_body[:half], chunked_body[half:]):
        creq += b"%x\r\n%s\r\n" % (len(part), part)
    creq += b"0\r\n\r\n"
    post_raw(creq)
    # 8. A body that could not be FRAMED at all. `Content-Length: 0` means nothing was
    #    sent and is recorded as `body: null`; this is the other case, and conflating the
    #    two is what defect 7 was. Length UNKNOWN is not length ZERO.
    post_raw(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
             b"Content-Type: application/json\r\nTransfer-Encoding: gzip\r\n"
             b"Connection: close\r\n\r\n")
    # 9. A NON-ASCII body. `raw.decode("utf-8", "replace")` -> `"ascii"` is one word, and
    #    it survived both self-tests and the whole preflight because every body above is
    #    ASCII -- while samples/fixtures/request_construction.hermes.jsonl already is not.
    cjk = {"model": "m", "system": "\u6f22\u5b57" * 200, "tools": [{"name": "t"}],
           "messages": [{"role": "user", "content": "\u30c6\u30b9\u30c8" * 100}]}
    cjk_bytes = json.dumps(cjk, ensure_ascii=False).encode("utf-8")
    post_bytes("/v1/messages", cjk_bytes)
    srv.shutdown()
    captured = [json.loads(l) for l in open(cap, encoding="utf-8") if l.strip()]
    # The legs below index the first six requests from the END, so the three sent above
    # are held back rather than renumbering six assertions around them.
    recs, extra = captured[:6], captured[6:]
    last = recs[-3]
    if last["raw_bytes"] == len(compact) and last["raw_bytes"] != len(normalised):
        ok("raw_bytes is the OBSERVED length (%d B of compact JSON), not a "
           "re-serialisation of the parsed body (%d B) -- the two differ here on "
           "purpose, because comparing them to each other cannot tell them apart"
           % (len(compact), len(normalised)))
    else:
        no("raw_bytes %r is a re-serialisation, not the wire length (compact %d, "
           "normalised %d)" % (last["raw_bytes"], len(compact), len(normalised)))

    unp = recs[-2]
    if (isinstance(unp.get("body"), dict) and "__unparseable__" in unp["body"]
            and unp["raw_bytes"] == len(garbage)):
        ok("an UNPARSEABLE body is still recorded, marked `__unparseable__`, with its "
           "real wire length (%d B) -- dropping it or storing an empty body would make "
           "the request vanish into the probe count downstream" % len(garbage))
    else:
        no("an unparseable body was not recorded with its marker and length: %r" % unp)
    if len(recs) == 6:
        ok("6 requests sent -> 6 recorded, including the two the recorder could not "
           "parse (a recorder that writes only what it understood reports a cheaper "
           "client than the real one)")
    else:
        no("6 requests sent -> %d recorded" % len(recs))

    empty = recs[-1]
    if empty.get("body") is None and empty["raw_bytes"] == 0:
        ok("a request that carried NO payload is recorded with `body: null`, not under "
           "the unparseable marker -- nothing sent is not the same as could not parse, "
           "and conflating them made the instrument refuse a whole good capture")
    else:
        no("an empty POST was not recorded as a null body: %r" % empty)

    # The one leg that protects a publishable artefact rather than a figure.
    blob = open(cap, encoding="utf-8").read()
    if "not-a-credential" not in blob and "Authorization" in blob:
        ok("header NAMES recorded, header VALUES not -- the Authorization value sent "
           "above is absent from the capture")
    else:
        no("an Authorization header VALUE reached the capture file")

    endpoints = {r["endpoint"] for r in recs}
    if endpoints == {"/v1/messages", "/v1/chat/completions"}:
        ok("both protocols landed on one capture, each tagged with its endpoint")
    else:
        no("endpoints captured: %r" % sorted(endpoints))

    if recs and all(r["arm"] == "selftest-arm" for r in recs):
        ok("the arm label is carried onto every record")
    else:
        no("arm label missing or wrong")

    # --- and the three sent just before the shutdown ---------------------------------
    if len(extra) == 3:
        ch, un, nz = extra
    else:
        ch = un = nz = {}
        no("3 requests sent after the six above -> %d recorded" % len(extra))
    if (ch.get("raw_bytes") == len(chunked_body)
            and isinstance(ch.get("body"), dict) and ch["body"].get("messages")):
        ok("a CHUNKED POST is de-chunked and recorded with its real length (%d B in 2 "
           "chunks, no Content-Length header at all) -- it used to record `raw_bytes: 0, "
           "body: null` and be counted downstream as a startup probe"
           % len(chunked_body))
    else:
        no("chunked POST recorded as raw_bytes=%r (sent %d B), body keys %r"
           % (ch.get("raw_bytes"), len(chunked_body),
              sorted(ch.get("body") or {})))
    if (un.get("raw_bytes") is None and isinstance(un.get("body"), dict)
            and UNREAD in un["body"] and "gzip" in un["body"][UNREAD]):
        ok("a body this recorder could not FRAME is marked `%s` with a NULL length and "
           "the reason -- not `raw_bytes: 0`, which would read as an observation that "
           "nothing was sent" % UNREAD)
    else:
        no("an unframeable body was not marked unread: %r" % un)
    if (nz.get("raw_bytes") == len(cjk_bytes)
            and (nz.get("body") or {}).get("system") == cjk["system"]):
        ok("a NON-ASCII body keeps its exact UTF-8 length (%d B, not the %d characters "
           "it decodes to) and round-trips unchanged -- decoding it as ascii replaces "
           "every multibyte character and inflated one real payload 2.9x"
           % (len(cjk_bytes), len(json.dumps(cjk, ensure_ascii=False))))
    else:
        no("non-ASCII body: raw_bytes=%r (sent %d B), system round-trip %s"
           % (nz.get("raw_bytes"), len(cjk_bytes),
              (nz.get("body") or {}).get("system") == cjk["system"]))


    print()
    print("self-test: %d pass, %d fail" % (len(checks), len(fails)))
    if fails:
        print("SELF-TEST FAILED -- do NOT believe any capture this produced.")
        return 1
    return 0


def main(argv):
    if "--help-blind" in argv:
        print(__doc__)
        return 0
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        print("usage: capture_proxy.py [PORT] | --self-test | --help-blind")
        print()
        print("  ARI_CAPTURE  path of the JSONL capture file (default %s)" % CAPTURE)
        print("  ARI_ARM      label written onto every captured record")
        return 0
    if "--self-test" in argv:
        return self_test()
    port = 4199
    for a in argv:
        if a.isdigit():
            port = int(a)
    srv = serve(port, CAPTURE, ARM)
    print("capture_proxy on 127.0.0.1:%d arm=%s capture=%s" % (port, ARM, CAPTURE),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
