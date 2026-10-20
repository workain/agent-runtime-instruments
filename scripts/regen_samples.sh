#!/usr/bin/env bash
# The recipe for every sample this repository can REPRODUCE, and the declared reason for
# every sample it cannot. This file is the single source of truth for both, so that
# `check_fixtures_fresh.sh` and a human regenerating a fixture by hand cannot disagree
# about what produced it.
#
#     bash scripts/regen_samples.sh --list                  # reproducible basenames
#     bash scripts/regen_samples.sh --list-unreproducible   # the rest, each with a reason
#     bash scripts/regen_samples.sh <outdir>                # regenerate into <outdir>
#     bash scripts/regen_samples.sh --write                 # regenerate IN PLACE (samples/)
#
# WHY THIS EXISTS AS A COMMITTED FILE. Until ARI#22 these recipes were tribal knowledge:
# they lived in a scratch script under /tmp belonging to whoever last regenerated a
# fixture. A fixture whose producing command nobody can name is not a reproducible
# artefact, it is a file that happens to look like output. Committing the recipes is what
# makes a freshness gate possible at all, because the gate's whole job is to re-run them.
#
# WHAT BELONGS IN WHICH LIST. A sample belongs in RECIPES only if its content is a pure
# function of (this repository's code + a committed fixture). A sample that captures
# anything about the machine or the moment it ran -- a pid, an RSS figure, a wall-clock
# timing, a CLI version, a paid API response -- can never match on a re-run, and pretending
# to verify it would mean a gate that fails for honest reasons until someone switches it
# off. Those go in UNREPRODUCIBLE with the specific reason. The gate asserts every tracked
# sample appears in exactly one of the two lists, which is the part that matters: the real
# ARI#16/ARI#17 drift was in a fixture that was NEW on its branch, so a gate that silently
# ignores files it has no recipe for would have missed the very case it was built for.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

I=instruments/measure_token_breakdown.py

# Each entry: <basename-without-samples/> <TAB> <mode> <TAB> <argv...>
#
# argv is LITERAL -- no variables, no globs, no shell expansion of any kind -- so
# reproducing a sample never depends on a shell feature and this file needs no `eval`.
# That makes the table longer to read than an abbreviated one; it also makes it exactly
# what ran.
#
#   run  -- stdout+stderr captured; the run is expected to succeed
#   refu -- a run whose EXIT CODE is part of what the sample documents: `exit=N` is
#           appended to the capture, so a refusal that stopped refusing would drift
#   env  -- like refu, with one leading VAR=VALUE applied to the process
RECIPES=$(cat <<'EOF'
measure_token_breakdown.out	run	samples/fixtures/token_breakdown.jsonl
measure_token_breakdown.json.out	run	--json --manifest samples/fixtures/token_breakdown.manifest.json
measure_token_breakdown.bash.out	run	samples/fixtures/token_breakdown.bash.jsonl
measure_token_breakdown.other.out	run	samples/fixtures/token_breakdown.other.jsonl
measure_token_breakdown.no-bash-turns.out	run	samples/fixtures/token_breakdown.no-bash-turns.jsonl
measure_token_breakdown.batch.out	run	samples/fixtures/token_breakdown.jsonl samples/fixtures/token_breakdown.child.jsonl
measure_token_breakdown.subsession.out	run	--manifest samples/fixtures/token_breakdown.manifest.json
measure_token_breakdown.user-entry-census.out	run	--survey-user-entries samples/fixtures/loop_flags.flagged.jsonl samples/fixtures/loop_flags.clean.jsonl samples/fixtures/token_breakdown.jsonl
measure_token_breakdown.loop-flags-FLAGGED.out	run	--flag-span-tool-calls 5 --flag-no-tool-streak 3 samples/fixtures/loop_flags.flagged.jsonl
measure_token_breakdown.loop-flags-clean.out	run	--flag-span-tool-calls 5 --flag-no-tool-streak 3 samples/fixtures/loop_flags.clean.jsonl
measure_token_breakdown.loop-flags-no-user-turn.out	run	--flag-span-tool-calls 5 --flag-no-tool-streak 3 samples/fixtures/loop_flags.no-user-turn.jsonl
measure_token_breakdown.loop-flags-no-threshold.out	run	samples/fixtures/loop_flags.flagged.jsonl
measure_token_breakdown.loop-flags.json.out	run	--json --flag-span-tool-calls 5 --flag-no-tool-streak 3 samples/fixtures/loop_flags.flagged.jsonl
measure_token_breakdown.selftest.out	refu	--self-test
measure_token_breakdown.no-transcript.out	refu	samples/fixtures/token_breakdown.missing.jsonl
measure_token_breakdown.corrupt-fixture-REFUSED.out	refu	samples/fixtures/token_breakdown.corrupt.jsonl
measure_token_breakdown.one-field-renamed-REFUSED.out	refu	samples/fixtures/token_breakdown.corrupt-one-field.jsonl
measure_token_breakdown.loop-flags-bad-env-REFUSED.out	env	ARI_FLAG_SPAN_TOOL_CALLS=twenty samples/fixtures/loop_flags.flagged.jsonl
measure_token_breakdown.loop-flags-threshold-zero-REFUSED.out	env	ARI_FLAG_SPAN_TOOL_CALLS=0 samples/fixtures/loop_flags.flagged.jsonl
EOF
)

# Each entry: <basename> <TAB> <reason>. The reason is not decoration -- it is the argument
# for why NOT verifying this file is honest rather than convenient, and a reviewer should be
# able to disagree with a specific one. "Captures a live measurement" and "costs a paid
# request" are real reasons; "nobody got round to it" would not be.
UNREPRODUCIBLE=$(cat <<'EOF'
bench_cache_residency.out	~6 paid API requests, and its token counts are one live run's
bench_cache_residency.run1-redacted.out	a historical run whose own header states it was NOT produced by the published code (ids redacted post-hoc)
bench_cache_residency.run2-redacted.out	a historical run, redacted post-hoc, same as run1
bench_cache_residency.run3-redacted.out	a historical run, redacted post-hoc, same as run1
bench_cold_start.out	wall-clock timings and peak RSS of a live process tree, behind a paid request
bench_cold_start.no-binaries.out	documents the SKIPPED path taken when `claude` is absent from PATH, which needs an environment rather than an argument to reproduce
bench_mcp_connect.echo.out	wall-clock timings of a live CLI, and embeds the CLI version string
bench_mcp_connect.broken-server.out	as echo.out, and a counterfactual run against a deliberately broken server
census_background_work.out	embeds live pids and process ages, which differ on every run by construction
census_background_work.degraded.out	live pids, under a forced degraded mode
census_background_work.selftest-FAILED.out	live pids, and captures a self-test FAILURE the current code does not produce
measure_active_concurrency.out	reads the running machine's own transcript roots
measure_session_footprint.out	live RSS/PSS and pids read from /proc
gate-evidence/check_figures.RED.out	not output of the current code at all: it records what check_figures.sh PRINTED while RED, stale figures included. See that directory's own header -- an artefact of a FAILURE is not an artefact of a MEASUREMENT
EOF
)

list()     { printf '%s\n' "$RECIPES"        | awk -F'\t' 'NF{print $1}'; }
list_unr() { printf '%s\n' "$UNREPRODUCIBLE" | awk -F'\t' 'NF{print $1 "\t" $2}'; }

regen() {
  local outdir="$1" rc=0 dst mode argv kv
  mkdir -p "$outdir" || return 1
  while IFS=$'\t' read -r dst mode argv; do
    [ -n "${dst:-}" ] || continue
    # Word-split the literal argv column. Unquoted on purpose: the table contains no path
    # with a space in it, and keeping it that way is cheaper than making this line clever.
    local -a args=(); read -r -a args <<<"$argv"
    case "$mode" in
      run)  python3 "$I" "${args[@]}" >"$outdir/$dst" 2>&1 || rc=1 ;;
      refu) { python3 "$I" "${args[@]}" 2>&1; echo "exit=$?"; } >"$outdir/$dst" ;;
      env)  kv="${args[0]}"
            { env "$kv" python3 "$I" "${args[@]:1}" 2>&1; echo "exit=$?"; } >"$outdir/$dst" ;;
      *)    echo "regen_samples: unknown mode '$mode' for $dst" >&2; return 1 ;;
    esac
  done < <(printf '%s\n' "$RECIPES")
  return $rc
}

case "${1:-}" in
  --list)                list ;;
  --list-unreproducible) list_unr ;;
  --write)               regen samples ;;
  "")  echo "usage: $0 --list | --list-unreproducible | --write | <outdir>" >&2; exit 2 ;;
  *)   regen "$1" ;;
esac
