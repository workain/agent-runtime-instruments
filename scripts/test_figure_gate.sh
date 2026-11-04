#!/usr/bin/env bash
# Prove check_figures.sh can FAIL, and prove it does NOT fail on ordinary prose.
#
# WHY THIS EXISTS, and why its absence was the real defect:
#
# Every other gate in this repository has a harness that proves it can fail --
# test_scrub_check.sh, test_fixture_freshness.sh, test_identity_allowlist.sh,
# test_flags_controls.sh. The FIGURE gate had none, and it is the one with the worst
# history: three consecutive review rounds found a regression in its span extraction,
# and the last two were in OPPOSITE directions.
#
#   round 2 added a joiner so a figure WRAPPED across a line break is still checked.
#   round 3 found that joining RE-PAIRS backticks, so a span left open on one line
#           swallows the next line's opening backtick and HIDES that line's figure.
#           Fixed by also reading the file as written and taking the union.
#   round 4 found that the as-written copy INVENTS figures: on the continuation line of
#           a legitimate wrapped span, the same mis-pairing emits the prose between one
#           span's closer and the next one's opener. Ordinary prose failed the gate.
#           Fixed by reading only the joined copy, and catching the hidden-span case
#           with a bare-`B` unit pattern over the file as written instead.
#
# Each of those fixes was invisible to every gate. Deleting the union leg, or deleting
# the bare-`B` pattern that replaced it, left the whole preflight green -- which is
# exactly how a figure stops being checked without anyone finding out. A hand-written
# evidence file under samples/gate-evidence/ recorded one attack once, in prose, and
# nothing re-ran it.
#
# So this harness pins BOTH directions, and pins them against the gate's own source:
# the catching cases must go RED, the ordinary-prose cases must stay GREEN, and removing
# the pattern that does the catching must make a catching case stop catching.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2

ROOT="$(pwd)"
fail=0
ok()  { printf '  ok   -- %s\n' "$1"; }
no()  { printf '  FAIL -- %s\n' "$1"; fail=1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# A pristine copy per case: ONE clone of HEAD, then a plain copy of it per case.
#
# A clone and not `git archive`: the gate asks git which samples are TRACKED, and an
# archive has no git metadata, so every cited sample reads as untracked and the pristine
# case fails for a reason that has nothing to do with figures. A clone and not `cp -r` of
# the working tree: an untracked scratch file must not be able to change what this
# harness measures.
base="$work/.base"
git clone -q "$ROOT" "$base" || exit 2
fresh() {
  local d="$work/$1"
  rm -rf "$d"
  cp -a "$base" "$d" || return 1
  printf '%s' "$d"
}

# run the gate in a copy; echo its exit code
gate() { ( cd "$1" && bash scripts/check_figures.sh >"$1/.out" 2>&1; echo $? ); }

insert_before_decisions() {   # $1=dir $2=file $3..=lines
  local d="$1" f="$2"; shift 2
  python3 - "$d/$f" "$@" <<'PY'
import sys
p, lines = sys.argv[1], sys.argv[2:]
t = open(p, encoding="utf-8").read().split("\n")
i = next((n for n, l in enumerate(t) if l.startswith("## ")), len(t))
t[i:i] = list(lines) + [""]
open(p, "w", encoding="utf-8").write("\n".join(t))
PY
}

echo "FIGURE GATE -- does it fail when it should, and only then?"

# ---------------------------------------------------------------- must go RED
# 1. THE HIDDEN-SPAN ATTACK (round 3's finding). Two lines of ONE paragraph, even
#    parity overall, no figure wrapped across them. The first line leaves a span open;
#    joining re-pairs the backticks so the planted figure is no longer inside a span.
#    `exit=2` really occurs in this repo's refusal samples, so quoting it is ordinary.
d="$(fresh hidden)" || exit 2
insert_before_decisions "$d" README.md \
  'The refusal ends `exit=2' \
  '`fixed 99999 B` is the per-turn cost, and the clean run ends `exit=0'
rc="$(gate "$d")"
if [ "$rc" != 0 ] && grep -q '99999 B' "$d/.out"; then
  ok "the HIDDEN-SPAN attack is caught (exit $rc), by the unit pattern that reads the file as written"
else
  no "hidden-span attack -> exit $rc, and the output does not name the figure"
fi

# 2. ...and the pattern that catches it is LOAD-BEARING. Delete the bare-`B` grep and
#    case 1 must stop catching. This is the leg nothing had: removing it left every
#    gate green, exactly as removing the union leg did before it.
d2="$(fresh hidden-nopattern)" || exit 2
insert_before_decisions "$d2" README.md \
  'The refusal ends `exit=2' \
  '`fixed 99999 B` is the per-turn cost, and the clean run ends `exit=0'
python3 - "$d2/scripts/check_figures.sh" <<'PY'
import sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
needle = """    grep -ohE '\\b[0-9][0-9.,]* ?B\\b' "$pf"\n"""
assert t.count(needle) == 1, "the bare-B leg is not where this harness thinks it is"
open(p, "w", encoding="utf-8").write(t.replace(needle, "", 1))
PY
rc2="$(gate "$d2")"
if [ "$rc2" = 0 ]; then
  ok "deleting the bare-\`B\` pattern makes that attack pass -- so the pattern is what catches it, not something else"
else
  no "with the bare-\`B\` pattern deleted the attack still fails (exit $rc2): this harness is pinning the wrong leg"
fi

# 3. A figure WRAPPED across a line break, present in no artefact (round 2's own case).
#    This is what the joined copy is for; it must still be caught.
d3="$(fresh wrapped)" || exit 2
insert_before_decisions "$d3" README.md 'The cost is `fixed 99999' 'bytes` per turn.'
rc3="$(gate "$d3")"
[ "$rc3" != 0 ] \
  && ok "a figure wrapped across a line break, in no artefact, is caught (exit $rc3)" \
  || no "wrapped-span figure -> exit $rc3"

# 4. A plain drifted figure on one line -- the gate's original job.
d4="$(fresh drifted)" || exit 2
python3 - "$d4/README.md" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
new, n = re.subn(r'`8225 bytes on the wire`', '`8226 bytes on the wire`', t)
assert n >= 1, "the pinned dsh wire clause is not in README"
open(p, "w", encoding="utf-8").write(new)
PY
rc4="$(gate "$d4")"
[ "$rc4" != 0 ] \
  && ok "a one-digit drift in a pinned clause is caught (exit $rc4)" \
  || no "one-digit drift -> exit $rc4"

# -------------------------------------------------------------- must stay GREEN
# 5. ROUND 4's FALSE ALARM: a long clause wrapped across a line, then a SECOND span
#    later on the continuation line. Ordinary prose, and this branch's own style. The
#    reverted union leg emitted the text between the two spans as a "figure".
d5="$(fresh ordinary)" || exit 2
cat >> "$d5/docs/method.md" <<'EOF'

The dsh arm publishes `6519 bytes (75.1% of the body) of provider metadata, which the
vendor documents at ZERO model-input tokens` -- 6519 of 8680 B -- while hermes publishes `provider metadata 0 B`.
EOF
rc5="$(gate "$d5")"
if [ "$rc5" = 0 ]; then
  ok "a wrapped clause followed by a second span on the continuation line does NOT false-alarm"
else
  ok_msg="$(grep -m1 '^FAIL' "$d5/.out" || true)"
  no "ordinary wrapped prose -> exit $rc5 ($ok_msg)"
fi

# 6. A CORRECT pinned clause quoted across two BLOCKQUOTE lines. Round 2's own fix:
#    without stripping the `> ` marker the continuation lands inside the span and the
#    gate invents a figure that exists nowhere.
d6="$(fresh blockquote)" || exit 2
insert_before_decisions "$d6" README.md \
  '> It reports `fixed 1665 bytes, paid again on' \
  '> every later turn` for the dsh arm.'
rc6="$(gate "$d6")"
[ "$rc6" = 0 ] \
  && ok "a correct clause quoted across two blockquoted lines does NOT false-alarm" \
  || no "blockquoted correct clause -> exit $rc6 ($(grep -m1 '^FAIL' "$d6/.out" || true))"

# 7. The unmodified tree must pass, or nothing above means anything.
d7="$(fresh pristine)" || exit 2
rc7="$(gate "$d7")"
[ "$rc7" = 0 ] \
  && ok "the unmodified tree passes, so every RED above is caused by its own planted defect" \
  || no "the UNMODIFIED tree fails the figure gate (exit $rc7) -- nothing above is meaningful"

echo
if [ "$fail" = 0 ]; then
  echo "FIGURE GATE SELF-TEST PASSED -- it fails on all 4 planted defects and on none of the 3 ordinary-prose cases"
  exit 0
fi
echo "FIGURE GATE SELF-TEST FAILED"
exit 1
