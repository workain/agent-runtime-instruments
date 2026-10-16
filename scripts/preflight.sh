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
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "preflight: figures"   && bash scripts/check_figures.sh    >/dev/null
echo "preflight: scrub"     && bash scripts/scrub_check.sh --all >/dev/null
echo "preflight: decoys"    && bash scripts/test_scrub_check.sh  >/dev/null
echo "preflight: OK -- all gates pass"
echo
echo "NOTE: run as a pre-COMMIT hook, this cannot see two things, because they do not"
echo "exist yet when it runs: the new commit's AUTHOR IDENTITY and its blobs in history."
echo "Both are checked by scrub_check.sh --all, so install it as pre-push too:"
echo "  ln -sf ../../scripts/scrub_check.sh .git/hooks/pre-push   # with --all"
echo "This gap is not theoretical: a commit was pushed twice carrying an identity the"
echo "pre-commit run had no way to inspect, because the env-var prefix in"
echo "\`VAR=x git add -A && git commit\` applies only to the FIRST command."
