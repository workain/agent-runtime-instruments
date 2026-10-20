#!/usr/bin/env bash
# FIXTURE FRESHNESS GATE (ARI#22). Regenerate every reproducible sample and require it to
# match the committed copy byte-for-byte.
#
#     bash scripts/check_fixtures_fresh.sh
#
# This is the only check in this repository that compares an artefact to the CODE. Every
# other one compares artefacts to each other, to a pinned substring, or to themselves:
#
#   * check_figures.sh asserts a figure quoted in prose appears in some tracked artefact.
#     A fixture that is merely OUT OF DATE still contains its pinned substring, so it
#     passes -- which is exactly what happened.
#   * a self-test asserts the code is self-consistent, and says nothing about files on disk.
#   * git asserts two branches' versions of a file can be combined without conflict, which
#     is not the same claim as either version being right.
#
# THE TWO REAL INCIDENTS THIS EXISTS FOR, both from CONFLICT-FREE merges:
#
#   1. ARI#16 changed the instrument's output and regenerated the six fixtures that existed
#      on its branch. ARI#17 ADDED a seventh, samples/measure_token_breakdown.no-bash-turns.out.
#      When ARI#16 landed first, there was no copy of that file on its side for git to merge
#      against, so ARI#17 carried its pre-ARI#16 capture forward untouched: no conflict, two
#      lines stale, and all four gates green. scripts/test_fixture_freshness.sh replays this
#      one against the REAL bytes.
#   2. Earlier, a clean merge produced a measure_token_breakdown.json.out holding one
#      branch's header above another branch's body. Nothing flagged it either.
#
# Note what both have in common: the merge was clean. A conflict is a question git asks you;
# this class of defect is the case where it had no question to ask.
#
# WHAT WOULD THIS PRINT IF IT WERE BLIND? If the regeneration silently produced nothing --
# wrong interpreter, a recipe table that stopped matching, an empty output directory -- a
# naive loop over the generated files would find no mismatches and report success. So the
# count of regenerated files is checked against the recipe count before any comparison is
# believed, and a shortfall is a FAILURE rather than a clean run. A gate that cannot fail
# while blind is not evidence of anything.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REGEN=scripts/regen_samples.sh

fail() { echo "FIXTURE FRESHNESS: FAILED -- $*" >&2; exit 1; }

[ -f "$REGEN" ] || fail "$REGEN is missing; the recipes are what this gate runs"

mapfile -t TRACKED   < <(git ls-files 'samples/*.out' | sed 's|^samples/||' | sort)
mapfile -t COVERED   < <(bash "$REGEN" --list | sort)
mapfile -t DECLARED  < <(bash "$REGEN" --list-unreproducible | cut -f1 | sort)

[ "${#TRACKED[@]}" -gt 0 ] || fail "no tracked samples/*.out found at all -- refusing to
         report success from an empty inventory"

# --- 1. Every tracked sample must be accounted for, in exactly one list. -----------------
# This is the check that would have caught incident 1's SHAPE going forward: the stale file
# was NEW, and a gate that quietly skips files it has no recipe for would skip exactly those.
UNACCOUNTED=$(comm -23 <(printf '%s\n' "${TRACKED[@]}") \
                       <(printf '%s\n' "${COVERED[@]}" "${DECLARED[@]}" | sort))
if [ -n "$UNACCOUNTED" ]; then
  echo "FIXTURE FRESHNESS: FAILED -- these tracked samples are in neither list:" >&2
  printf '         %s\n' $UNACCOUNTED >&2
  echo "         Add a recipe to $REGEN, or declare it unreproducible WITH A REASON." >&2
  echo "         A sample nobody can regenerate is not verifiable by anything here." >&2
  exit 1
fi

BOTH=$(comm -12 <(printf '%s\n' "${COVERED[@]}") <(printf '%s\n' "${DECLARED[@]}"))
[ -z "$BOTH" ] || fail "listed as BOTH reproducible and unreproducible: $(echo $BOTH)"

# --- 2. Neither list may name a sample that no longer exists. ---------------------------
GHOSTS=$(comm -13 <(printf '%s\n' "${TRACKED[@]}") \
                  <(printf '%s\n' "${COVERED[@]}" "${DECLARED[@]}" | sort))
if [ -n "$GHOSTS" ]; then
  echo "FIXTURE FRESHNESS: FAILED -- listed but not tracked (stale table):" >&2
  printf '         %s\n' $GHOSTS >&2
  exit 1
fi

# --- 3. Regenerate and compare. ----------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/fixture-fresh.XXXXXX")" || fail "could not mktemp"
trap 'rm -rf "$WORK"' EXIT

bash "$REGEN" "$WORK" || fail "regeneration itself failed; see the output above.
         Nothing about the committed fixtures has been proven either way."

GENERATED=$(find "$WORK" -maxdepth 1 -name '*.out' | wc -l | tr -d ' ')
[ "$GENERATED" -eq "${#COVERED[@]}" ] \
  || fail "regeneration produced $GENERATED files but there are ${#COVERED[@]} recipes.
         Refusing to compare a partial run: with files missing, every remaining
         comparison could pass and the gate would look clean while measuring almost
         nothing."

DRIFTED=()
for b in "${COVERED[@]}"; do
  [ -f "$WORK/$b" ] || { DRIFTED+=("$b (not regenerated)"); continue; }
  cmp -s "samples/$b" "$WORK/$b" || DRIFTED+=("$b")
done

if [ "${#DRIFTED[@]}" -ne 0 ]; then
  echo "FIXTURE FRESHNESS: FAILED -- ${#DRIFTED[@]} committed fixture(s) do not match what" >&2
  echo "the code now produces:" >&2
  printf '         %s\n' "${DRIFTED[@]}" >&2
  for b in "${DRIFTED[@]}"; do
    [ -f "$WORK/$b" ] || continue
    echo >&2; echo "--- diff: samples/$b (committed) vs regenerated ---" >&2
    diff "samples/$b" "$WORK/$b" >&2 | head -40
  done
  echo >&2
  echo "Fix by REGENERATING -- bash scripts/regen_samples.sh --write -- and committing the" >&2
  echo "result. Do not hand-edit a fixture to match: a capture that was typed rather than" >&2
  echo "captured is no longer evidence of anything the instrument did." >&2
  exit 1
fi

echo "FIXTURE FRESHNESS: ${#COVERED[@]} of ${#TRACKED[@]} tracked samples regenerated and"
echo "byte-identical to the committed copies."
echo
echo "NOT VERIFIED -- ${#DECLARED[@]} samples cannot be reproduced from code plus a committed"
echo "fixture, each for a stated reason (this list is printed, not hidden: an unverified"
echo "file that nobody mentions reads as a verified one):"
bash "$REGEN" --list-unreproducible | while IFS=$'\t' read -r b why; do
  printf '  %-46s %s\n' "$b" "$why"
done
