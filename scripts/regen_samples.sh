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

# ARI#24 added a second instrument with reproducible samples (instruments/flags.py), so the
# instrument is now a COLUMN rather than this one variable. One table and one loop, not two
# of each: a per-instrument table would have to be added to `list()`, to `regen()` and to
# the gate's own count check every time, and the one that got forgotten would be the one
# silently unverified -- which is the exact failure class ARI#22 built this file for.

# Each entry: <basename-without-samples/> <TAB> <instrument> <TAB> <mode> <TAB> <argv...>
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
measure_token_breakdown.out	instruments/measure_token_breakdown.py	run	samples/fixtures/token_breakdown.jsonl
measure_token_breakdown.json.out	instruments/measure_token_breakdown.py	run	--json --manifest samples/fixtures/token_breakdown.manifest.json
measure_token_breakdown.bash.out	instruments/measure_token_breakdown.py	run	samples/fixtures/token_breakdown.bash.jsonl
measure_token_breakdown.other.out	instruments/measure_token_breakdown.py	run	samples/fixtures/token_breakdown.other.jsonl
measure_token_breakdown.no-bash-turns.out	instruments/measure_token_breakdown.py	run	samples/fixtures/token_breakdown.no-bash-turns.jsonl
measure_token_breakdown.batch.out	instruments/measure_token_breakdown.py	run	samples/fixtures/token_breakdown.jsonl samples/fixtures/token_breakdown.child.jsonl
measure_token_breakdown.subsession.out	instruments/measure_token_breakdown.py	run	--manifest samples/fixtures/token_breakdown.manifest.json
measure_token_breakdown.user-entry-census.out	instruments/measure_token_breakdown.py	run	--survey-user-entries samples/fixtures/loop_flags.flagged.jsonl samples/fixtures/loop_flags.clean.jsonl samples/fixtures/token_breakdown.jsonl
measure_token_breakdown.loop-flags-FLAGGED.out	instruments/measure_token_breakdown.py	run	--flag-span-tool-calls 5 --flag-no-tool-streak 3 samples/fixtures/loop_flags.flagged.jsonl
measure_token_breakdown.loop-flags-clean.out	instruments/measure_token_breakdown.py	run	--flag-span-tool-calls 5 --flag-no-tool-streak 3 samples/fixtures/loop_flags.clean.jsonl
measure_token_breakdown.loop-flags-no-user-turn.out	instruments/measure_token_breakdown.py	run	--flag-span-tool-calls 5 --flag-no-tool-streak 3 samples/fixtures/loop_flags.no-user-turn.jsonl
measure_token_breakdown.loop-flags-no-threshold.out	instruments/measure_token_breakdown.py	run	samples/fixtures/loop_flags.flagged.jsonl
measure_token_breakdown.loop-flags.json.out	instruments/measure_token_breakdown.py	run	--json --flag-span-tool-calls 5 --flag-no-tool-streak 3 samples/fixtures/loop_flags.flagged.jsonl
measure_token_breakdown.selftest.out	instruments/measure_token_breakdown.py	refu	--self-test
measure_token_breakdown.no-transcript.out	instruments/measure_token_breakdown.py	refu	samples/fixtures/token_breakdown.missing.jsonl
measure_token_breakdown.corrupt-fixture-REFUSED.out	instruments/measure_token_breakdown.py	refu	samples/fixtures/token_breakdown.corrupt.jsonl
measure_token_breakdown.one-field-renamed-REFUSED.out	instruments/measure_token_breakdown.py	refu	samples/fixtures/token_breakdown.corrupt-one-field.jsonl
measure_token_breakdown.loop-flags-bad-env-REFUSED.out	instruments/measure_token_breakdown.py	env	ARI_FLAG_SPAN_TOOL_CALLS=twenty samples/fixtures/loop_flags.flagged.jsonl
measure_token_breakdown.loop-flags-threshold-zero-REFUSED.out	instruments/measure_token_breakdown.py	env	ARI_FLAG_SPAN_TOOL_CALLS=0 samples/fixtures/loop_flags.flagged.jsonl
flags.FIRED.out	instruments/flags.py	run	--flag-batchable-bash 2 --flag-repeated-calls 4 --flag-agent-gap 5 samples/fixtures/flags.batchable.jsonl
flags.repeats-FIRED.out	instruments/flags.py	run	--flag-batchable-bash 2 --flag-repeated-calls 4 --flag-agent-gap 5 samples/fixtures/flags.repeats.jsonl
flags.agent-gap-FIRED.out	instruments/flags.py	run	--flag-batchable-bash 2 --flag-repeated-calls 4 --flag-agent-gap 5 samples/fixtures/flags.content.jsonl
flags.clean.out	instruments/flags.py	run	--flag-batchable-bash 2 --flag-repeated-calls 4 --flag-agent-gap 5 samples/fixtures/flags.dependent.jsonl
flags.agent-gap-gated.out	instruments/flags.py	run	--flag-batchable-bash 2 --flag-repeated-calls 4 --flag-agent-gap 5 samples/fixtures/flags.orchestration.jsonl
flags.unmeasured.out	instruments/flags.py	run	--flag-batchable-bash 2 --flag-repeated-calls 4 --flag-agent-gap 5 samples/fixtures/flags.no-tool.jsonl
flags.no-threshold.out	instruments/flags.py	run	samples/fixtures/flags.batchable.jsonl
flags.windowed.out	instruments/flags.py	run	--last-turns 4 --flag-repeated-calls 2 samples/fixtures/flags.repeats.jsonl
flags.json.out	instruments/flags.py	run	--json --flag-batchable-bash 2 --flag-repeated-calls 4 --flag-agent-gap 5 samples/fixtures/flags.content.jsonl
flags.selftest.out	instruments/flags.py	refu	--self-test
flags.no-transcript.out	instruments/flags.py	refu	samples/fixtures/flags.missing.jsonl
flags.two-windows-REFUSED.out	instruments/flags.py	refu	--last-turns 5 --from-offset 100 samples/fixtures/flags.repeats.jsonl
flags.threshold-zero-REFUSED.out	instruments/flags.py	refu	--flag-repeated-calls 0 samples/fixtures/flags.repeats.jsonl
bench_window_read.selftest.out	instruments/bench_window_read.py	refu	--self-test
bench_window_read.empty-corpus-REFUSED.out	instruments/bench_window_read.py	env	ROOTS=/nonexistent/ari24 --top 3
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
bench_window_read.out	reads the running machine's own transcript roots, and reports wall-clock; both the corpus and the timings differ on every run (the files grow while the fleet works, including this session's own)
bench_window_read.first-chunk-sweep.out	same live corpus as bench_window_read.out -- the byte totals move as the transcripts grow
gate-evidence/check_figures.RED.out	not output of the current code at all: it records what check_figures.sh PRINTED while RED, stale figures included. See that directory's own header -- an artefact of a FAILURE is not an artefact of a MEASUREMENT
EOF
)

list()     { printf '%s\n' "$RECIPES"        | awk -F'\t' 'NF{print $1}'; }
list_unr() { printf '%s\n' "$UNREPRODUCIBLE" | awk -F'\t' 'NF{print $1 "\t" $2}'; }

regen() {
  local outdir="$1" rc=0 dst mode argv kv
  mkdir -p "$outdir" || return 1
  while IFS=$'\t' read -r dst inst mode argv; do
    [ -n "${dst:-}" ] || continue
    # Word-split the literal argv column. Unquoted on purpose: the table contains no path
    # with a space in it, and keeping it that way is cheaper than making this line clever.
    local -a args=(); read -r -a args <<<"$argv"
    case "$mode" in
      run)  python3 "$inst" "${args[@]}" >"$outdir/$dst" 2>&1 || rc=1 ;;
      refu) { python3 "$inst" "${args[@]}" 2>&1; echo "exit=$?"; } >"$outdir/$dst" ;;
      env)  kv="${args[0]}"
            { env "$kv" python3 "$inst" "${args[@]:1}" 2>&1; echo "exit=$?"; } >"$outdir/$dst" ;;
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
