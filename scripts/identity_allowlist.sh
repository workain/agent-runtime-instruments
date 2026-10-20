#!/usr/bin/env bash
# The ONE list of commits grandfathered past the identity gates, plus the single matcher
# both of them use. Sourced by scripts/scrub_check.sh (--history/--all) and by
# scripts/pre-push-hook.sh; not executable on its own.
#
# Why a shared file rather than a copy in each gate: those two carry the SAME identity
# leg against different rev ranges (--all vs HEAD's ancestry), and two copies of a
# security allowlist drift. A drifted copy fails OPEN in whichever gate kept the entry,
# and nothing announces it -- both still print a pass.
#
# WHAT IS ALLOWLISTED HERE IS A COMMIT, NEVER A PATTERN. An entry is one exact 40-char
# SHA, matched whole and anchored. That is what makes this an exception rather than a
# hole: a SHA names one immutable commit including its own author and committer fields,
# so an entry cannot be made to cover a commit that does not exist yet -- changing a
# commit's identity changes its SHA. Any OTHER non-noreply identity, including one
# introduced tomorrow on top of an allowlisted commit, still fails exactly as before.
#
# WHAT WOULD THIS PRINT IF IT WERE BLIND?
#   Nothing -- it is a filter, and a filter that removes too much makes the gate that
#   uses it print the same "clean" it prints when history really is clean. Three ways to
#   be over-broad, and the guard for each:
#     1. A malformed entry (short, uppercase, a pattern). identity_allowlist_rx REFUSES
#        with exit 2 rather than building a regex out of it -- a broken allowlist is a
#        broken gate, not a pass.
#     2. A prefix silently matching a longer SHA. The match is anchored at both ends of
#        the SHA field (^...<space>), so a short entry matches NOTHING instead of
#        everything under it.
#     3. An entry whose commit is gone (history rewritten, repo recreated). It then
#        matches nothing, which is the safe direction: the gate gets stricter, not
#        looser, and the dead entry can be deleted at leisure.

# Each entry: one exact 40-char lowercase SHA, with the issue and the reason above it.
IDENTITY_ALLOWLIST=(
  # workain/agent-runtime-instruments#7 -- grandfathered pre-existing commit (main's tip,
  # the squash-merge of PR #2), already public before this gate could refuse it; the
  # operator approved this one-time exception instead of rewriting shared history. It is
  # not a general relaxation: see the merge recipe in README.md ("Merging a reviewed
  # branch into main"), which is the fix for the cause rather than for this commit.
  40372496f2687e0d1140cf94675f87dbf2309adf

  # workain/agent-runtime-instruments#10 -- the squash-merge of PR #10, and the SECOND
  # commit to need this exception for the SAME cause: it was merged through GitHub rather
  # than by the local recipe above, so GitHub set its author to the merging token's real
  # identity. Operator-approved as a one-time exception, on the same basis as the entry
  # above -- that identity is already this repo's own public commit identity throughout
  # this history, so grandfathering it exposes nothing new, and rewriting a shared main is
  # the worse trade.
  #
  # Being the second of these is the point worth reading. The cause is documented and
  # preventable -- README.md's recipe exists precisely because `gh pr merge` stamps the
  # merger's identity, and it names the first such commit by SHA -- so a third entry here
  # would mean the recipe is being bypassed rather than that the gate is too strict. The
  # gate is doing its job both times; what keeps failing is upstream of it.
  ed38f4d0fd9318ed7f76ae3ec8d0145db20ad78b
)

# Check every entry's SHAPE. Returns 2 (and says why) on a malformed one; never exits,
# so a caller decides whether that is fatal -- which matters because the callers below
# run in command substitutions, where an `exit` could only end the subshell anyway.
identity_allowlist_validate() {
  local sha
  for sha in ${IDENTITY_ALLOWLIST[@]+"${IDENTITY_ALLOWLIST[@]}"}; do
    # 40 chars AND nothing outside [0-9a-f] -- the second half is what rejects a regex
    # metacharacter, which is the entry that would quietly widen the match. (A bare `.*`
    # written here never even reaches this test as a metacharacter: an array literal is
    # glob-expanded at assignment, so it arrives as a list of filenames. Both forms are
    # refused, for the same reason -- neither is a commit.)
    [ "${#sha}" -eq 40 ] && [ -z "${sha//[0-9a-f]/}" ] && continue
    echo "IDENTITY-ALLOWLIST-ERROR: not a 40-char lowercase SHA: '$sha'" >&2
    echo "  Entries are matched whole, so anything else would either match nothing or" >&2
    echo "  match too much. Refusing to run the identity gate on a malformed list." >&2
    return 2
  done
  return 0
}

# True if this exact SHA is allowlisted. For a caller that already HAS a full SHA and no
# git history to match it against -- scripts/check_remote_identities.sh reads its SHAs
# from the remote's activity log, some of them deliberately unreachable locally, which
# is the whole reason that gate exists apart from the other two. Equality, not a regex:
# the activity log is already filtered to full 40-char lowercase SHAs, the same form
# stored here. Validate once at startup; this does not re-validate per call.
identity_allowlist_contains() {
  local sha
  for sha in ${IDENTITY_ALLOWLIST[@]+"${IDENTITY_ALLOWLIST[@]}"}; do
    [ "$1" = "$sha" ] && return 0
  done
  return 1
}

# Print an extended-regex alternation of the allowlisted SHAs. Exits 2 on a malformed
# entry, and prints an alternation that cannot match anything when the list is empty.
identity_allowlist_rx() {
  identity_allowlist_validate || exit 2
  if [ "${#IDENTITY_ALLOWLIST[@]}" -eq 0 ]; then
    printf '%s' 'x^'          # matches nothing, so the filter below is a no-op
    return
  fi
  ( IFS='|'; printf '%s' "${IDENTITY_ALLOWLIST[*]}" )
}

# Print every "<sha> <identity>" pair in the given rev range that is neither a noreply
# address nor carried by an allowlisted commit. Empty output = clean.
# Usage: identity_offenders --all   |   identity_offenders HEAD
identity_offenders() {
  local rx log
  rx=$(identity_allowlist_rx) || exit 2
  # git's own failure is caught here rather than inside the pipe, because the pipe's
  # status is unusable: grep exits 1 when it filters EVERYTHING out, which is precisely
  # the clean case. Hence the `|| true` below -- and hence a git error had to be caught
  # before it could arrive as that same empty, "clean"-looking output.
  log=$(git log "$@" --format='%H %an <%ae>%n%H %cn <%ce>') || {
    echo "IDENTITY-ERROR: git log failed for rev args: $*" >&2
    echo "  Refusing rather than reporting an empty (clean-looking) result." >&2
    exit 2; }
  { printf '%s\n' "$log" \
      | grep -vE '@users\.noreply\.github\.com>$' \
      | grep -vE "^($rx) " \
      | sort -u ; } || true
}
