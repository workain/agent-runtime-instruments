#!/usr/bin/env bash
# Refuse to let site-specific or private material into a PUBLIC repository.
#
# Scans the WORKING TREE and, with --history, every blob in git history. History matters
# as much as the tree: a path scrubbed in a later commit is still public in an earlier
# one, so this must pass BEFORE the first push, not before the repo is made public.
#
# WHAT WOULD THIS PRINT IF IT WERE BLIND?
#   "CLEAN" -- the same thing it prints when the tree really is clean. So the suite
#   `scripts/test_scrub_check.sh` plants a decoy for EVERY pattern below and asserts
#   this script fails on each one. A scrubber with no decoy test is decoration: an
#   anchored regex that silently stops matching looks exactly like a clean tree.
#
# Exit 0 = clean. Exit 1 = findings. Exit 2 = cannot run (so a broken scrubber is never
# mistaken for a clean result).
set -uo pipefail

MODE="${1:---tree}"
cd "$(dirname "${BASH_SOURCE[0]}")/.." || { echo "SCRUB-ERROR: cannot cd to repo root" >&2; exit 2; }
command -v git >/dev/null 2>&1 || { echo "SCRUB-ERROR: git not found" >&2; exit 2; }

# Each entry: <label>|<extended regex>. Deliberately specific: a pattern broad enough to
# match the public package names this repo legitimately discusses (claude-agent-acp,
# --remote-control, claude-agent-sdk) would fire constantly and be switched off.
PATTERNS=(
  # The trailing slash here used to be MANDATORY, so a bare `/home/harness` was
  # invisible -- and the decoy for this pattern always supplied one, so the suite could
  # never have caught it. Found by an independent reviewer. /Users and /root added for
  # the same reason: the pattern was named "absolute home path" and only knew one OS.
  'absolute home path|(/home|/Users)/[A-Za-z0-9_][A-Za-z0-9_.-]*|/root(/|$|[^A-Za-z0-9])'
  # `harness[_-]control` not `harness-control`: the underscore spelling appears in
  # module paths and env var names and was passing.
  'internal fleet-control product|harness[_-]control|harness[_-]projects|harness[_-]tmux|harness/sessions'
  'internal PMO repo|agent[_-]lab[_-]manager'
  'internal MCP servers|cao[_-]mcp[_-]server|harness[_-]control[_-]mcp[_-]server|gateway\.mcp_server'
  'internal gateway/executor|gateway\.(db|sqlite3?)|executor\.api|hangar[_-]deploy|alm[_-]service'
  'operator account identifiers|klabulan|claude[_-]code[_-]hangar'
  # {12,} is deliberate, not an oversight: the instruments' own --anonymise emits
  # `sess-<8 hex>`, which is a sha256 PREFIX of the real tag and not a truncation of it
  # (verified: anonymise('pressure-telemetry-1654') == 'sess-36e72ec4', which is not a
  # prefix of the input). A tighter bound would flag every anonymised sample. The
  # consequence, stated: a real identifier that is only 8 hex chars long would pass.
  'session identifiers|[Ss][Ee][Ss][Ss]-[0-9a-fA-F]{12,}'
  'bare UUIDs (session ids leak this way)|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
  'email addresses|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
  # 172.16/12 was missing: RFC1918 has three blocks and this knew two.
  'cloud metadata endpoint|169\.254\.169\.254'
  'internal hostnames|[A-Za-z0-9-]+\.internal\.[A-Za-z0-9.-]+'
  'private IPs / internal hosts|\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b|\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b|\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b'
  # GitHub has retired `ghp_` as the only shape: fine-grained PATs are `github_pat_`,
  # and app/user tokens are `ghs_`/`gho_`. OpenAI project keys are `sk-proj-`.
  'credential-shaped strings|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gh[so]_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}'
  'actions-runner / CI host paths|actions-runner'
)

# Files this scanner must not flag itself on: it necessarily CONTAINS every pattern.
SELF_EXCLUDE='^(scripts/scrub_check\.sh|scripts/test_scrub_check\.sh)$'

fail=0
scan_tree() {
  local files
  files=$(git ls-files 2>/dev/null) || { echo "SCRUB-ERROR: git ls-files failed" >&2; exit 2; }
  if [ -z "$files" ]; then
    echo "SCRUB-ERROR: git ls-files returned nothing -- an empty scan is not a clean scan" >&2
    exit 2
  fi
  echo "scanning $(echo "$files" | wc -l | tr -d ' ') tracked files"
  local entry label rx hits
  for entry in "${PATTERNS[@]}"; do
    label="${entry%%|*}"; rx="${entry#*|}"
    # `grep -a`, NOT `-I`. With `-I` grep SKIPS binary files entirely, and a compiled
    # .pyc stores its source path verbatim in `co_filename`: a tracked
    # instruments/__pycache__/*.pyc embedded an internal absolute path, matched two of
    # the patterns below, and this scanner certified the tree CLEAN. Measured on that
    # blob: `grep -aE` -> 1 match, `grep -IE` -> 0. Found by an independent reviewer.
    hits=$(echo "$files" | grep -Ev "$SELF_EXCLUDE" | tr '\n' '\0' \
           | xargs -0 -r grep -naE "$rx" 2>/dev/null)
    if [ -n "$hits" ]; then
      fail=1
      echo "FINDING [$label]:"
      echo "$hits" | sed 's/^/    /' | head -25
    fi
  done
}

scan_history() {
  echo "scanning git history (all blobs, all commits)"
  local revs entry label rx hits
  revs=$(git rev-list --all 2>/dev/null)
  if [ -z "$revs" ]; then
    echo "  (no commits yet -- history is trivially clean)"
    return
  fi
  for entry in "${PATTERNS[@]}"; do
    label="${entry%%|*}"; rx="${entry#*|}"
    # -G searches added/removed lines across the whole history.
    # The exclusion MUST be a git pathspec, not a grep over the output: `--oneline`
    # prints commit SUBJECTS, so grepping it filtered by commit message and let this
    # script's own files (which necessarily contain every pattern) trip every pattern.
    hits=$(git log --all --oneline -G"$rx" -- . \
             ':(exclude)scripts/scrub_check.sh' \
             ':(exclude)scripts/test_scrub_check.sh' 2>/dev/null)
    if [ -n "$hits" ]; then
      fail=1
      echo "FINDING IN HISTORY [$label]:"
      echo "$hits" | sed 's/^/    /' | head -15
    fi
  done
}

scan_binaries() {
  # Stronger and cheaper than scanning binaries well: a stdlib-only Python repo has no
  # business tracking any binary file, so refuse them all. This closes the whole class
  # rather than the one .pyc that was found -- a compiled artefact can embed a build
  # path, a username or an absolute source path with no visible sign in the diff.
  local bins
  bins=$(git ls-files | grep -Ev "$SELF_EXCLUDE" | while read -r f; do
           [ -f "$f" ] || continue
           # -I makes grep report nothing for a binary file, which is exactly the
           # property used here as a binary DETECTOR rather than as a filter
           if ! grep -qI . "$f" 2>/dev/null; then echo "$f"; fi
         done)
  if [ -n "$bins" ]; then
    fail=1
    echo "FINDING [tracked binary file]: binaries are refused outright in this repo:"
    echo "$bins" | sed 's/^/    /'
  fi

  # History leg: `git log -G` does NOT search binary diffs (measured: 0 commits for a
  # string provably inside a committed .pyc), and `git log -p` prints only
  # "Binary files ... differ". So detect binary blobs STRUCTURALLY via numstat, where
  # a binary change is recorded as "-	-".
  local histbins
  histbins=$(git log --all --numstat --format='%H' 2>/dev/null \
             | awk '/^-\t-\t/ {print $3}' | sort -u \
             | grep -Ev "$SELF_EXCLUDE" || true)
  if [ -n "$histbins" ]; then
    fail=1
    echo "FINDING [binary blob in history]: present in some commit, unreachable to -G:"
    echo "$histbins" | sed 's/^/    /'
    echo "    (deleting these in a NEW commit is not enough -- the blob stays public)"
  fi
}

scan_identities() {
  # Commit METADATA is public too, and file-content scanning cannot see it. A personal
  # author/committer address in history is as public as a leaked path and as permanent.
  local revs bad
  revs=$(git rev-list --all 2>/dev/null)
  if [ -z "$revs" ]; then
    echo "scanning commit identities: (no commits yet)"
    return
  fi
  echo "scanning commit identities (author + committer, all commits)"
  bad=$(git log --all --format='%an <%ae>%n%cn <%ce>' | sort -u \
        | grep -vE '@users\.noreply\.github\.com>$' || true)
  if [ -n "$bad" ]; then
    fail=1
    echo "FINDING [commit identity]: non-noreply identities in history:"
    echo "$bad" | sed 's/^/    /'
  fi
}

case "$MODE" in
  --tree)    scan_tree; scan_binaries ;;
  --history) scan_history; scan_identities; scan_binaries ;;
  --all)     scan_tree; scan_history; scan_identities; scan_binaries ;;
  --help|-h)
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    printf '\nUsage: %s [--tree|--history|--all]\n' "$(basename "$0")"
    exit 0 ;;
  *) echo "unknown mode: $MODE (use --tree|--history|--all|--help)" >&2; exit 2 ;;
esac

if [ "$fail" -eq 0 ]; then
  echo "CLEAN -- no pattern matched. (See the header: this is also what a BROKEN"
  echo "scanner prints, which is why scripts/test_scrub_check.sh plants a decoy per"
  echo "pattern and asserts this script fails on each.)"
  exit 0
fi
echo
echo "SCRUB FAILED -- the findings above must be removed before any push."
exit 1
