#!/usr/bin/env bash
# Does the fixture-freshness gate actually FAIL when a fixture is stale?
#
#     bash scripts/test_fixture_freshness.sh
#
# A gate is only evidence if it can fail, and a gate that has only ever been observed
# passing is indistinguishable from one that cannot fail at all. check_figures.sh has
# scripts/test_scrub_check.sh's equivalent (a planted decoy per pattern); this is that, for
# ARI#22's gate.
#
# LEG 2 IS THE ONE THAT MATTERS, and it is a REAL replay, not a synthetic drift: it restores
# the exact bytes samples/measure_token_breakdown.no-bash-turns.out had at commit ee705ad,
# the conflict-free-but-stale state ARI#22 was filed about, and requires the gate to fail and
# to NAME that file. A hand-made two-line edit would prove the diff machinery works; it would
# not prove the gate catches what actually got past four other gates.
#
# All mutations are made in the working tree and undone by a trap on EXIT/INT/TERM, and the
# test asserts at the end that the tree it leaves behind is the tree it was given. It refuses
# to start if the relevant paths are already dirty, because then "restored" has no meaning.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

GATE=scripts/check_fixtures_fresh.sh
REGEN=scripts/regen_samples.sh
SAMPLE=samples/measure_token_breakdown.no-bash-turns.out
REAL_STALE=scripts/testdata/fixture-freshness/no-bash-turns.pre-ari16.out
NEWFILE=samples/measure_token_breakdown.__freshness_test__.out

PASS=0; FAIL=0
ok()   { echo "  ok   -- $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL -- $*" >&2; FAIL=$((FAIL+1)); }

for f in "$GATE" "$REGEN" "$SAMPLE" "$REAL_STALE"; do
  [ -f "$f" ] || { echo "test_fixture_freshness: missing $f" >&2; exit 2; }
done

# "Restored" is only a meaningful claim if these started clean.
if ! git diff --quiet -- "$SAMPLE" "$REGEN" || [ -e "$NEWFILE" ]; then
  echo "test_fixture_freshness: REFUSING -- $SAMPLE or $REGEN is already modified, or" >&2
  echo "$NEWFILE already exists. This test mutates and restores them, and cannot tell" >&2
  echo "its own mutation from a pre-existing one." >&2
  exit 2
fi

BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/freshness-test.XXXXXX")"
cp "$REGEN" "$BACKUP/regen.bak"
restore() {
  git checkout --quiet -- "$SAMPLE" 2>/dev/null || true
  cp "$BACKUP/regen.bak" "$REGEN" 2>/dev/null || true
  git rm --quiet --cached --force "$NEWFILE" >/dev/null 2>&1 || true
  rm -f "$NEWFILE"
  rm -rf "$BACKUP"
}
trap restore EXIT INT TERM

echo "FIXTURE-FRESHNESS GATE -- does it fail when it should?"

# --- LEG 1: a clean tree must PASS. ------------------------------------------------------
# Without this, every other leg is satisfied by a gate that simply always fails.
if bash "$GATE" >/dev/null 2>&1; then
  ok "clean tree: gate passes (so the failures below are caused by the mutation)"
else
  bad "clean tree: gate FAILED before anything was mutated -- nothing below is meaningful"
fi

# --- LEG 2: the REAL ARI#16/ARI#17 drift. ------------------------------------------------
cp "$REAL_STALE" "$SAMPLE"
OUT="$(bash "$GATE" 2>&1)"; RC=$?
if [ "$RC" -ne 0 ]; then
  ok "real ARI#16/#17 stale bytes (ee705ad): gate fails, exit $RC"
  case "$OUT" in
    *measure_token_breakdown.no-bash-turns.out*)
      ok "real drift: the failure NAMES the stale file" ;;
    *)  bad "real drift: gate failed but never named $SAMPLE, so a reader cannot act on it" ;;
  esac
  case "$OUT" in
    *"mode 4b"*) ok "real drift: the diff of the missing lines is shown, not just a verdict" ;;
    *)           bad "real drift: no diff shown; a bare 'something changed' is hard to act on" ;;
  esac
else
  bad "real ARI#16/#17 stale bytes: gate PASSED. This is the exact defect it exists to catch."
fi
git checkout --quiet -- "$SAMPLE"

# --- LEG 3: a NEW sample with no recipe. -------------------------------------------------
# The real incident's stale file was NEW on its branch. A gate that silently skips samples it
# has no recipe for would skip precisely those, so being unaccounted-for must itself fail.
printf 'this file has no recipe\n' > "$NEWFILE"
git add --intent-to-add -- "$NEWFILE" >/dev/null
OUT="$(bash "$GATE" 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && [ "${OUT#*__freshness_test__}" != "$OUT" ]; then
  ok "a tracked sample with no recipe and no declared reason: gate fails and names it"
else
  bad "unaccounted sample: gate exit $RC -- a new fixture can enter unverified"
fi
git rm --quiet --cached --force "$NEWFILE" >/dev/null 2>&1 || true
rm -f "$NEWFILE"

# --- LEG 4: what would it print if it were BLIND? ----------------------------------------
# Replace the recipe runner with one that lists everything and regenerates NOTHING. A gate
# that loops over generated files would find no mismatches and report success.
cat > "$REGEN" <<STUB
#!/usr/bin/env bash
# Lists exactly what the real table lists, so the accounting checks still pass -- and then
# regenerates nothing, which is the only thing being tested here.
case "\${1:-}" in
  --list|--list-unreproducible) bash "$BACKUP/regen.bak" "\$1" ;;
  *) mkdir -p "\$1" ;;
esac
STUB
OUT="$(bash "$GATE" 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && [ "${OUT#*recipes}" != "$OUT" ]; then
  ok "blind regeneration (recipes list files, produce none): gate fails on the count check"
elif [ "$RC" -ne 0 ]; then
  bad "blind regeneration: gate failed, but not on the file-count check -- so the"
  bad "  anti-blindness guard is not what stopped it"
else
  bad "blind regeneration: gate PASSED while verifying nothing at all"
fi
cp "$BACKUP/regen.bak" "$REGEN"

# --- LEG 5: the tree is as it was found. -------------------------------------------------
if git diff --quiet -- "$SAMPLE" "$REGEN" && [ ! -e "$NEWFILE" ]; then
  ok "every mutation was undone"
else
  bad "the test left the working tree modified -- restore is broken"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "FIXTURE-FRESHNESS SELF-TEST PASSED -- $PASS checks, including a real replay of the"
  echo "ARI#16/ARI#17 drift and a blind-regeneration control."
  exit 0
fi
echo "FIXTURE-FRESHNESS SELF-TEST FAILED -- $FAIL of $((PASS+FAIL)) checks" >&2
exit 1
