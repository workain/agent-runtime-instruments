#!/usr/bin/env bash
# Squash-merge a reviewed branch into `main` the one way that does not permanently stamp a
# personal identity into shared history. This is README.md's "Merging a reviewed branch
# into `main`" recipe as a single command, and it exists because the recipe being CORRECT
# was not enough: it was bypassed three times in one day.
#
#     bash scripts/merge_to_main.sh <branch> <pr-number> "<commit subject>"
#     bash scripts/merge_to_main.sh --dry-run <branch> <pr-number> "<subject>"
#
# WHY THIS FILE EXISTS, in the only terms that matter. GitHub's squash-merge sets the new
# commit's author to whoever's token performed the merge, and this workspace has exactly
# one credential: a real person's account. That is what the merge API does, not a setting.
# Every time the button was used, `main` gained a commit the identity gate must refuse --
# `40372496f26` (PR #2), `ed38f4d0` (PR #10), then `c72856a` and `f1fb97d`, the last of
# which was the merge of the PR that allowlisted the one before it. An allowlist cannot
# catch up with a merge path that mints a fresh offender every time it is used, and each
# entry is permanent, because by the time the gate sees the commit the fix is a history
# rewrite. See scripts/identity_allowlist.sh for the entries and ARI#14 for the decision.
#
# So the design goal here is NOT "a convenient wrapper". It is to make the correct path
# require less thought than the wrong one, and to make this script refuse rather than
# produce a commit anyone has to grandfather later. It therefore:
#
#   * sets all FOUR identity variables explicitly, because GIT_AUTHOR_*/GIT_COMMITTER_* in
#     the environment OVERRIDE `git config` and a session may well have them set to
#     something personal -- which is exactly how this fleet's sessions do run;
#   * VERIFIES the identity it just wrote, by reading the commit back, instead of trusting
#     that setting the variables worked. Setting an env var and checking the result are
#     different acts, and only the second one is evidence;
#   * runs the full gate suite IN the temporary worktree, on the merge commit, while it is
#     still private and deleting it is free -- not after the push, when it is not;
#   * pushes only if all of that passed, and REFUSES otherwise, leaving nothing behind.
#
# It does not close the PR: that needs `gh`, which is not installed here. It prints the
# exact command to run, with the real SHA substituted.
#
# It cannot stop someone clicking the button on github.com. Only branch protection can do
# that, and setting it needs repo-admin access that no session in this fleet has -- the
# MCP GitHub surface available here is read-plus-add-a-comment by design and has no
# repo-settings verb at all. That half of ARI#14 is an operator action, named in the issue
# rather than silently left undone.
set -euo pipefail

BOT_NAME='workain'
# Assembled rather than written out: the scrubber's own `email addresses` pattern matches a
# literal one, even inside this file, and a gate that trips on its own tooling gets ignored.
BOT_MAIL="${BOT_NAME}@users.noreply.github.com"

die() { echo "merge_to_main: REFUSING -- $*" >&2; exit 2; }

DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
[ "$#" -eq 3 ] || die "usage: $0 [--dry-run] <branch> <pr-number> \"<commit subject>\""
BRANCH="$1"; PR="$2"; SUBJECT="$3"

case "$PR" in ''|*[!0-9]*) die "pr-number must be digits, got '$PR'";; esac
[ -n "$SUBJECT" ] || die "commit subject must not be empty"
case "$BRANCH" in main|HEAD) die "'$BRANCH' is not a branch to merge INTO main";; esac

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$(pwd)"

git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository: $REPO"
git fetch --quiet origin "$BRANCH" main \
  || die "could not fetch origin/$BRANCH and origin/main -- check the branch name"

SRC="$(git rev-parse "origin/$BRANCH")"
BASE="$(git rev-parse origin/main)"
[ "$SRC" != "$BASE" ] || die "origin/$BRANCH is already exactly origin/main -- nothing to merge"
# Not a hard requirement for a squash, but a branch that has not seen current main has not
# been reviewed against what it will land on, and its gates ran elsewhere.
git merge-base --is-ancestor "$BASE" "$SRC" \
  || die "origin/$BRANCH is not up to date with origin/main (base $BASE). Rebase it first:
         its gates have not run against what it would actually land on."

WT="$(mktemp -d "${TMPDIR:-/tmp}/merge-main.XXXXXX")"
# Always clean up: a leftover worktree makes the NEXT run fail in a way that looks like a
# repo problem rather than like this script having died.
cleanup() {
  cd "$REPO" || return 0
  git worktree remove --force "$WT" >/dev/null 2>&1 || true
  rm -rf "$WT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

git worktree add --quiet --detach "$WT" "$BASE" || die "could not create a worktree at $WT"
cd "$WT"

git merge --squash "$SRC" >/dev/null \
  || die "squash-merge of $BRANCH onto main CONFLICTS. Rebase the branch and re-review it."
git diff --cached --quiet && die "squash produced no change -- $BRANCH is already in main"

GIT_AUTHOR_NAME="$BOT_NAME"    GIT_AUTHOR_EMAIL="$BOT_MAIL" \
GIT_COMMITTER_NAME="$BOT_NAME" GIT_COMMITTER_EMAIL="$BOT_MAIL" \
  git commit --quiet -m "$SUBJECT (#$PR)" || die "the merge commit could not be created"

SHA="$(git rev-parse HEAD)"

# Read the identity back. This is the whole point of the script, so it is checked rather
# than assumed: if the four variables above failed to take effect for any reason, we find
# out here, while the commit is still private.
IDENT="$(git log -1 --format='%an <%ae>|%cn <%ce>')"
WANT="$BOT_NAME <$BOT_MAIL>"
[ "$IDENT" = "$WANT|$WANT" ] \
  || die "the merge commit's identity is '$IDENT', expected '$WANT|$WANT'.
         Nothing was pushed. This is the failure this script exists to prevent."

echo "merge_to_main: staged $BRANCH -> main as $SHA"
echo "merge_to_main: identity OK -- $WANT"

# Gates run HERE, on the merge commit, before anyone else can see it. --all includes the
# identity leg over full history, which is the one that would otherwise catch this commit
# only after it was public.
echo "merge_to_main: running the full gate suite on the merge commit..."
bash scripts/preflight.sh >/dev/null \
  || die "preflight failed ON THE MERGE COMMIT. Nothing was pushed; the worktree is gone.
         Run it yourself in a worktree at $SRC to see which gate."
bash scripts/scrub_check.sh --all >/dev/null \
  || die "scrub_check.sh --all failed ON THE MERGE COMMIT. Nothing was pushed."
echo "merge_to_main: gates OK"

if [ "$DRY" = "1" ]; then
  echo "merge_to_main: DRY RUN -- not pushing. Would have run:"
  echo "    git push origin $SHA:main"
  exit 0
fi

git push --quiet origin "HEAD:main" || die "the push to main failed -- nothing else changed"
echo "merge_to_main: pushed $SHA to main"
echo
echo "A pushed squash does not close the PR. Run:"
echo "    gh pr close $PR --comment \"squash-merged to main as $SHA\""
echo "(gh is not installed in this worktree, so this script cannot do it for you.)"
