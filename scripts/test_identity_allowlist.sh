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
     "$ROOT/scripts/pre-push-hook.sh" "$ROOT/scripts/check_remote_identities.sh" scripts/ \
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

# ---------------------------------------------------------------------------
# The REMOTE leg. check_remote_identities.sh reads the remote's activity log through
# `gh`, so it is the one gate that cannot be exercised by planting a local commit -- and
# `gh` is absent from some environments entirely (it is absent from the one this was
# written in). So `gh` is STUBBED from fixture files: no network, deterministic, and it
# makes the "the remote cannot resolve this SHA" branch testable, which a live remote
# cannot be asked to produce on demand.
#
# What the stub does NOT prove: that the real `gh api` returns these shapes. That is
# checked against the real remote by whoever has credentials; this suite checks the
# allowlist logic layered on top of it, which is the part that can silently widen.
# ---------------------------------------------------------------------------
STUB="$TMP/bin"; mkdir -p "$STUB"
cat > "$STUB/gh" <<'STUBEOF'
#!/usr/bin/env bash
# fixture-backed stand-in for the two `gh api` calls check_remote_identities.sh makes
case "${2:-}" in
  */activity)  cat "$GH_FIXTURES/activity" ;;
  */commits/*) f="$GH_FIXTURES/commit-${2##*/}"
               [ -f "$f" ] || exit 1      # a SHA the remote will not resolve
               cat "$f" ;;
  *) exit 1 ;;
esac
STUBEOF
chmod +x "$STUB/gh"
export GH_FIXTURES="$TMP/fx"
OTHER1=1111111111111111111111111111111111111111
OTHER2=2222222222222222222222222222222222222222
GONE=3333333333333333333333333333333333333333
GOOD="workain${AT}users.noreply.github.com workain${AT}users.noreply.github.com"

fixtures() {  # fixtures <sha>:<emails-or-"gone"> ...
  rm -rf "$GH_FIXTURES"; mkdir -p "$GH_FIXTURES"; : > "$GH_FIXTURES/activity"
  local spec sha body
  for spec in "$@"; do
    sha="${spec%%:*}"; body="${spec#*:}"
    echo "$sha" >> "$GH_FIXTURES/activity"
    [ "$body" = "gone" ] || echo "$body" > "$GH_FIXTURES/commit-$sha"
  done
}

remote_check() {  # remote_check <label> <cwd> <want-rc> [<needle>...]
  local label="$1" cwd="$2" want="$3"; shift 3
  local out rc needle
  # Captured and status-checked in ONE scope on purpose: an earlier version set the exit
  # code inside the command substitution, where the assignment died with the subshell --
  # the same shape as the `exit 2` bug these controls were written to catch.
  out=$(cd "$cwd" && PATH="$STUB:$PATH" \
          bash "$TMP/clone/scripts/check_remote_identities.sh" owner/repo 2>&1); rc=$?
  if [ "$rc" -ne "$want" ]; then
    echo "FAIL [$label]: exited $rc, expected $want"
    echo "$out" | sed 's/^/      /' | head -6; fail=$((fail+1)); return
  fi
  for needle in "$@"; do
    if ! echo "$out" | grep -qF "$needle"; then
      echo "FAIL [$label]: output never said '$needle'"
      echo "$out" | sed 's/^/      /' | head -6; fail=$((fail+1)); return
    fi
  done
  echo "ok   [$label]"; pass=$((pass+1))
}

# R1: the grandfathered commit, served by the remote with the identity it really has.
#     Must pass -- AND must say it was exempted rather than checked, and must not claim
#     2 of 2 commits carry a noreply identity when it only ever looked at one.
fixtures "$ALLOWED:Maxim Levko${AT}example.invalid Maxim Levko${AT}example.invalid" "$OTHER1:$GOOD"
remote_check "remote: allowlisted commit passes and says so" "$TMP" 0 \
  "ALLOWLISTED" "${ALLOWED:0:10}" "1 of 2 pushed commits" "1 allowlisted by SHA"

# R2: any other identity the remote serves is still a finding.
fixtures "$ALLOWED:$GOOD" "$OTHER1:jane.doe${AT}example.com jane.doe${AT}example.com"
remote_check "remote: a different identity is still refused" "$TMP" 1 \
  "FINDING" "${OTHER1:0:10}"

# R3: the decisive one again, on this leg: the allowlisted commit's OWN address, on a
#     different SHA the remote serves, must still be refused.
fixtures "$ALLOWED:$GOOD" "$OTHER2:levko.maxim${AT}gmail.com levko.maxim${AT}gmail.com"
remote_check "remote: the allowlisted IDENTITY on another SHA is still refused" "$TMP" 1 \
  "FINDING" "${OTHER2:0:10}"

# R4: a SHA the remote no longer resolves is still reported and still counted -- the
#     behaviour that existed before the allowlist, guarded against this edit.
fixtures "$GONE:gone" "$OTHER1:$GOOD"
remote_check "remote: an unresolvable SHA is still reported, not skipped" "$TMP" 0 \
  "NOT RESOLVABLE" "${GONE:0:10}"

# R5: run from a DIFFERENT directory. The other two gates cd to the repo root before
#     sourcing; this one never cds, so a cwd-relative source would break here and only
#     here. $TMP above is already not the repo -- this one is deeper and unrelated.
mkdir -p "$TMP/elsewhere/deeper"
fixtures "$ALLOWED:Maxim Levko${AT}example.invalid Maxim Levko${AT}example.invalid"
remote_check "remote: works when run from outside the repo" "$TMP/elsewhere/deeper" 0 \
  "ALLOWLISTED"

# R6: a malformed allowlist must stop this gate too, before it scans anything.
sed -i "s|^  ${ALLOWED}\$|  4037249|" "$TMP/clone/scripts/identity_allowlist.sh"
fixtures "$ALLOWED:$GOOD"
remote_check "remote: malformed entry makes the gate refuse, not pass" "$TMP" 2 \
  "IDENTITY-ALLOWLIST-ERROR"
stage_scripts

# R7: and with no allowlist file beside it at all, it must refuse rather than fall back
#     to scanning without one -- the failure mode where a gate loses its exception list
#     and starts reporting findings nobody can act on.
mkdir -p "$TMP/lonely/scripts"
cp "$ROOT/scripts/check_remote_identities.sh" "$TMP/lonely/scripts/"
fixtures "$ALLOWED:$GOOD"
out=$(cd "$TMP" && PATH="$STUB:$PATH" bash "$TMP/lonely/scripts/check_remote_identities.sh" \
        owner/repo 2>&1); rc=$?
if [ "$rc" -eq 2 ] && echo "$out" | grep -qF "cannot load identity_allowlist.sh"; then
  echo "ok   [remote: refuses when its allowlist file is missing]"; pass=$((pass+1))
else
  echo "FAIL [remote: refuses when its allowlist file is missing]: exited $rc"
  echo "$out" | sed 's/^/      /' | head -4; fail=$((fail+1))
fi

echo
echo "identity-allowlist suite: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
