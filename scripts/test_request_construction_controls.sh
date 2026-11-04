#!/usr/bin/env bash
# Do the request-construction instruments' own self-tests actually PROTECT the behaviours
# those files rely on?
#
# Same gate, same reasoning, as scripts/test_flags_controls.sh -- see that file's header.
# The short version: on this repository, 13 of 53 mutants once left a self-test green,
# because the checks pinned the reviewer's findings rather than the fixes. A new
# instrument therefore arrives WITH this gate instead of acquiring one after the next
# review round finds the same hole again.
#
# Each case reverts one behaviour to the simpler implementation somebody would really
# write -- a three-column decomposition, framing defined as the remainder, a header dump,
# a faithfulness check that cannot fail -- and requires a control to turn RED.
#
# Mutations are applied to a COPY of instruments/ in a temp directory; the real tree is
# never written to, so an interrupted run cannot leave a mutant behind.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"
exec "$PY" "$ROOT/scripts/test_request_construction_controls.py" "$@"
