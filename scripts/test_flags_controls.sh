#!/usr/bin/env bash
# Does flags.py's self-test actually PROTECT the behaviours flags.py changed?
#
# Round 2 of the ARI#24 review found 13 of 53 mutants green, and named the cause: the
# controls added after round 1 pinned the REVIEWER'S FINDINGS rather than the FIXES. Four
# of six changed behaviours could be reverted with the suite still green, two of them
# losing data outright. The controls are fixed; this script is what stops the gap coming
# back, because the next fix to this file will arrive with the same temptation.
#
# Same shape as scripts/test_scrub_check.sh, which plants a decoy per scrub pattern and
# asserts the gate FAILS on each: a gate that has only ever been seen passing is
# indistinguishable from a gate that cannot fail.
#
# Each case below REVERTS one fix this branch made -- back to the code that was actually
# there, not to arbitrary damage -- and asserts the self-test turns RED, naming the check
# that caught it. A case that leaves the suite green is reported as a COVERAGE GAP.
#
# The mutations are applied to a COPY of instruments/ in a temp directory. The real tree is
# never written to, so an interrupted run cannot leave a mutant behind -- which the
# in-place approach this was modelled on can.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"
exec "$PY" "$ROOT/scripts/test_flags_controls.py" "$@"
