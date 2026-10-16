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
