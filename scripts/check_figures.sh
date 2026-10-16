#!/usr/bin/env bash
# Every load-bearing figure in the prose must still appear in the artefact it came from.
#
# WHY THIS EXISTS. The prose was written, then the samples were regenerated, and the box's
# load had changed underneath. 13 of 14 figures in README.md and ARTICLE.md silently stopped
# matching their sample files -- "21 session trees / 6837 MB RSS" against a committed sample
# saying "19 / 7505". Nothing caught it but a hand check. A figure in prose is a claim about
# a file; this makes that claim checkable.
#
# WHAT WOULD THIS PRINT IF IT WERE BLIND?
#   "all N figures verified" -- the same thing it prints when everything genuinely matches.
#   Two guards, because this script is itself the kind of gate it exists to enforce:
#     1. If a FIGURE string is absent from the prose, that is a FAILURE, not a skip. An
#        entry that matches nothing in either direction would otherwise sit here forever
#        looking like coverage while checking nothing.
#     2. The pair count is asserted against a declared minimum, so silently deleting
#        entries to make it pass is itself a failure.
#   It cannot tell you about a figure nobody listed here. That is a real limit: this gate
#   is only as complete as the list, and the list is maintained by hand.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2

MIN_PAIRS=22

# figure-string | sample file it must appear in
PAIRS=(
  'session trees: 19|samples/measure_session_footprint.out'
  'summed RSS: 7505 MB|samples/measure_session_footprint.out'
  'summed PSS: 6302 MB|samples/measure_session_footprint.out'
  'PSS/RSS = 0.84|samples/measure_session_footprint.out'
  'p50=331 MB|samples/measure_session_footprint.out'
  'p50=254 MB|samples/measure_session_footprint.out'
  'sessions with any activity in 7d: 35|samples/measure_active_concurrency.out'
  'mean=0.14|samples/measure_active_concurrency.out'
  'busy_minutes=931/10080|samples/measure_active_concurrency.out'
  'ready p50=4.35s|samples/bench_cold_start.out'
  'ready p50=0.98s|samples/bench_cold_start.out'
  'ready p50=3.31s|samples/bench_cold_start.out'
  'min=6.83s  max=7.68s  spread=0.85s|samples/bench_mcp_connect.echo.out'
  'LIVE sessions owning background work older than 900s: 0|samples/census_background_work.out'
  'LIVE session cgroups: 12|samples/census_background_work.out'
  'B_read(26264) == C_read(14848) + A_creation(11416)|samples/bench_cache_residency.out'
  'B_read(26265) == C_read(14848) + A_creation(11417)|samples/bench_cache_residency.run3-redacted.out'
  "'read': 26264, 'create': 2875|samples/bench_cache_residency.run2-redacted.out"
  'PRIMARY (hit-fraction) -> control HOLDS|samples/bench_cache_residency.out'
  'PRIMARY (hit-fraction) -> control FAILED|samples/bench_cache_residency.run3-redacted.out'
  'SELF-TEST FAILED -- do NOT believe the counts above.|samples/census_background_work.selftest-FAILED.out'
  'REFUSING this rep|samples/bench_mcp_connect.broken-server.out'
  'SKIPPED|samples/bench_cold_start.no-binaries.out'
)

PROSE=(README.md ARTICLE.md docs/method.md docs/limits.md)

if [ "${#PAIRS[@]}" -lt "$MIN_PAIRS" ]; then
  echo "SCRUB-ERROR: only ${#PAIRS[@]} pairs declared, minimum is $MIN_PAIRS." >&2
  echo "Deleting entries to make this pass is itself a failure." >&2
  exit 2
fi

fail=0; verified=0
for entry in "${PAIRS[@]}"; do
  fig="${entry%%|*}"; file="${entry#*|}"
  if [ ! -f "$file" ]; then
    echo "FAIL: artefact missing: $file  (figure: $fig)"; fail=1; continue
  fi
  if ! grep -qF -- "$fig" "$file"; then
    echo "FAIL: figure NOT in its artefact"
    echo "      figure:   $fig"
    echo "      artefact: $file"
    fail=1; continue
  fi
  # guard 1: the figure must actually be cited in the prose, or this entry checks nothing
  if ! grep -qrF -- "$fig" "${PROSE[@]}" 2>/dev/null; then
    echo "FAIL: figure is in its artefact but cited NOWHERE in the prose -- this entry is"
    echo "      coverage theatre. Either cite it or remove it deliberately."
    echo "      figure: $fig"
    fail=1; continue
  fi
  verified=$((verified+1))
done

# every sample file cited by name in the prose must exist and be tracked
for f in $(grep -rhoE '[A-Za-z0-9_.-]+\.out' "${PROSE[@]}" | sort -u); do
  if [ ! -f "samples/$f" ]; then
    echo "FAIL: prose cites samples/$f which does not exist"; fail=1
  elif ! git ls-files --error-unmatch "samples/$f" >/dev/null 2>&1; then
    echo "FAIL: prose cites samples/$f which exists but is NOT TRACKED"; fail=1
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "all $verified figures verified against their artefacts, and every cited sample exists"
  exit 0
fi
echo
echo "FIGURE CHECK FAILED."
exit 1
