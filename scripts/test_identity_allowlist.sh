#!/usr/bin/env bash
# Prove the identity allowlist is an EXCEPTION and not a hole.
#
# scripts/identity_allowlist.sh lets a named commit past the identity leg of
# scrub_check.sh and pre-push-hook.sh. That is a bypass living inside a privacy gate, and
# a bypass nobody tests is indistinguishable from a bypass that has quietly widened --
# the same argument scripts/test_scrub_check.sh makes for the pattern legs.
#
# WHAT WOULD THIS PRINT IF IT WERE BLIND?
#   "all N controls passed" -- the same line it prints when the allowlist is sound. So
#   every control here is a NEGATIVE one: each plants a commit that MUST still be
#   refused, and the suite fails if the gate accepts it. The decisive one is C, which
#   plants the allowlisted commit's OWN identity on a different SHA: if that passes, the
#   exception is keyed to a person rather than to a commit, which is the failure this
#   whole design exists to prevent.
#
# Nothing here touches the real repository: every commit is planted in a throwaway clone
# in a temp directory, and the clone is given the WORKING-TREE copies of the three
# scripts, so this tests what you are about to commit rather than what you last did.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
ROOT=$(pwd)
ALLOWED=40372496f2687e0d1140cf94675f87dbf2309adf   # the one grandfathered commit

pass=0; fail=0
TMP=$(mktemp -d) || { echo "FAIL: cannot create a temp dir, so this class is UNTESTED"; exit 1; }
trap 'rm -rf "$TMP"' EXIT

git clone -q --no-hardlinks "$ROOT" "$TMP/clone" 2>/dev/null || {
  echo "FAIL: cannot clone this repo, so this class is UNTESTED"; exit 1; }
cd "$TMP/clone" || exit 2

# The clone's own copies are the COMMITTED ones; these are the ones you are about to
# commit. Re-staged after every rollback below, because `git reset --hard` would put the
# committed versions back -- which it silently did, and two controls then passed
# judgement on the wrong scanner.
stage_scripts() {
  cp "$ROOT/scripts/scrub_check.sh" "$ROOT/scripts/identity_allowlist.sh" \
     "$ROOT/scripts/pre-push-hook.sh" scripts/ \
    || { echo "FAIL: cannot stage working-tree scripts"; exit 1; }
}
stage_scripts

# A test clone that does not contain the allowlisted commit would make control A vacuous
# -- it would pass for the wrong reason. Reported as a failure, not skipped: if the
# commit is really gone, the entry is dead weight in a security file and should go.
if ! git cat-file -e "$ALLOWED^{commit}" 2>/dev/null; then
  echo "FAIL: the allowlisted commit is absent from this history, so control A below"
  echo "      proves nothing. Either this is a partial clone, or the entry is stale and"
  echo "      should be deleted from scripts/identity_allowlist.sh."
  exit 1
fi

AT='@'   # assembled: a literal address here would (correctly) trip the scrubber's own
         # `email addresses` pattern, and this file is deliberately NOT self-excluded.

plant() {  # plant <name> <email> -- an EMPTY commit, so only identity is under test
  GIT_AUTHOR_NAME="$1" GIT_AUTHOR_EMAIL="$2" \
  GIT_COMMITTER_NAME="$1" GIT_COMMITTER_EMAIL="$2" \
    git commit -q --allow-empty -m "synthetic control commit" || return 1
  git rev-parse HEAD
}

check() {  # check <label> <expected-rc> [<string the output must contain>]
  local label="$1" want="$2" needle="${3:-}" out rc
  out=$(bash scripts/scrub_check.sh --all 2>&1); rc=$?
  if [ "$rc" -ne "$want" ]; then
    echo "FAIL [$label]: scrub_check.sh --all exited $rc, expected $want"
    echo "$out" | sed -n '/FINDING/,$p' | sed 's/^/      /' | head -6
    fail=$((fail+1)); return
  fi
  if [ -n "$needle" ] && ! echo "$out" | grep -qF "$needle"; then
    echo "FAIL [$label]: exit code was right but the finding did not name $needle"
    fail=$((fail+1)); return
  fi
  echo "ok   [$label]"; pass=$((pass+1))
}

# A: the grandfathered commit alone must PASS -- otherwise the exception does not work
#    and every branch cut from main stays blocked, which is what issue #7 was about.
check "grandfathered commit alone passes" 0

# B: any OTHER real-looking identity must still be refused, and named.
sha=$(plant "Jane Doe" "jane.doe${AT}example.com")
check "a different identity is still refused" 1 "$sha"
out=$(bash scripts/pre-push-hook.sh 2>&1); rc=$?
if [ "$rc" -eq 1 ] && echo "$out" | grep -qF "$sha"; then
  echo "ok   [pre-push hook refuses it too]"; pass=$((pass+1))
else
  echo "FAIL [pre-push hook refuses it too]: exited $rc without naming $sha"
  fail=$((fail+1))
fi
git reset -q --soft HEAD~1; stage_scripts

# C: THE DECISIVE ONE. The allowlisted commit's own author identity, on a different
#    commit, must still be refused -- the exception is a SHA, not a person.
sha=$(plant "Maxim Levko" "levko.maxim${AT}gmail.com")
check "the allowlisted IDENTITY on another SHA is still refused" 1 "$sha"
git reset -q --soft HEAD~1; stage_scripts

# D: the committer address GitHub's own merge button writes is not a users.noreply one
#    either, so a future `gh pr merge` must still be caught. This is the control that
#    keeps README's merge recipe honest rather than advisory.
sha=$(plant "GitHub" "noreply${AT}github.com")
check "a GitHub web-merge committer is still refused" 1 "$sha"
git reset -q --soft HEAD~1; stage_scripts

# E/F: a malformed entry must make the gate REFUSE (exit 2), never pass. A short SHA
#      would otherwise prefix-match, and a metacharacter would widen the alternation.
for bad in '4037249' '.*'; do
  sed -i "s|^  ${ALLOWED}\$|  ${bad}|" scripts/identity_allowlist.sh
  out=$(bash scripts/scrub_check.sh --all 2>&1); rc=$?
  if [ "$rc" -eq 2 ] && echo "$out" | grep -q 'IDENTITY-ALLOWLIST-ERROR'; then
    echo "ok   [malformed entry '$bad' makes the gate refuse, not pass]"; pass=$((pass+1))
  else
    echo "FAIL [malformed entry '$bad']: exited $rc, expected 2 with an explicit refusal"
    fail=$((fail+1))
  fi
  stage_scripts
done

# G: and the list must be load-bearing -- with it emptied, the grandfathered commit is
#    refused again. If this passes, the exception is doing nothing and something else is.
sed -i "s|^  ${ALLOWED}\$|  |" scripts/identity_allowlist.sh
out=$(bash scripts/scrub_check.sh --all 2>&1); rc=$?
if [ "$rc" -eq 1 ] && echo "$out" | grep -qF "$ALLOWED"; then
  echo "ok   [with the entry removed, the gate refuses that commit again]"; pass=$((pass+1))
else
  echo "FAIL [entry removed]: exited $rc -- the allowlist is not what makes control A pass"
  fail=$((fail+1))
fi
stage_scripts

echo
echo "identity-allowlist suite: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
