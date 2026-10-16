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

# EVERY measurement-SHAPED span in EVERY prose file must exist in some artefact,
# BACKTICKED OR NOT, or be exempted below with a reason.
#
# Two review rounds established why this has to be the default rather than a filter.
#
# (a) Declared pairs ask whether a figure is cited SOMEWHERE in the prose, so any one
#     file satisfies a pair. Drifting only one file's copy left the gate green with
#     nonsense in it.
# (b) An earlier version of this leg only looked at BACKTICKED spans with a unit or an
#     `=`. So `103 transcripts` and `sessions with any activity in 7d: 35` survived four
#     commits unnoticed -- and when the prose was finally corrected, the figures that got
#     fixed were exactly the ones this gate could see, and no others. THE GATE'S BLIND
#     SPOT AND THE SURVIVING DRIFT WERE THE SAME SET. That is the failure mode to design
#     against: when the correction signal IS the gate, its coverage becomes the ceiling
#     on the prose's accuracy.
#
# So the default is inverted: anything shaped like a measurement must match an artefact,
# and every exemption is listed with a reason. The exemption list is long and that is the
# deliberate trade -- paid once, after which a stale figure cannot enter silently in any
# shape.
#
# samples/gate-evidence/ is EXCLUDED from the search. See the note at that directory:
# it records what a GATE printed, including the stale figures a round was fixing, so
# searching it makes every previously-wrong figure permanently findable. Measured: with
# it included, both `spread=0.85s` and `activity in 7d: 35` passed this leg on the
# strength of a file whose purpose is to record them as WRONG.
EXEMPT=(
  # --- not measurements: versions, env vars, parameters, mount options ---
  '2.1.197' '2.1.221' '0.147.0' 'SELF_TEST=0' 'SKIP_PAID=1' 'min_age=1' 'hidepid=2'
  'SELF_TEST_AGE_GAP_S' 'MCP_TIMEOUT' '900s' '900 s' '10 s' '2 s'
  # --- figures quoted from THIRD-PARTY sources; no artefact here by construction, and
  # --- each is attributed at its use site (docs/citations.md) ---
  '238 MB' '340 MB' '0.1x' '0.025x' '0.05x' '1.25x' '20 blocks' '5-minute' '1-hour'
  '30 seconds' '25-58 ms' '58 ms' '1.05-2.42 s' '2.42 s' '1.6 s' '26 s'
  # --- measured by `stat`, not by an instrument here, and used only as a contrast ---
  '245,517,112 bytes'
  # --- HISTORICAL readings, explicitly labelled in the prose as uncommitted asides:
  # --- earlier boot p50s on a quieter box, and the +3s stdin artefact's own figures ---
  '1.15 s' '2.38 s' '4.35 s' '3.48 s' '4.04 s'
  # --- HYPOTHETICAL blind output. These MUST NOT exist in an artefact: the passages
  # --- quoting them exist to say "this is what a blind run prints". If one ever appears
  # --- under samples/, that is a finding about the instrument, not about this list. ---
  'mean=0.00' 'session trees: 0' 'owning background work: 0'
  # --- prose glosses of a pinned figure; the pinned form is what the pairs assert ---
  '16 GB'               # gloss of `MemTotal: 15987 MB`
  '323 MB'              # gloss of the pinned `323021104 bytes = 323 MB decimal = 308 MiB`
  '0.65 s'              # gloss of the pinned `ready p50=0.65s`
  # --- the A/B/C cache table re-spaced for readability; the pinned identity line
  # --- `B_read(26264) == C_read(14848) + A_creation(11416)` is what guards these ---
  'read=14848  creation=11416' 'read=14848  creation=14307' 'read=26264  creation=2875'
  'read=14848   creation=11416' 'read=14848   creation=14307' 'read=26264   creation=2875'
  # --- extractor artefacts: prose fragments that merely contain a digit ---
  'on n=2,' 'on n=2.' 'The caching result is n=1' 'timings too: 24'
  # --- counts belonging to the SOURCE STUDY's corpus, not to this repo's samples.
  # --- docs/limits.md names them precisely to stop the two corpora being confused. ---
  '146 transcripts' '97 sessions'
  # --- counts quoted from the instruments' own defect history (v1/v2/v3 answers) ---
  '16 sessions' '15 cgroups' '15 live'
  # --- the WRONG figure, quoted in docs/method.md as the example of the defect it
  # --- caused. It must not match an artefact: that is the point of quoting it. Caught
  # --- by this gate on its first run against the paragraph describing it, which is the
  # --- gate working rather than a problem. ---
  '103 transcripts'
  # --- extractor artefact: prose containing "404'd" matches the label:N shape ---
  "blob the GitHub API 404'd was still served by"
  'returns 0 bytes, so'
  'v1 | "0 of 16 sessions own background work" | Its filter discarded'
  'v3 | "15 live session cgroups" | Discovery used the broad "is this agent-internal?" pattern, which matches a bare'
)
is_exempt() {
  local f="$1" n
  for n in "${EXEMPT[@]}"; do [ "$f" = "$n" ] && return 0; done
  return 1
}

for pf in "${PROSE[@]}"; do
  while IFS= read -r fig; do
    [ -z "$fig" ] && continue
    is_exempt "$fig" && continue
    # a sample FILENAME is not a figure; the cited-artefact leg above already asserts
    # each one exists and is tracked. Exempting them by name here would need an edit
    # every time a sample is added, which is how an exemption list rots.
    case "$fig" in *.out) continue;; esac
    if ! grep -rqF --exclude-dir=gate-evidence -- "$fig" samples/ 2>/dev/null; then
      echo "FAIL: $pf states '$fig' which appears in NO artefact under samples/."
      echo "      Either it drifted, or it is not a measurement and belongs in EXEMPT"
      echo "      in this script WITH A REASON."
      fail=1
    fi
  done < <( { grep -ohE '`[^`]*[0-9][^`]*`' "$pf" | sed 's/^`//; s/`$//'
              grep -ohE '[A-Za-z][A-Za-z0-9_ ()<=-]{2,34}[:=] ?[0-9][0-9.,/]*' "$pf"
              grep -ohE '\b[0-9][0-9.,]* ?(MB|MiB|GB|KB|ms|bytes)\b' "$pf"
              grep -ohE '\b[0-9]+[0-9.,]* ?s\b' "$pf"
              # NUMBER-FIRST counts, e.g. `103 transcripts`. Restricted to the nouns the
              # instruments actually report, rather than any `<digits> <word>`: the
              # generic form returned 25 spans of pure prose noise ("0 makes", "13 that")
              # and an exemption list of that shape rots into a dumping ground. Naming
              # the quantities keeps every entry meaningful. This is the shape that let
              # `103 transcripts` survive four commits with no gate noticing.
              grep -ohE '\b[0-9][0-9.,]* (transcripts|sessions|session trees|trees|cgroups|processes|procs|lines)\b' "$pf"
            } 2>/dev/null | sed 's/^ *//; s/ *$//' | sort -u )
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
