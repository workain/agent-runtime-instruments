#!/usr/bin/env bash
# Run every gate and FAIL THE SHELL if any of them fails. Use this before committing.
#
# Why it exists: a commit was pushed with check_figures.sh exiting 1, because the command
# that ran it printed the exit code and then committed anyway -- a check whose output
# nobody acts on, which is the exact failure mode these gates exist to prevent. `set -e`
# here makes the result load-bearing instead of informational.
#
# Install as a pre-commit hook:  ln -sf ../../scripts/preflight.sh .git/hooks/pre-commit
set -euo pipefail

# Two things this file has to get right before any gate runs, and the obvious one-liner
# (`cd "$(dirname "${BASH_SOURCE[0]}")/.."`) gets both wrong when run as a HOOK -- which
# is the installation the lines above recommend.
#
# 1. git EXPORTS GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE into a hook, and GIT_DIR
#    OVERRIDES directory discovery for every later git call. The gate suite makes real
#    commits inside its own throwaway clones (scripts/test_identity_allowlist.sh plants
#    one per control); with GIT_DIR still set, those land in THIS repository instead --
#    which fires the pre-commit hook, which runs this file, which plants again. Observed
#    as an infinite recursion that had to be killed, with ~36 nested runs.
# 2. Invoked through a symlink, `${BASH_SOURCE[0]}` is the symlink's own path --
#    `.git/hooks/pre-commit` -- so `dirname/..` is `.git`, and every gate below fails
#    with "No such file or directory". That hook could never have passed. And in a linked
#    WORKTREE the script's directory belongs to the MAIN checkout, so resolving by it
#    would gate the wrong tree: a worktree's commit checked against another tree's files.
#
# So: ask git which tree is being committed, and fall back to the script's own directory
# only when the current directory is not inside this repository at all.
unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_OBJECT_DIRECTORY GIT_QUARANTINE_PATH
root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$root" ] || [ ! -f "$root/scripts/check_figures.sh" ]; then
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"
fi
if [ -z "$root" ] || [ ! -f "$root/scripts/check_figures.sh" ]; then
  echo "preflight: cannot find this repository's root, so no gate below can run." >&2
  echo "  A gate that cannot locate what it checks does not get to pass. Exiting 2." >&2
  exit 2
fi
cd "$root"
echo "preflight: figures"   && bash scripts/check_figures.sh    >/dev/null
echo "preflight: scrub"     && bash scripts/scrub_check.sh --all >/dev/null
echo "preflight: decoys"    && bash scripts/test_scrub_check.sh  >/dev/null
echo "preflight: identity"  && bash scripts/test_identity_allowlist.sh >/dev/null
echo "preflight: fixtures"  && bash scripts/check_fixtures_fresh.sh >/dev/null
echo "preflight: fixtures-gate" && bash scripts/test_fixture_freshness.sh >/dev/null
# Reverting any fix flags.py makes must turn one of its own controls RED. Review round 2
# found 13 of 53 mutants green because the controls pinned the REVIEWER'S FINDINGS and not
# the FIXES -- four changed behaviours were revertible with the suite green, two of them
# losing data. This gate is what stops that recurring on the next fix.
echo "preflight: revert-each-fix" && bash scripts/test_flags_controls.sh >/dev/null
# The same gate for the request-construction pair. A second gate rather than a second
# table inside the first: the two mutate different files and their baselines are
# different self-tests, and a shared runner would have to be taught both before either
# could be added -- which is how the newer one ends up unguarded.
echo "preflight: revert-each-fix (request construction)" \
  && bash scripts/test_request_construction_controls.sh >/dev/null
# An instrument's own positive/negative controls are a gate, not documentation: they are
# the only thing standing between a silently-wrong number and a published one. Every
# instrument that ships a --self-test runs here. Add the flag to an instrument and it is
# picked up automatically -- nobody has to remember to edit this list.
for inst in instruments/*.py; do
  grep -q -- '"--self-test"' "$inst" || continue
  echo "preflight: self-test $(basename "$inst")" && python3 "$inst" --self-test >/dev/null
done
echo "preflight: OK -- all gates pass"
echo
echo "NOTE: run as a pre-COMMIT hook, this cannot see two things, because they do not"
echo "exist yet when it runs: the new commit's AUTHOR IDENTITY and its blobs in history."
echo "Both are checked by scrub_check.sh --all, so install it as pre-push too:"
echo "  ln -sf ../../scripts/scrub_check.sh .git/hooks/pre-push   # with --all"
echo "This gap is not theoretical: a commit was pushed twice carrying an identity the"
echo "pre-commit run had no way to inspect, because the env-var prefix in"
echo "\`VAR=x git add -A && git commit\` applies only to the FIRST command."
