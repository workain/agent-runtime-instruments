#!/usr/bin/env bash
# Prove scrub_check.sh can FAIL. One decoy per pattern, each planted alone.
#
# Why this exists: scrub_check.sh prints "CLEAN" both when the tree is clean and when
# its own regexes have silently stopped matching, and those two look identical. A
# scrubber with no decoy test is decoration.
#
# Each decoy is planted in a fresh temp file, the scanner is run, and the run must FAIL
# (exit 1) AND name that pattern's label. A decoy that trips a DIFFERENT pattern than
# the one it was written for is reported, because then that pattern is untested.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2

# label-fragment : decoy string
DECOYS=(
  'absolute home path|/home/someuser/secret/path'
  'internal fleet-control product|harness-control is the internal product'
  'internal PMO repo|see agent-lab-manager for context'
  'internal MCP servers|cao-mcp-server was running'
  'internal gateway/executor|read from gateway.db directly'
  'operator account identifiers|user klabulan on this box'
  'session identifiers|sess-0123456789abcdef'
  'bare UUIDs (session ids leak this way)|id 49565226-d4c0-4ce1-8202-d9ff1fe5103f'
  'email addresses|contact someone@example.com'
  'private IPs / internal hosts|connect to 10.1.2.3 now'
  'credential-shaped strings|ghp_abcdefghij0123456789abcdefghij'
  'actions-runner / CI host paths|under actions-runner/_work'
)

pass=0; fail=0
probe="scrub_decoy_probe.txt"
bin_probe=""
trap 'rm -f "$probe" "$bin_probe" 2>/dev/null; git rm --cached -q "$probe" "$bin_probe" 2>/dev/null; rmdir instruments/__pycache__ 2>/dev/null' EXIT

for entry in "${DECOYS[@]}"; do
  label="${entry%%|*}"; decoy="${entry#*|}"
  printf '%s\n' "$decoy" > "$probe"
  git add -f "$probe" >/dev/null 2>&1
  out=$(bash scripts/scrub_check.sh --tree 2>&1); rc=$?
  if [ "$rc" -ne 1 ]; then
    echo "FAIL [$label]: scanner exited $rc, expected 1 -- this pattern does not fire"
    fail=$((fail+1))
  elif ! echo "$out" | grep -qF "FINDING [$label]"; then
    other=$(echo "$out" | grep -o 'FINDING \[[^]]*\]' | head -1)
    echo "FAIL [$label]: scanner failed but reported $other instead -- this pattern is untested"
    fail=$((fail+1))
  else
    echo "ok   [$label]"
    pass=$((pass+1))
  fi
  rm -f "$probe"; git rm --cached -q "$probe" >/dev/null 2>&1
done

# BINARY decoy. This class is here because the 12 text decoys above could not catch it:
# they are all text written to a .txt, so the scanner's file-type handling was never
# exercised. A tracked .pyc embedding an internal absolute path was certified CLEAN,
# because `grep -I` skips binary files and `git log -G` does not search binary diffs.
# The decoy is a real compiled .pyc, built here, so it carries a real co_filename.
bin_probe="instruments/__pycache__/scrub_binary_decoy.cpython-probe.pyc"
mkdir -p instruments/__pycache__
printf 'x = "/home/someuser/harness-projects/leak"
' > /tmp/scrub_decoy_src.py
if python3 -c "import py_compile,sys; py_compile.compile('/tmp/scrub_decoy_src.py', cfile='$bin_probe', doraise=True)" 2>/dev/null    && [ -s "$bin_probe" ] && ! grep -qI . "$bin_probe" 2>/dev/null; then
  git add -f "$bin_probe" >/dev/null 2>&1
  out=$(bash scripts/scrub_check.sh --tree 2>&1); rc=$?
  if [ "$rc" -ne 1 ]; then
    echo "FAIL [binary decoy]: scanner exited $rc, expected 1 -- binary blindness is back"
    fail=$((fail+1))
  elif ! echo "$out" | grep -qF 'FINDING [tracked binary file]'; then
    echo "FAIL [binary decoy]: scanner failed but did not name the tracked-binary finding"
    fail=$((fail+1))
  else
    echo "ok   [binary decoy: tracked binary refused]"; pass=$((pass+1))
    # and separately: the CONTENT inside the binary must be reachable to the pattern scan
    if echo "$out" | grep -qF 'FINDING [absolute home path]'; then
      echo "ok   [binary decoy: content inside the blob is scanned (grep -a, not -I)]"
      pass=$((pass+1))
    else
      echo "FAIL [binary decoy]: the path INSIDE the blob was not matched -- grep -I is back"
      fail=$((fail+1))
    fi
  fi
  git rm --cached -q "$bin_probe" >/dev/null 2>&1; rm -f "$bin_probe"
else
  # a skipped control must never read as a passed one
  echo "FAIL [binary decoy]: could not build a binary probe, so this class is UNTESTED"
  fail=$((fail+1))
fi
rmdir instruments/__pycache__ 2>/dev/null
rm -f /tmp/scrub_decoy_src.py

# and the negative leg: with no decoy, the scanner must pass
out=$(bash scripts/scrub_check.sh --tree 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then
  echo "ok   [clean tree passes]"; pass=$((pass+1))
else
  echo "FAIL [clean tree passes]: scanner exited $rc on a tree with no decoy:"
  echo "$out" | grep -A3 FINDING | head -20
  fail=$((fail+1))
fi

echo
echo "decoy suite: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
