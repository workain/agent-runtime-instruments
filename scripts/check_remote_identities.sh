#!/usr/bin/env bash
# The ONLY gate that can see an orphaned bad commit. Everything else in this repo is
# blind to it by construction.
#
# WHY THIS EXISTS. A commit carrying a personal email was force-pushed off the branch.
# After that force-push every local gate went green and was CORRECT to: scrub_check.sh's
# identity leg uses `git log --all`, and the pre-push hook scans HEAD's ancestry -- both
# reachable-objects-only, and the commit was no longer reachable. Meanwhile GitHub kept
# serving it. So the exposure persisted with no detector anywhere on this side.
#
# This asks the REMOTE what it has, via the repository activity log, which records the
# before/after SHA of every push including force-pushes.
#
# WHAT WOULD THIS PRINT IF IT WERE BLIND?
#   "all N pushed commits carry a noreply identity" -- the same thing it prints when they
#   genuinely do. Three ways to be blind, and the guard for each:
#     1. The activity log is empty or unreadable (token scope, API change). Then the
#        count is 0 and this REFUSES with exit 2 rather than printing a clean result.
#     2. A SHA no longer resolves. That is reported per-SHA, not skipped -- a commit the
#        remote has forgotten is a different fact from one with a good identity.
#     3. `gh` is absent or unauthenticated -> exit 2.
#
#     4. An allowlisted commit reading as a verified one. Commits named in
#        scripts/identity_allowlist.sh are NOT scanned here, so each is printed
#        individually as ALLOWLISTED and counted apart from the ones that passed -- the
#        closing line never claims a commit carries a noreply identity when the gate
#        did not look. A malformed allowlist refuses with exit 2 rather than scanning
#        on, for the same reason an empty activity log does.
#
#   And one it cannot close: this sees what the ACTIVITY LOG recorded. An object reaching
#   the remote by some other route is outside its view.
set -uo pipefail
REPO="${1:-}"
[ -n "$REPO" ] || REPO=$(git remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]##; s#\.git$##')
[ -n "$REPO" ] || { echo "usage: $0 <owner/repo>" >&2; exit 2; }
command -v gh >/dev/null 2>&1 || { echo "REMOTE-CHECK-UNAVAILABLE: gh not on PATH" >&2; exit 2; }

# The same allowlist the two LOCAL identity legs use (scrub_check.sh, pre-push-hook.sh),
# deliberately the same file rather than a third copy of the rule. Sourced relative to
# THIS script, not to the cwd: this gate takes owner/repo as an argument and is meant to
# run from anywhere, so it never cds to the repo root the way those two do.
# shellcheck source=scripts/identity_allowlist.sh
. "$(dirname "${BASH_SOURCE[0]}")/identity_allowlist.sh" 2>/dev/null || {
  echo "REMOTE-CHECK-UNAVAILABLE: cannot load identity_allowlist.sh beside this script" >&2
  echo "  A gate that cannot read its own allowlist does not get to report a pass." >&2
  exit 2; }
# Validated ONCE, here, where a refusal can actually end the script. Inside the loop it
# would run in a command substitution, whose subshell is all that an exit can end -- the
# shape that already produced one gate printing "cannot run" and then a clean result.
identity_allowlist_validate || exit 2

shas=$(gh api "repos/$REPO/activity" --paginate \
         --jq '.[] | .before, .after' 2>/dev/null \
       | grep -E '^[0-9a-f]{40}$' | grep -v '^0\{40\}$' | sort -u) || {
  echo "REMOTE-CHECK-ERROR: could not read the activity log for $REPO" >&2; exit 2; }

n=$(echo "$shas" | grep -c . || true)
if [ "${n:-0}" -eq 0 ]; then
  echo "REMOTE-CHECK-UNAVAILABLE: activity log returned no SHAs. An empty scan is not a" >&2
  echo "clean scan -- refusing rather than reporting a pass." >&2
  exit 2
fi

fail=0; checked=0; allowed=0
while IFS= read -r sha; do
  [ -z "$sha" ] && continue
  if identity_allowlist_contains "$sha"; then
    # Counted and printed, never silently skipped: a `continue` here would shrink the
    # closing line's own total, and an exempted commit would read as a verified one in
    # the one gate whose entire job is saying what the remote still serves.
    echo "  ${sha:0:10}  ALLOWLISTED -- not scanned (scripts/identity_allowlist.sh)"
    checked=$((checked+1)); allowed=$((allowed+1)); continue
  fi
  out=$(gh api "repos/$REPO/commits/$sha" \
          --jq '[.commit.author.email, .commit.committer.email] | join(" ")' 2>/dev/null) || {
    echo "  ${sha:0:10}  NOT RESOLVABLE on the remote (reported, not skipped)"
    checked=$((checked+1)); continue; }
  checked=$((checked+1))
  bad=0
  for e in $out; do
    case "$e" in *@users.noreply.github.com) ;; *) bad=1;; esac
  done
  if [ "$bad" -eq 1 ]; then
    echo "FINDING: commit ${sha:0:10} is SERVED BY THE REMOTE with a non-noreply identity: $out"
    echo "  Reachable history may be clean and every local gate green -- this commit is"
    echo "  orphaned, not removed. A force-push does not delete the object."
    echo "  Remediation: delete and recreate the repository, then verify with BOTH"
    echo "  a FULL-40-char-SHA fetch (expect 'not our ref') and the API (expect 404)."
    fail=1
  fi
done <<< "$shas"

if [ "$fail" -eq 0 ]; then
  if [ "$allowed" -gt 0 ]; then
    # Deliberately NOT "all N ... carry a noreply identity": an allowlisted commit does
    # not, which is why it needed allowlisting. Saying so is the whole point.
    echo "$((checked - allowed)) of $checked pushed commits the activity log knows about"
    echo "carry a noreply identity; $allowed allowlisted by SHA and not scanned at all"
  else
    echo "all $checked pushed commits the activity log knows about carry a noreply identity"
  fi
  echo "(see the header: this reads the activity log, not every object the remote holds)"
  exit 0
fi
exit 1
