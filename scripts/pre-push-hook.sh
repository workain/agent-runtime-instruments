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
bad=$(git log HEAD --format='%an <%ae>%n%cn <%ce>' | sort -u \
      | grep -vE '@users\.noreply\.github\.com>$' || true)
if [ -n "$bad" ]; then
  echo "pre-push REFUSED: non-noreply identity in the commits being pushed:" >&2
  echo "$bad" | sed 's/^/    /' >&2
  echo "  fix: git commit --amend --reset-author, with the GIT_AUTHOR_*/GIT_COMMITTER_*" >&2
  echo "  env vars UNSET (env -u ...) -- they override git config." >&2
  exit 1
fi
bash scripts/check_figures.sh >/dev/null
bash scripts/scrub_check.sh --tree >/dev/null
