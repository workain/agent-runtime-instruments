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

MIN_PAIRS=30

# figure-string | sample file it must appear in
PAIRS=(
  'REVERT-EACH-FIX GATE -- 13 cases|samples/test_flags_controls.out'
  # ARI#28, measure_request_construction.py. Its own revert-each-fix gate, pinned the same
  # way -- and its FIRST run was RED on 3 of 13, which is the only evidence a gate of this
  # kind can offer that it is able to fail at all.
  'REVERT-EACH-FIX GATE (request construction) -- 51 cases|samples/test_request_construction_controls.out'
  'PASSED -- all 51 fixes are pinned: reverting any one of them turns this gate RED.|samples/test_request_construction_controls.out'
  # Every state this instrument can END in needs an artefact a reader can tell from the
  # others, for the same reason the loop flags do: the dangerous output is not a zero, it
  # is a complete-looking decomposition that is simply too small.
  #
  # Each claim is pinned as the WHOLE CLAUSE, direction words included, not as a bare
  # figure. A review kept every digit in this README byte-identical and inverted the
  # sentence around them -- metadata declared BILLED rather than unbilled, which is the
  # error that flipped the source study's sign -- and every gate stayed green, because a
  # figure-substring check reads digits and not meaning. Pinning the clause means an
  # inversion cannot keep the digits and flip the verb in place. It can still REFRAME
  # the clause -- quote it as a retracted draft and assert the opposite around it -- which
  # a second review demonstrated with every gate green. Presence is not meaning, and no
  # gate here reads meaning; see docs/limits.md.
  '8225 bytes on the wire|samples/measure_request_construction.claims.out'
  'fixed 1665 bytes, paid again on every later turn|samples/measure_request_construction.claims.out'
  'model input 2161 bytes of a 8680-byte body, which is what a per-token bill is computed from|samples/measure_request_construction.claims.out'
  '6519 bytes (75.1% of the body) of provider metadata, which the vendor documents at ZERO model-input tokens|samples/measure_request_construction.claims.out'
  'every figure measured here is quoted in the prose|samples/measure_request_construction.claims.out'
  # The normalised split can legitimately EXCEED the observed wire figure, and unflagged
  # that reads as a broken instrument. Pinned because README's table now says so.
  'NOTE: model input 49655 B and fixed 49426 B EXCEED wire 48820 B (101.7%)|samples/measure_request_construction.hermes.out'
  'normalised body 8680 B|samples/measure_request_construction.dsh.out'
  'normalised body 49655 B|samples/measure_request_construction.hermes.out'
  '3 tool schema(s) exposed|samples/measure_request_construction.dsh.out'
  '20 tool schema(s) exposed|samples/measure_request_construction.hermes.out'
  'provider metadata 6519 B (75.1% of the body)|samples/measure_request_construction.dsh.out'
  'provider metadata 0 B|samples/measure_request_construction.hermes.out'
  'NONE is measurable|samples/measure_request_construction.probes-only-NO-MEASUREMENT.out'
  'UNKNOWN BODY FIELD(S): totally_new_vendor_field|samples/measure_request_construction.unknown-field-REFUSED.out'
  'REPRODUCTION FAILED|samples/measure_request_construction.faithfulness-FAILED.out'
  'DRIFT: a figure above is not in the prose|samples/measure_request_construction.claims-drift-FAILED.out'
  'NOT MEASURED -- no tokeniser given|samples/measure_request_construction.tokeniser-unavailable.out'
  # The three refusals added after the review. Each was a SILENT wrong answer before:
  # a traceback with no file or line, a 10 KB system prompt reduced to 3 bytes with the
  # coverage banner still printing, and 144 KB of real traffic filed as startup probes.
  'is JSON but not an object (it is a list)|samples/measure_request_construction.not-an-object-REFUSED.out'
  'repeats the JSON key(s) system|samples/measure_request_construction.duplicate-key-REFUSED.out'
  '144000 raw bytes between them|samples/measure_request_construction.unparseable-REFUSED.out'
  # Both captures taken TWICE, from separate processes, and compared by --reference. The
  # passing side and the failing side are both pinned: a faithfulness check is only worth
  # anything if it can refuse, and this is the run where it did.
  'every field reproduces to the digit|samples/measure_request_construction.hermes-rerun-faithful.out'
  'provider_metadata_bytes            6515         6519  -4|samples/measure_request_construction.dsh-rerun-REPRODUCTION-FAILED.out'
  'model_input_bytes                  2161         2161  MATCH|samples/measure_request_construction.dsh-rerun-REPRODUCTION-FAILED.out'
  # Both densities, because the claim the README makes is the COMPARISON between them:
  # over the whole body the two engines look ~30% apart and over model input ~3%, and
  # only the second answers a question about cost.
  '3.24 bytes/token over the whole body|samples/measure_request_construction.tokens.out'
  '4.11 bytes/token over MODEL INPUT|samples/measure_request_construction.tokens.out'
  '4.23 bytes/token over MODEL INPUT|samples/measure_request_construction.tokens.out'
  # The engine adapters' STORAGE claims, which a review found asserted with no artefact
  # while the verified=True flag was set. The adapter-note leg below only requires a note
  # to name SOME tracked .out; these pin the specific facts the notes state.
  'session.v4.jsonl|samples/engine_evidence.dsh-hermes-storage.out'
  'state.db|samples/engine_evidence.dsh-hermes-storage.out'
  'PASSED -- all 13 fixes are pinned: reverting any one of them turns a control RED.|samples/test_flags_controls.out'
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
  # measure_token_breakdown.py's loop flags: every state the two signals can end in is
  # pinned to a published run, because the dangerous output here is not a zero but a
  # reassuring "nothing flagged" -- so "flagged", "measured clean", "not computed" and
  # "unmeasured" each need an artefact a reader can tell apart from the others.
  'FLAGGED spans: 1 of 2|samples/measure_token_breakdown.loop-flags-FLAGGED.out'
  'FLAGGED spans: 0 of 2 -- a MEASURED clean result against this threshold|samples/measure_token_breakdown.loop-flags-clean.out'
  'threshold: NOT SET -> NOT COMPUTED|samples/measure_token_breakdown.loop-flags-no-threshold.out'
  'UNMEASURED: not one real user turn was found|samples/measure_token_breakdown.loop-flags-no-user-turn.out'
  'is below 1|samples/measure_token_breakdown.loop-flags-threshold-zero-REFUSED.out'
  # ARI#17: signal 2 used to be pinned here by its 'NOT IMPLEMENTED' line. It is
  # implemented now, so the figure that replaces it pins the thing worth pinning -- that
  # the signal actually COUNTS on a published run -- rather than deleting the check and
  # leaving the newest signal the only one with no artefact behind it. Both of its windows
  # are pinned, because neither subsumes the other and a reader must be able to tell a
  # consecutive run from a within-span multiplicity.
  'longest consecutive run of one signature: 8|samples/measure_token_breakdown.loop-flags-FLAGGED.out'
  'most occurrences of one signature within a single span: 7|samples/measure_token_breakdown.loop-flags-FLAGGED.out'
  # and its own 'nothing to measure' state, the one that must not read as a clean result
  'UNMEASURED: no tool call was seen at all|samples/measure_token_breakdown.no-bash-turns.out'
  # ARI#24, flags.py. Same reasoning as the loop-flag block above, for the same reason: the
  # dangerous output of a flag library is not a zero, it is a reassuring "nothing flagged".
  # So each of the FOUR outcomes a caller must be able to tell apart is pinned to a run --
  # fired, measured-clean, not-computed, unmeasured -- plus the gated case, which is the
  # one a reviewer is most likely to assume cannot happen (a measure OVER its threshold
  # that still does not fire).
  'FIRED -- safely-mergeable consecutive single-Bash-call pairs|samples/flags.FIRED.out'
  'a MEASURED clean result|samples/flags.clean.out'
  'NOT_COMPUTED: no threshold given|samples/flags.no-threshold.out'
  'UNMEASURED: no tool call was seen at all in the window|samples/flags.unmeasured.out'
  'below the 3-turn break-even|samples/flags.agent-gap-gated.out'
  # and that a window really does narrow what is seen, rather than reading the whole file
  # and calling it a window
  'SPANS ARE LOCAL to this window|samples/flags.windowed.out'
  # ARI#24, the cost claim. Every figure the README states about the windowed read is
  # pinned here, because "cheap" was the one thing the issue said must be a number.
  # Each of these three carries its own median AND its best AND its worst on one line, so
  # one pinned string covers all three claims the prose makes about that cost. Quoting the
  # artefact's own line rather than reformatting it is deliberate: a gloss ("1.00 MB against
  # 41.90 MB") is a figure this gate's extractor cannot match to anything, which is how the
  # first draft of this section failed it in eight places at once.
  'WINDOW / FULL, bytes read    median 0.237   best 0.075   worst 0.387|samples/bench_window_read.out'
  'WINDOW / FULL, lines parsed  median 0.271   best 0.080   worst 0.414|samples/bench_window_read.out'
  'WINDOW / FULL, wall-clock    median 0.139   best 0.052   worst 0.270|samples/bench_window_read.out'
  'RESUME / FULL, bytes read    median 0.097   best 0.046   worst 0.161|samples/bench_window_read.out'
  'bytes:                FULL 43.7 MB, WINDOW 8.0 MB, RESUME 3.7 MB|samples/bench_window_read.out'
  'lines fed to json.loads: FULL 16307, WINDOW 3336, RESUME 1486|samples/bench_window_read.out'
  'corpus: 10 transcripts >= 524288 bytes, largest first|samples/bench_window_read.out'
  # The resumed-transcript outcome, which must stay distinguishable from a real MISMATCH for
  # the same reason the live-growth one must: a documented residual occupying the slot
  # reserved for an unexplained defect makes the two indistinguishable thereafter.
  'RESUMED TRANSCRIPT, DISCLOSED RESIDUAL|samples/bench_window_read.out'
  # The live-growth outcome, which must stay distinguishable from a real MISMATCH. Pinned
  # to the SELF-TEST, not to bench_window_read.out: whether a live run happens to catch a
  # session mid-turn is luck, so an artefact of the real corpus cannot be relied on to
  # contain it, while the control forces the case deterministically.
  'a file that grew mid-measurement|samples/bench_window_read.selftest.out'
  # and the tuning sweep the default was taken from -- both rows, since the claim is a
  # COMPARISON between them and pinning only the winner would not check it
  '       1024K        10.00      10            0 of 10|samples/bench_window_read.first-chunk-sweep.out'
  '        256K        10.00      20           10 of 10|samples/bench_window_read.first-chunk-sweep.out'
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

# A flattened copy of every prose file for the citation guard below -- ONE LINE PER FILE,
# which matters twice.
#
#   * `grep -F` matches within a line, so a pinned clause cannot be satisfied by a splice
#     across two files. A review removed a clause from README, planted its two halves at
#     the end of one prose file and the start of the next, and a single flattened `cat`
#     of all four passed it -- the clause existing in no file at all.
#   * A leading blockquote `> ` is stripped before flattening. Flattening alone left that
#     marker mid-clause, so a correct claim quoted inside a blockquote FAILED -- the
#     cry-wolf behaviour this guard was made whitespace-insensitive to avoid.
prose_flat="$(mktemp)"
trap 'rm -f "$prose_flat"' EXIT
: > "$prose_flat"
for pf in "${PROSE[@]}"; do
  sed 's/^[[:space:]]*>[[:space:]]*//' "$pf" 2>/dev/null \
    | tr '\n' ' ' | tr -s ' ' >> "$prose_flat"
  echo >> "$prose_flat"
done

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
  # guard 1: the figure must actually be cited in the prose, or this entry checks nothing.
  #
  # WHITESPACE-INSENSITIVE on the prose side only. The artefact check above stays exact --
  # the artefact's own spelling is the thing being pinned -- but a claim in a markdown
  # file WRAPS, and a 100-character pinned clause always wraps. This is the same lesson
  # measure_request_construction.py's --check-claims carries in its own docstring: that
  # gate once failed purely because a claim had broken across a line, the number was
  # right, and a gate that cries wolf at reflow is a gate that gets switched off. Pinning
  # whole clauses rather than bare figures (so an inversion cannot keep the digits and
  # flip the meaning) made every long claim hit this, which is how it was found again.
  if ! grep -qF -- "$(printf '%s' "$fig" | tr '\n' ' ' | tr -s ' ')" "$prose_flat"; then
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
# TWO directories under samples/ are EXCLUDED from the search, for the same reason.
#
# samples/gate-evidence/ records what a GATE printed, including the stale figures a round
# was fixing, so searching it makes every previously-wrong figure permanently findable.
# Measured: with it included, both `spread=0.85s` and `activity in 7d: 35` passed this
# leg on the strength of a file whose purpose is to record them as WRONG.
#
# samples/raw-payloads/ is the same hazard arriving from the other side: verbatim protocol
# traces, kept as evidence of a COUNT. One was committed in this very branch -- a dsh event
# log, so that its `"notifications": 16` summary would be countable rather than asserted --
# and a review used it the same afternoon to launder a max_tokens value the client had sent
# into README's engine table as a wire-byte figure, with the whole preflight green. See that
# directory's own README -- including why neither it nor this comment may quote the value.
#
# samples/fixtures/ is the other direction: it holds instrument INPUTS -- captured JSON
# request bodies, and a markdown file whose entire purpose is to contain a WRONG figure
# for a drift gate to catch. An artefact is a thing an instrument PRINTED; an input is
# not evidence of anything. A review demonstrated the cost of not excluding it: with
# this branch's capture fixtures in the searched corpus, the README's engine-comparison
# table -- its headline deliverable -- could be rewritten to carry a max_tokens value
# from inside the dsh fixture as its wire-byte figure, `9` fixed bytes and `1` tool, and
# both this gate and the whole preflight stayed green. Near-miss digit flips were still
# caught, so the gate had teeth only against values adjacent to the truth.
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
  # (one space: the extractor squeezes runs, because a span reunited across a line break
  #  carries its second half's indentation as interior whitespace)
  'read=14848 creation=11416' 'read=14848 creation=14307' 'read=26264 creation=2875'
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
  # --- git plumbing in README's merge recipe, not measurements. The SHA must NOT be
  # --- findable under samples/: it names the one commit grandfathered past the identity
  # --- gate (scripts/identity_allowlist.sh), and an artefact is not where that lives. ---
  '40372496f26' 'git log -1'
  # --- extractor artefact: prose containing "404'd" matches the label:N shape ---
  "blob the GitHub API 404'd was still served by"
  'returns 0 bytes, so'
  # --- ARI#24. Prose glosses of a figure pinned in its artefact's own spelling: the sweep
  # --- table says `256K` and `1024K`, and the pinned rows above are what guard these. ---
  '256 KB' '128 KB'
  # --- ARI#24 review findings quoted in the prose as the defects they WERE. These must not
  # --- match an artefact: the whole point of naming them is that they no longer happen, and
  # --- the artefact now shows the corrected behaviour. Same rule as '103 transcripts'. ---
  'it returned 437 for N=400,' 'could **exceed 1** — up to'
  'xtends its buffer backwards instead: 0'
  # --- gloss of the pinned per-file row's size column (the largest transcript in the run) ---
  '42MB'
  # --- ARI#28 round 3. Code quoted in the prose as the defect it was, in the spelling the
  # --- mutation used. None is a reading of anything and none can appear in an artefact --
  # --- same rule as '103 transcripts' above. `utf-8` is here because it contains a digit
  # --- and `system_bytes > 5000` because the leg that asserted it was the weak one. ---
  'int(Content-Length or 0)' 'parts.append(instr[:1])' 'system_bytes > 5000' 'utf-8'
  # --- the output the DEFECTIVE recorder produced for 40391 real bytes, quoted as the
  # --- finding. It must not match an artefact: the artefacts now show it de-chunked. ---
  '1 measurable, 1 startup probes'
  # --- ARI#28 round 5. A gate-case LABEL and the lossy fallback one of them mutates to.
  # --- Neither is a reading of anything: the label names a case in
  # --- scripts/test_request_construction_controls.py (whose own output artefact lists it,
  # --- but under its full name, not the bare label), and the other is code quoted as the
  # --- wrong answer it was. ---
  'C51' '.encode("utf-8", "replace")'
  # --- ARI#28 round 4. The "figure" the reverted union leg INVENTED out of an ordinary
  # --- wrapped sentence, quoted in BOTH docs/limits.md and README as the false alarm it
  # --- was -- so this entry is load-bearing for two files, not one, and removing it fails
  # --- on both. It is a fragment of prose, not a reading, and it must not match an
  # --- artefact: that is the point of quoting it. Same rule as '103 transcripts'. ---
  '-- 6519 of 8680 B -- while hermes publishes'
  # --- ARI#28 round 3. ILLUSTRATIONS in docs/limits.md, not readings: a float raw_bytes
  # --- some producers emit, and the number spelling whose re-serialisation is the second
  # --- cause the overrun refusal cannot distinguish from a mis-decoded body. Each is an
  # --- example of a shape, and none is a measurement of anything here. ---
  '8225.0' '1E5' '100000.0' 'Content-Length: 0'
  # --- API surface, not measurements: a parameter name, a command line, and an example
  # --- ISO-8601 instant in a code block showing how to call read_window(). None of these
  # --- is a reading of anything, and none could appear in an artefact. ---
  'last_turns=40' 'T12:00' 'python3 instruments/flags.py --help-blind'
  # --- extractor artefacts: markdown TABLE ROWS from the flags table, which the
  # --- `label: N`-shaped matcher picks up because a row contains `|` and a `#NNN` ---
  "| consecutive single-Bash turns that could have been **one** call | alm#831's"
  "| one identical tool+arguments signature, over and over | this repo's own signal 2 in"
  'v1 | "0 of 16 sessions own background work" | Its filter discarded'
  'v3 | "15 live session cgroups" | Discovery used the broad "is this agent-internal?" pattern, which matches a bare'
  # --- ARI#28. API surface and on-disk layout, not measurements: a tokeniser spec, an
  # --- argv index, and the dsh session-log PATH (which contains `v4` and so trips the
  # --- backticked-span-with-a-digit matcher). A path is a claim about a filesystem, and
  # --- the adapter's own note in instruments/engines.py is where it is accountable --
  # --- the engine-adapter leg below is what checks that note names a tracked artefact. ---
  '--tokeniser tiktoken:cl100k_base' 'argv[0]' '${BASH_SOURCE[0]}'
  '$DSH_HOME/sessions/<cwd-slug>/<session-id>/session.v4.jsonl'
  # --- the WRONG denominator, quoted in README as the error an earlier draft published
  # --- (6519/8225 = 79.3%, against a figure computed over the 8680-byte body). Same rule
  # --- as '103 transcripts': it must NOT match an artefact, or the correction could not
  # --- be written down.
  '79.3%'
  # --- JSON literals illustrating what a compact serialization looks like next to
  # --- json.dumps' default separators. Not readings of anything.
  '{"a":1}' '{"a": 1}'
  # --- a CODE EXPRESSION, quoted as the shortcut a mutation reverts framing to. Not a
  # --- reading; it could not appear in an artefact.
  'len(key) + 2' 'rows[0]'
  # --- CODE EXPRESSIONS: the fixed-divisor spellings docs/limits.md names as the ones a
  # --- review walked past the first version of the tokeniser guard. Not readings.
  '(len+3)//4' 'len//4+1' 'len//5' 'int(len/3.8)' 'round(len/4)' 'len(bytes)//4'
  # --- the WRONG figure, quoted beside its correction ((3.65-1.96)/1.96 = 86.2%, not 87%).
  # --- Must NOT match an artefact: naming it is how the correction gets written down.
  '87%'
  # --- ARI#28, surfaced by the percentage and `N requests`/`N tools` patterns added to
  # --- the extractor above, which nothing had matched before.
  # --- A COST ESTIMATE for a paid benchmark (how many requests it spends), not a reading
  '6 requests'
  # --- a figure about a PAST DEFECT: how wrong a reviewer found one sentence to be. Same
  # --- rule as '103 transcripts' -- it must NOT match an artefact, that is the point.
  '38%'
  # --- gloss of the pinned `min=1.96s  max=3.65s`. (3.65-1.96)/1.96 = 86.2%. The prose
  # --- said 87% until this extractor learned to read percentages, on its first run.
  '86%'
)
is_exempt() {
  local f="$1" n
  for n in "${EXEMPT[@]}"; do [ "$f" = "$n" ] && return 0; done
  return 1
}

# The extractor, as ONE function, because it now has two callers: the prose files below
# and every instrument's own --help-blind answer further down. Two copies would drift,
# and the copy that drifted would be the one guarding the newer surface.
extract_figures() {
  local pf="$1"
  # The backticked-span pattern reads a copy in which a line still INSIDE an inline code
  # span has been joined to the next; every other pattern reads the file as written.
  #
  # Why only that one, and why joined rather than wholly flattened: a backticked span is
  # delimited, so reuniting the two halves of one cannot run two separate spans together,
  # and spans are the only pattern that needs it -- this branch's move to long pinned
  # clauses makes a wrapped span the normal case, and per-line grep never sees one. A
  # review planted `fixed 99999 bytes` on a single line (caught) and the same figure
  # wrapped across a line break (NOT caught), then pointed out that this instrument's own
  # --help-blind answer carries `fixed 1665 bytes` wrapped exactly that way -- a
  # currently published figure that no gate was reading. Flattening the whole file was
  # the first attempt and was worse: it merges fenced code blocks and unmatched
  # backticks, which re-pairs every span in the file and produced 400-character "figures"
  # of ordinary prose.
  #
  # Joining re-PAIRS backticks, and that cuts BOTH ways -- two reviews, one each way. A
  # span left open on one line swallows the opening backtick of the next line's span, so
  # a figure typed on that line lands between two backticks that are no longer a
  # delimiter pair: `fixed 99999 B` was published in README that way, present in no
  # artefact, with the whole preflight green. The answer looked like reading the file as
  # written TOO and taking the union -- which the next review broke immediately, because
  # on the continuation line of a legitimate wrapped span the same mis-pairing EMITS the
  # prose between one span's closer and the next one's opener. An ordinary sentence in
  # this branch's own style failed the gate with '-- 6519 of 8680 B -- while hermes
  # publishes' as its "figure".
  #
  # Both directions have the same root: per-line backtick pairing is ambiguous, and no
  # arrangement of greps resolves it. So the span leg reads the JOINED copy only -- code
  # spans as markdown defines them -- and the hidden-span case is covered where there is
  # no ambiguity at all: the `B` unit pattern below reads the file AS WRITTEN, so the
  # planted `fixed 99999 B` is caught as `99999 B` whatever its backticks are doing.
  #
  # WHAT THAT LEAVES UNCOVERED, stated rather than implied absent: a figure typed between
  # two backticks that are not a delimiter pair, carrying NO unit this file's patterns
  # know. It is not a published code span (it renders as mangled prose, backticks and
  # all), and no gate here reads it. `docs/limits.md` says so too.
  local joined
  joined="$(mktemp)"
  awk '
    /^[[:space:]]*```/ { if (p) { print b; b = ""; p = 0; o = 0 } f = !f; print; next }
    # A blank line ends a paragraph, and an inline span cannot cross one. Flushing here
    # bounds a re-pairing to the paragraph it starts in.
    /^[[:space:]]*$/ { if (p) { print b; b = ""; p = 0; o = 0 } print; next }
    { if (f) { print; next }
      # A blockquote marker is stripped before the span is reunited. Without this the
      # `> ` of a continuation line lands INSIDE the span, and a correct clause quoted
      # across two blockquoted lines is extracted as a figure that exists nowhere --
      # a false alarm, which is how a gate loses its reader.
      sub(/^[[:space:]]*>[[:space:]]*/, "")
      l = $0; c = gsub(/`/, "`", l)
      b = p ? b " " $0 : $0
      o = (o + c) % 2
      if (o == 0) { print b; b = ""; p = 0 } else p = 1 }
    END { if (p) print b }' "$pf" > "$joined"
  { grep -ohE '`[^`]*[0-9][^`]*`' "$joined" | sed 's/^`//; s/`$//'
    grep -ohE '[A-Za-z][A-Za-z0-9_ ()<=-]{2,34}[:=] ?[0-9][0-9.,/]*' "$pf"
    grep -ohE '\b[0-9][0-9.,]* ?(MB|MiB|GB|KB|ms|bytes)\b' "$pf"
    # BARE `B` for bytes -- the spelling every column of this branch's own tables uses
    # (`wire 8225 B`, `provider metadata 0 B`). Read over the file AS WRITTEN, which is
    # what makes it the answer to the hidden-span case the union leg was reverted for: a
    # five-digit figure planted inside mis-paired backticks is still `99999 B` here.
    # Measured when added: 12 published byte figures across README and docs/limits.md
    # that only the span leg had been reading, every one of them already in an artefact,
    # and no new exemption needed. `238 MB` does not match it (the character before `B`
    # is not a digit or a space), and `6519 bytes` does not either (case-sensitive).
    grep -ohE '\b[0-9][0-9.,]* ?B\b' "$pf"
    grep -ohE '\b[0-9]+[0-9.,]* ?s\b' "$pf"
    # PERCENTAGES. Added after a review kept every digit in a headline intact and moved
    # its DENOMINATOR: the prose said "8225 bytes on the wire, of which 6519 bytes
    # (75.1%)", and 6519/8225 is 79.3% -- the sentence named one denominator and quoted
    # the other's answer. No pattern here matched a bare percentage, so nothing could
    # see it. No trailing \b, because `%` is already a non-word character and a word
    # boundary after it never matches.
    grep -ohE '\b[0-9][0-9.,]*%' "$pf"
    # NUMBER-FIRST counts, e.g. `103 transcripts`. Restricted to the nouns the
    # instruments actually report, rather than any `<digits> <word>`: the generic form
    # returned 25 spans of pure prose noise ("0 makes", "13 that") and an exemption list
    # of that shape rots into a dumping ground. Naming the quantities keeps every entry
    # meaningful. This is the shape that let `103 transcripts` survive four commits.
    grep -ohE '\b[0-9][0-9.,]* (transcripts|sessions|session trees|trees|cgroups|processes|procs|lines|tools|requests)\b' "$pf"
  } 2>/dev/null | tr -s ' ' | sed 's/^ *//; s/ *$//' | sort -u
  rm -f "$joined"
}

# The searched corpus, space-squeezed once, for the same reason the figures above are:
# a span reunited across a line break carries the leading indentation of its second half
# as interior whitespace, and `grep -F` is exact. Both sides squeezed, so a figure whose
# own spacing is load-bearing still matches its artefact's.
samples_flat="$(mktemp)"
trap 'rm -f "$prose_flat" "$samples_flat"' EXIT
find samples -type f -name '*.out' \
     -not -path 'samples/gate-evidence/*' \
     -not -path 'samples/fixtures/*' \
     -not -path 'samples/raw-payloads/*' -print0 \
  | xargs -0 cat 2>/dev/null | tr -s ' ' > "$samples_flat"

for pf in "${PROSE[@]}"; do
  while IFS= read -r fig; do
    [ -z "$fig" ] && continue
    is_exempt "$fig" && continue
    # a sample FILENAME is not a figure; the cited-artefact leg above already asserts
    # each one exists and is tracked. Exempting them by name here would need an edit
    # every time a sample is added, which is how an exemption list rots.
    case "$fig" in *.out) continue;; esac
    if ! grep -qF -- "$fig" "$samples_flat" 2>/dev/null; then
      echo "FAIL: $pf states '$fig' which appears in NO artefact under samples/."
      echo "      Either it drifted, or it is not a measurement and belongs in EXEMPT"
      echo "      in this script WITH A REASON."
      fail=1
    fi
  done < <( extract_figures "$pf" )
done

# EVERY INSTRUMENT'S `--help-blind` ANSWER IS PROSE, and is checked like prose.
#
# docs/method.md rule 1 rests on that answer: "what would this print if it were blind?"
# is the first thing a reader is told to ask, and the text that answers it is published
# by `--help-blind`. It was not in PROSE, so no figure in it was checked by anything.
#
# A review found what that costs. measure_request_construction.py's blind answer stated
# its central finding as `79.3% of the body (6,522 of 8,228 bytes)` and reasoned "wrong by
# a factor of five" from it. Those are the SOURCE study's figures, not this repository's:
# the committed capture measures 6519 of 8680 body bytes. All three numbers were absent
# from every artefact and every prose file here, three bytes from the real ones in both
# numerator and total -- close enough to read as a transcription of this repo's own
# measurement, which is exactly why nobody re-derived them.
#
# Only the MODULE DOCSTRING is read, because that is what --help-blind prints. Scanning
# whole source files would make every literal in the code a claim about an artefact.
BLIND_EXEMPT=(
  # --- HYPOTHETICAL blind output: these passages exist to say "this is what a blind run
  # --- prints". They MUST NOT exist in an artefact. Same rule as the EXEMPT block above.
  'requests: 0' 'mean=0.00' 'session trees: 0' 'transcripts found: 0' 'busy_minutes=0'
  'sidechain_turns: 0' 'read=0,' 'read=0, creation=76443' 'creation=76443'
  'LIVE sessions owning background work older than Ns: 0'
  'wning background work older than Ns: 0' '{"verdict": "drove a turn", "notifications": 0}'
  # --- API SURFACE: parameter names, defaults and signatures. Not readings.
  'last_turns=40' 'agent_gap_threshold=30' 'batchable_threshold=2,' 'repeated_threshold=4,'
  'DEFAULT_MAX_TURNS = 10' 'max_iterations: int | None = 15' 'None = 15'
  '--tokeniser tiktoken:cl100k_base' '/v1/models' 'sess-<8 hex>'
  './roast-hc9999-fake-example' 'x2,' 'reports size 0, so' 'processes (hidepid=2,'
  "tests the FILTER, i.e. v2's" 'Treating n=2' '30 s'
  'ch of the session it is NOT reading: 40'
  # --- HISTORICAL readings, labelled as such in the docstring that quotes them
  '0.65 s' '1.15 s' '1.16 s' '3.16 s' '3.48 s' '4.04 s' '3.53 s' '3.74 s'
  # --- third-party / stat figures, attributed at the use site (docs/citations.md)
  '323,021,104 bytes'
  # --- ILLUSTRATIVE figures inside a blind answer's own explanation: an example of what
  # --- a misleading output would LOOK like ("mean 0.43 sessions" over an empty corpus),
  # --- and a round proportion standing for "nearly all" ("a corpus that was 99%
  # --- skipped"). Neither is a reading, and neither should match an artefact.
  '0.43 sessions' '99%'
  # --- the SOURCE STUDY's own published figures, named in the paragraph that explains
  # --- how counting provider metadata as model input inverted their sign. Same rule as
  # --- '146 transcripts'/'97 sessions' in EXEMPT: another corpus, no artefact here.
  '20%' '44%'
  # --- the WRONG figures, quoted in that same paragraph as the ones an earlier draft of
  # --- it published by mistake. They must NOT match an artefact -- naming them is the
  # --- whole point, exactly like '103 transcripts' in EXEMPT. A gate that demanded an
  # --- artefact for them would force the correction to be deleted to pass.
  '79.3%' '8,228 bytes'
  # --- quoted from a cited third-party paper (the MAST failure taxonomy), attributed at
  # --- the use site. Another corpus; no artefact of ours can contain them.
  '15.7%' '13.2%'
)
blind_fail=0; blind_files=0; blind_figs=0
for inst in instruments/*.py; do
  grep -q -- '"--help-blind"' "$inst" || continue
  doc=$(mktemp); python3 - "$inst" >"$doc" <<'PY'
import ast, io, sys
print(ast.get_docstring(ast.parse(io.open(sys.argv[1], encoding="utf-8").read())) or "")
PY
  blind_files=$((blind_files+1))
  while IFS= read -r fig; do
    [ -z "$fig" ] && continue
    case "$fig" in *.out) continue;; esac
    is_exempt "$fig" && continue
    skip=0
    for n in "${BLIND_EXEMPT[@]}"; do [ "$fig" = "$n" ] && skip=1 && break; done
    [ "$skip" -eq 1 ] && continue
    if ! grep -rqF --exclude-dir=gate-evidence --exclude-dir=fixtures --exclude-dir=raw-payloads \
         -- "$fig" samples/ 2>/dev/null; then
      echo "FAIL: $inst --help-blind states '$fig' which appears in NO artefact under"
      echo "      samples/. A blind answer is published prose: either it drifted, or it"
      echo "      is not a measurement and belongs in BLIND_EXEMPT WITH A REASON."
      blind_fail=1; fail=1
    else
      blind_figs=$((blind_figs+1))
    fi
  done < <( extract_figures "$doc" )
  rm -f "$doc"
done
if [ "$blind_files" -lt 10 ]; then
  echo "SCRUB-ERROR: only $blind_files instruments were scanned for --help-blind prose;"
  echo "this repository has more than that. The extractor or the grep broke." >&2
  fail=1
elif [ "$blind_fail" -eq 0 ]; then
  echo "all $blind_figs figures in $blind_files --help-blind answers exist in an artefact"
fi

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
