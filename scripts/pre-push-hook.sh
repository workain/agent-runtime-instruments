#!/usr/bin/env bash
# Identity and history exist only AFTER a commit, so a pre-COMMIT hook structurally
# cannot see them. This checks at push time instead.
#
# It scans HEAD's own ancestry, NOT --all: scanning every ref includes the stale
# remote-tracking ref, so a bad commit already on the remote made the hook block the
# very force-push that would remove it. A gate that prevents its own remediation is a
# deadlock, and it happened here.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
# Same allowlist and same matcher as scrub_check.sh's own identity leg -- deliberately
# the same FILE, not a second copy of it. This leg and that one differ only in rev range
# (HEAD's ancestry vs --all, see above); when they differed in their allowlist too, a
# grandfathered commit would be refused by whichever copy was not updated, and this hook
# would block every push off main while the scrubber said CLEAN.
# shellcheck source=scripts/identity_allowlist.sh
. scripts/identity_allowlist.sh 2>/dev/null || {
  echo "pre-push REFUSED: scripts/identity_allowlist.sh missing -- the identity check" >&2
  echo "  cannot run, and a check that cannot run does not get to pass." >&2
  exit 1; }
# `|| exit 1` for the same reason scrub_check.sh spells out at its own call site: the
# refusal inside identity_offenders can only end the subshell this substitution runs in,
# so an uncaught one would push with the check having never completed.
bad=$(identity_offenders HEAD) || exit 1
if [ -n "$bad" ]; then
  echo "pre-push REFUSED: non-noreply identity in the commits being pushed:" >&2
  echo "$bad" | sed 's/^/    /' >&2
  echo "  fix: git commit --amend --reset-author, with the GIT_AUTHOR_*/GIT_COMMITTER_*" >&2
  echo "  env vars UNSET (env -u ...) -- they override git config." >&2
  exit 1
fi
bash scripts/check_figures.sh >/dev/null
bash scripts/scrub_check.sh --tree >/dev/null
