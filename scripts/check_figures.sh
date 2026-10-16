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

MIN_PAIRS=24

# figure-string | sample file it must appear in
PAIRS=(
  'session trees: 20|samples/measure_session_footprint.out'
  'summed RSS: 7483 MB|samples/measure_session_footprint.out'
  'summed PSS: 5175 MB|samples/measure_session_footprint.out'
  'PSS/RSS = 0.69|samples/measure_session_footprint.out'
  'p50=350 MB|samples/measure_session_footprint.out'
  'p50=236 MB|samples/measure_session_footprint.out'
  'sessions with any activity in 7d: 42|samples/measure_active_concurrency.out'
  'mean=0.15|samples/measure_active_concurrency.out'
  'busy_minutes=964/10080|samples/measure_active_concurrency.out'
  'ready p50=1.88s|samples/bench_cold_start.out'
  'ready p50=0.65s|samples/bench_cold_start.out'
  'ready p50=2.49s|samples/bench_cold_start.out'
  'min=1.96s  max=3.65s|samples/bench_cold_start.out'
  'min=5.22s  max=6.97s  spread=1.75s|samples/bench_mcp_connect.echo.out'
  'LIVE session cgroups: 20|samples/census_background_work.out'
  'LIVE sessions owning background work older than 900s: 0|samples/census_background_work.out'
  'FLOOR DEMONSTRATION|samples/census_background_work.degraded.out'
  'B_read(26264) == C_read(14848) + A_creation(11416)|samples/bench_cache_residency.out'
  'B_read(26265) == C_read(14848) + A_creation(11417)|samples/bench_cache_residency.run3-redacted.out'
  'PRIMARY (hit-fraction) -> control HOLDS|samples/bench_cache_residency.out'
  'PRIMARY (hit-fraction) -> control FAILED|samples/bench_cache_residency.run3-redacted.out'
  'SELF-TEST FAILED -- do NOT believe the counts above.|samples/census_background_work.selftest-FAILED.out'
  'REFUSING this rep|samples/bench_mcp_connect.broken-server.out'
  'SKIPPED|samples/bench_cold_start.no-binaries.out'
)

# docs/limits.md is in this list deliberately. A review round found four stale figures in
# it while the gate was green, because every declared pair happened to be cited only in
# README or ARTICLE -- the gate's coverage is the list, and the list had a hole exactly
# where the defect lived. limits.md now quotes the SAME pinned strings, so the existing
# pairs cover it; the "cited nowhere in the prose" check is what keeps that true.
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
  # search anywhere under samples/, since some artefacts live in subdirectories
  # (samples/gate-evidence/). Matching only samples/<name> reported a false missing file.
  found=$(find samples -type f -name "$f" -print -quit 2>/dev/null)
  if [ -z "$found" ]; then
    echo "FAIL: prose cites $f which does not exist anywhere under samples/"; fail=1
  elif ! git ls-files --error-unmatch "$found" >/dev/null 2>&1; then
    echo "FAIL: prose cites $found which exists but is NOT TRACKED"; fail=1
  fi
done

# Every engine adapter claiming verified=True must name a tracked sample. Without this,
# flipping one flag makes provenance_banner() assert "VERIFIED (run and output kept)"
# about an adapter whose own note still says "UNTESTED ADAPTER, pattern from
# documentation only" -- and every gate stays green. A reviewer demonstrated exactly
# that. This is the figures rule applied to the other kind of claim.
while IFS=$'\t' read -r name note; do
  [ -z "$name" ] && continue
  named=0
  for f in $(echo "$note" | grep -ohE '[A-Za-z0-9_.-]+\.out' || true); do
    found=$(find samples -type f -name "$f" -print -quit 2>/dev/null)
    if [ -n "$found" ] && git ls-files --error-unmatch "$found" >/dev/null 2>&1; then
      named=1
    fi
  done
  if [ "$named" -eq 0 ]; then
    echo "FAIL: engine adapter '$name' claims verified=True but its note names no"
    echo "      tracked sample file. A verification claim needs an artefact, same as a"
    echo "      figure. Either cite the sample or set verified=False."
    fail=1
  fi
done < <(python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("e", "instruments/engines.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for e in m.ENGINES:
    if e["verified"]:
        print(e["name"] + "\t" + e["note"])
PY
)

if [ "$fail" -eq 0 ]; then
  echo "all $verified figures verified against their artefacts, every cited sample exists,"
  echo "and every verified=True engine adapter names a tracked sample"
  exit 0
fi
echo
echo "FIGURE CHECK FAILED."
exit 1
