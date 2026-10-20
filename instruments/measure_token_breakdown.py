#!/usr/bin/env python3
"""Where did one agent session's tokens actually go -- by tool, by subagent, by subsession?

An agent session's cost is dominated by the four token counts the engine records per
assistant turn (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`). This reads real Claude Code transcript `.jsonl` files and
splits that spend three ways:

  BY TOOL       -- group each usage-bearing turn by the SET of tool names that turn's own
                   assistant message invoked (`tool_use` blocks). This is TURN-GRAIN
                   attribution, not a per-call split: a turn that calls N tools has ONE
                   `usage` entry that cannot be divided exactly among them, and this
                   instrument does not pretend it can. A turn that calls Read and Edit is
                   reported under the pair `Edit+Read`, not half to each. Bash turns are
                   additionally clustered BY COMMAND (the command's first token, after
                   stripping `cd ...&&` chains and `VAR=` prefixes) so a lumped `Bash`
                   bucket splits into `git`, `pytest`, `curl`, ... -- a heuristic, not a
                   semantic parse, with an explicit `(unparsed)` bucket. The published key
                   vocabulary is CLOSED: a word from a public-command allowlist, `(other)`,
                   or `(unparsed)`, and nothing else -- so a local script, branch or
                   worktree name cannot become a key, which it could while the test was
                   only the SHAPE of a word. `(other)`'s distinct-command COUNT is reported
                   so the bucket stays readable without naming anything. The peel is
                   quote-aware and runs to a fixed point. All of this is because a cluster
                   key is PUBLISHED and is not anonymised: see `bash_cluster`.
  BY AGENT      -- separate same-session Task-tool subagent turns (`isSidechain: true`)
                   from the main thread, grouped by subagent type (`attributionAgent`)
                   where the transcript records one. These are sidechains WITHIN one
                   session's transcript, NOT separate child sessions -- those are the
                   subsession rollup below.
  BY SUBSESSION -- an optional caller-supplied manifest (plain JSON) describing a tree of
                   SEPARATE sessions/transcripts, summed bottom-up to each parent, plus
                   agent-level cross-tabs: each node's own total, each node's own by-tool
                   breakdown, and the grand by-tool profile both WITH every node summed in
                   and root-only (so delegated vs orchestrator-own cost is comparable).
                   Cross-session parent/child linkage is harness-specific, so this
                   instrument does not infer it; the caller states it, and this repo stays
                   harness-agnostic. Passing 2+ transcripts positionally WITHOUT a manifest
                   prints a flat PER-SOURCE table instead -- a batch of sessions, no
                   hierarchy claimed.

  LOOP FLAGS   -- two mechanical (non-LLM) signals correlated with excessive work cycles,
                   computed from transcript STRUCTURE alone -- turn boundaries and
                   `tool_use` block counts -- and never from judging what was SAID:
                   (1) tool calls in each span between consecutive REAL user turns, and
                   (3) runs of consecutive assistant turns that called no tool at all.
                   (2) is a deliberate gap -- see SIGNAL 2 below.

THESE ARE FLAGS, NOT VERDICTS. A flagged span says "an unusual amount of work happened
here by this threshold", which is a pointer for a human or a later semantic layer to look
at. It is NOT a finding that the work was wasted, redundant, or wrong: a 40-call span can
be a correct, necessary refactor and a 3-call span can be a pointless loop. This
instrument measures and surfaces; it does not judge a session, and it will not be made to
by adding a pass/fail line to its output.

NO THRESHOLD IS SHIPPED AS A DEFAULT, AND THAT IS THE MEASURED POSITION. The obvious
default -- "more than 20 tool calls after one user turn is abnormal" -- was checked
against published practice before being implemented, and nothing grounds it. The agent
frameworks' own loop ceilings, each read at first hand from its own reference or source
(docs/citations.md), are two orders of magnitude apart and none of them is derived from a
task:

    langchain_classic AgentExecutor   `max_iterations: int | None = 15`
    LangGraph recursion_limit         "Starting in version 1.0.6, the default recursion
                                       limit is set to 1000 steps."
    OpenAI Agents SDK                 `DEFAULT_MAX_TURNS = 10`
    Claude Agent SDK ClaudeAgentOptions  `max_turns` | `int | None` | default `None`

Three defaults spanning 10 to 1000 and one framework that declines to have one. They are
cost and runaway-loop backstops, not non-convergence detectors. So this instrument always
reports the DISTRIBUTION, which needs no constant, and flags only against a threshold the
caller sets explicitly (`--flag-span-tool-calls` / `ARI_FLAG_SPAN_TOOL_CALLS`,
`--flag-no-tool-streak` / `ARI_FLAG_NO_TOOL_STREAK`). Pick one from your own
distribution; whatever you pick is a starting point for recalibration, not a validated
constant, and this tool does not know a better one.

(Two of those four numbers arrived here wrong, from a research summary that had been
written by reading about the frameworks rather than reading them -- one default was
reported as a 5-10 range where the source assigns a single 10, another cited a superseded
value. Re-fetching each page is rule 4 of docs/method.md, and it changed the answer
again.)

WHAT A "REAL USER TURN" IS, AND THE TRAP IN NOT ASKING. Spans are delimited by operator
input, so everything turns on which `type: "user"` entries ARE operator input -- and in a
real Claude Code transcript, most are not. The engine hands every tool's output back as a
`user` entry carrying `tool_result` blocks, so counting those would cut a span at every
single tool call and this signal would report that nothing ever exceeds anything.
`isMeta: true` entries are synthetic prompts the engine wrote, including the relaunch stub
`"Continue from where you left off."`, which read as the operator asking for more work
when nothing asked for anything.

    One private corpus of 43 real transcripts, surveyed once with this file's own
    classifier while building it: of 4552 `user` entries, 4099 were tool_result carriers,
    26 were isMeta, 2 were engine-injected `<task-notification>` prompts, and 425 were
    actual operator input. A naive reader of `type: "user"` would therefore have
    overstated the operator's turns by roughly tenfold, and cut every span to about one
    tool call. That is a SNAPSHOT of a corpus that was still being written to as it was
    read -- an earlier pass of the same directory counted 4466 entries -- and it is a
    corpus nobody else can run, which is exactly why no number from it is used as a
    constant anywhere in this instrument. `--survey-user-entries` prints the same census
    for YOUR corpus; every normal run prints the per-reason exclusion counts for the
    transcripts it just read. Check that shape before trusting signal 1 on an unfamiliar
    format.

Every exclusion is structural -- a block type or an exact field, never a substring of
anyone's text -- and each is counted and printed, so an over-eager rule is visible rather
than silent. Where an entry cannot be classified it counts as REAL, which splits a span
and lowers every count: of the two directions to be wrong in, that is the one that cannot
manufacture a finding.

SIGNAL 2 IS NOT IMPLEMENTED HERE, deliberately and visibly. Repeated/near-duplicate tool
invocations are the signal published work actually names as dominant: MAST's failure
taxonomy, built from "1600+ annotated traces collected across 7 popular MAS frameworks",
rates "step repetitions (FM-1.3, 15.7%)" the most frequent of its 14 modes -- the next
highest is reasoning-action mismatch at 13.2% -- and that is identical-action-without-
state-change, which needs no LLM to detect. It is exactly what this instrument should
flag next. It is deferred rather
than approximated because a tool-NAME-only match ("Bash called 9 times") is noise in a
Bash-heavy session, and the command-level clustering that makes it meaningful is the BY
BASH COMMAND view above (see `bash_cluster`), which landed only now -- signal 2 is the
next thing to build ON it, and is still not approximated under its own name here. `--help-blind` says so, the run prints it as NOT IMPLEMENTED, and no
partial version of it is reported under its name.

`--json` emits a compact digest (totals, the breakdowns above, the loop flags, the N
costliest turns by cache-read, a cache_read:output ratio per node) sized to hand to an LLM
for qualitative review. It is deliberately NOT the raw transcript, which runs to tens of MB.

ONE MEASUREMENT TRAP THIS REPO WOULD OTHERWISE HIT. A single logical assistant turn is
written to the transcript as SEVERAL `.jsonl` entries that share one `message.id` -- a
`thinking` block, then one entry per `tool_use` block -- and EVERY one of those entries
repeats the SAME `usage` object. Summing `usage` over raw entries therefore multiplies a
turn's cost by its block count. This instrument keys every turn by `message.id` and counts
its `usage` ONCE, unioning the `tool_use` names across the turn's entries. The positive
control (`--self-test`) builds one turn as three entries repeating a single `usage` and
fails if the dedup regresses -- a reproducible stand-in for the production transcripts
where this was first seen.

Read-only: opens the transcript and manifest paths for READING only. No writes, no
network, no database. `--self-test` builds its fixture in memory and writes nothing.

WHAT WOULD THIS PRINT IF IT WERE BLIND?
    Its most dangerous blind output is `total=0`, which looks exactly like a free
    session. So a zero is never printed as a measurement: the run REFUSES (exit 2) in
    every case where it measured nothing, and prints an attribution-coverage line on
    every run so a partial blindness is visible rather than averaged away. Five ways to
    be blind, and the guard for each:

      1. NO TRANSCRIPT. A path that does not exist or cannot be read yields no turns. If
         NO source is readable the run REFUSES (exit 2): "I looked in the wrong place"
         must not share a shape with "this session was free".
      2. A TRANSCRIPT WHOSE USAGE FIELDS ARE WRONG -- wholly or in PART. If NO assistant
         turn carries any recognised `usage` field (an older format, every field renamed)
         the run REFUSES (exit 2) rather than printing `total=0`. The subtler case is a
         SINGLE renamed field: three fields still parse, so the turn is still "usage-
         bearing", but the renamed one reads as a silent 0 for a whole token dimension --
         `cache_read` quietly dropping to 0 across a real session is exactly the "looks
         free" trap. So presence is tracked PER FIELD: any canonical field absent from
         EVERY usage-bearing turn is treated as unmeasured and the run REFUSES (exit 2),
         and a field missing from SOME turns prints a WARNING that its total is a floor.
         A zero cost is a MEASURED zero only when that field was actually present.
      3. A MANIFEST NODE WHOSE PATH IS MISSING/UNREADABLE. It is reported as `n/a` in the
         rollup and contributes nothing to its parent's sum -- never silently dropped and
         never counted as 0, because a child that cost nothing and a child we could not
         read must not look the same. The count of `n/a` nodes is printed.
      4. THE BY-AGENT SPLIT SEEING NO SUBAGENT, in either of two ways.
         (a) SUBAGENT TYPE NOT RECORDED. Some sidechain turns carry no `attributionAgent`.
         They are still counted as sidechain spend, under "(subagent type not recorded)",
         never merged into the main thread -- the by-agent split would otherwise under-
         report subagent cost whenever the label happened to be absent.
         (b) THE SIDECHAIN TURNS ARE IN A FILE NOBODY PASSED IN -- the dangerous one,
         because it prints as a clean `0` rather than as a gap. This instrument counts
         sidechains in the transcripts it is GIVEN, and at least one real on-disk layout
         writes a subagent's turns to a SEPARATE transcript file beside the parent's
         rather than inline: measured on 109 real transcripts, the separate per-subagent
         files were 100% sidechain turns (2179 of 2179) and the session transcripts were
         0% (0 of 5805). Both numbers are correct, and a caller who enumerates only
         session transcripts therefore gets `sidechain_turns: 0` NO MATTER HOW MUCH
         delegation happened. So a zero here means "none in these files", never "no
         subagent ran", and the run says so on the line itself rather than leaving the
         reader to infer it. If you need the subagent spend, pass those files too (or
         name them in a `--manifest`); if a tree really did delegate by spawning separate
         SESSIONS instead, that is the subsession rollup's job, not this split's.
      5. A BASH TURN WHOSE COMMAND IS UNREADABLE OR NOT PUBLIC. The by-command
         clustering reads `tool_use.input.command`, and two separate things can stop it
         naming a command -- kept as two buckets, because collapsing them would hide
         which one happened. When the command is missing or does not reduce to a plain
         command word, the turn clusters into `(unparsed)`. When it IS a well-formed word
         but not on the public-command allowlist -- a local script, a branch or worktree
         name -- it clusters into `(other)`, counted and never named, because a published
         key is not anonymised and must not carry an internal name. Neither bucket is
         ever silently merged into a real command's, and neither is dropped, so the two
         counts together are the honest measure of what the heuristic did not name. The
         reassuring-looking failure here is a `(other)` that quietly swallows a command
         you cared about: its DISTINCT COUNT is printed next to it for exactly that
         reason, and the allowlist is a short, auditable list you are meant to read.

    Three more ways to be blind belong to the LOOP FLAGS specifically, because the flags'
    dangerous output is the opposite of a zero -- it is a reassuring "nothing flagged":

      6. NO IDENTIFIABLE USER TURNS AT ALL. A transcript whose `user` entries are all
         `tool_result` carriers or all `isMeta` -- a sidechain-only export, a format whose
         user entries are shaped differently, a mid-session fragment -- has no span
         boundaries, so signal 1 cannot be computed. The whole file would collapse into
         ONE span, whose call count then exceeds any threshold and reads as a dramatic
         finding about a session nobody can see the turns of. So zero real user turns is
         reported as UNMEASURED and NO span is flagged -- the one case where this
         instrument declines to flag precisely because flagging would be easy.
      7. A THRESHOLD OF 0 OR LESS. `0` flags every span including the empty ones (a user
         turn answered with no tool call at all is a span of 0), and a negative threshold
         flags all of them unconditionally. A detector that fires on everything
         discriminates nothing, so a threshold below 1 is REFUSED (exit 2) rather than
         honoured -- in the CLI flag and in the env var alike, since an env var you
         believe you set is not an env var you got. An unparseable env value is refused
         the same way, never silently ignored back to "no threshold".
      8. NO THRESHOLD SET AT ALL -- the default. "0 spans flagged" would then be a
         statement about the absent threshold, not about the session, and it would look
         exactly like a clean run. So the unset case prints NOT COMPUTED, a THIRD state
         distinct from both "flagged" and "clean", and the distribution is printed anyway
         so the run is still worth something without one.

    The positive controls: `--self-test` builds a synthetic transcript with hand-computed
    totals (including a split turn, a sidechain, and a no-tool turn) and asserts the
    computed numbers match EXACTLY, and a second fixture of known Bash commands
    (`git`x2, `pytest`, one unparseable, one with a QUOTED multi-word env value, and one
    with an env prefix BEFORE a `cd ...&&` chain) asserts the clustering matches exactly.
    A PUBLISHED-VOCABULARY control (ARI#11) runs commands whose first word is a
    plausible internal name -- `./roast-hc9999-fake-example`, a `poll_ci*.sh` shape, a
    worktree path -- and asserts each one clusters to `(other)`, never to the literal
    name, that the name appears nowhere in either output mode, that every key emitted is
    inside the closed vocabulary, and that `git`/`pytest`/`npm` STILL keep their own keys,
    so the gate cannot pass by bucketing everything. It is a separate control from the one
    below rather than an extension of it, because the secret-value control passed
    throughout the period the name leak was live: a script name is not a quoted value, and
    a guard aimed at values cannot see it.

    A PRIVACY control runs six commands whose env value is a quoted multi-word secret
    through the text report AND the `--json` digest, with and without `--anonymise`, and
    fails if any WORD of any value appears anywhere in either -- the leak it exists to
    catch shipped once, as a cluster key `efgh` derived from
    `AWS_SECRET_ACCESS_KEY="abcd efgh ijkl"`. That control validates itself first: it
    renders a secret-free fixture and fails if a sentinel word turns up there, so it
    cannot pass by matching the report's own prose. TWO
    negative controls run in the same pass, because a control defeated by a one-field
    change is no control: (a) rename ALL usage fields and assert every turn reads as
    unmeasured and the run refuses; (b) rename a SINGLE field and assert the per-field
    diagnostic flags that one dimension as absent and the run still refuses -- the weaker
    all-or-nothing check passed (b) silently.

    The flags carry their own controls in the same pass, built so each can fail on a
    change the others survive: a POSITIVE fixture that exceeds both thresholds and must be
    flagged; a NEGATIVE fixture under both that must report clean (not "not computed");
    a NEAR-MISS pair at exactly the threshold and exactly one over, asserting the boundary
    is strictly `>` in both directions rather than trivially always-true; a span fixture
    whose `user` entries are `tool_result` carriers and `isMeta` stubs, asserting it reads
    as ONE span rather than as one per tool call (delete either exclusion and this control
    goes red); and the refusals for a zero/negative/unparseable threshold and for a
    transcript with no real user turn. Nine more legs exist because a mutation sweep found
    each of them missing -- including one that caught a real leak: `--anonymise` was
    hashing the span rows and not the FLAGGED rows, which are separate copies of the same
    records, so the one output mode whose job is to omit the transcript path was printing
    it. That leg now asserts over the whole serialised digest.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import sys
import textwrap

# The four token fields every assistant turn's `usage` block carries. A turn is
# "usage-bearing" iff at least one of these is present AND numeric -- which is exactly the
# test the negative control trips by renaming them.
USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens")

NO_TOOL = "(no tool call)"
NO_AGENT_TYPE = "(subagent type not recorded)"
MAIN = "main thread"
UNPARSED = "(unparsed)"
# A well-formed command word that is not a PUBLIC one. Parenthesised like the other
# sentinels so it can never collide with a real command (`_CMD_WORD` admits no parens).
OTHER = "(other)"

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")  # a `VAR=value` token, value already unquoted
_CHAIN_OPS = ("&&", "||", "|", ";", "&")
_CMD_WORD = re.compile(r"^[A-Za-z0-9._-]+$")

# The PUBLISHED cluster-key vocabulary is CLOSED: a key is a word from this set, `(other)`,
# or `(unparsed)`, and nothing else can ever appear. That closure IS the privacy property.
#
# `_CMD_WORD` alone was not enough, and the gap was measured rather than theorised. It is a
# SHAPE test -- it admits any basename -- so a locally-named script or a worktree/branch name
# passed it and was published verbatim. Found on real data while re-vendoring this file into a
# downstream consumer (ARI#11, which records the observed keys; they are internal names and
# are deliberately not copied into this repo, which stays harness-agnostic). None were
# credentials -- they were branch, worktree and local-script names that a published digest has
# no business carrying, and `--anonymise` does not touch a cluster key by design.
#
# WHY AN ALLOWLIST AND NOT A HASH. The other candidate fix was to publish `cmd-<8hex>` for an
# unrecognized word. That obscures the name without removing it: a command/branch name is
# low-entropy and the candidate space is enumerable, so `cmd-<hash>` is confirmable by anyone
# willing to guess, which does not meet "structurally impossible to reach the digest". A
# closed vocabulary does, and it is auditable by reading this list.
#
# HOW THIS LIST WAS BUILT, and the asymmetry that governs it. Omitting a public command costs
# a little SIGNAL (its turns land in `(other)`); admitting a non-public one costs the
# GUARANTEE. So the bias is toward omission, and the bar for adding a word is that it names a
# tool published to the world rather than something a repo or a machine happens to call its
# own. Three sources, kept distinct so the boundary can be reviewed:
#
#   1. MEASURED. Every public command word observed in a survey of 102 real transcripts
#      (5517 Bash turn-attributions, 67 distinct keys). With this list, `(other)` absorbs
#      11 of 5517 turns -- 0.20% -- and 7 of the 67 distinct keys; those 7 are exactly two
#      worktree/branch-shaped names and three local `.sh` scripts, i.e. the leak itself.
#      The public commands are the HEAD of that distribution and the internal names its
#      TAIL, which is why gating costs almost no signal.
#   2. THIS REPO'S OWN FIXTURES, so existing controls keep meaning what they say: aws,
#      mysql, npm, pytest.
#   3. STANDARD UTILITIES AND WIDELY-PUBLISHED DEV CLIs not present in either survey but
#      whose absence would over-bucket another corpus. This tier is a judgement call, not a
#      measurement, and is labelled as such rather than presented as data. The second survey
#      below independently makes the case for it: `make`, `docker`, `jq`, `ssh`, `go` and
#      `cargo` appear in NEITHER corpus, which is one fleet's usage pattern rather than a
#      statement that they are not public commands.
#   4. A SECOND, INDEPENDENT SURVEY, relayed from the downstream consumer's own re-vendor
#      probe (a real 70-child dev tree, 34 measurable sessions, unioned with 5 separate
#      probe transcripts -- 57 public words; its artefact is cited in ARI#11).
#      It contributed `free`, `pgrep`, `setsid`, `type`, `uptime`. Its own caveats are worth
#      keeping: the two halves of it disagreed on five tail words, and 13 of its 57 occurred
#      in exactly ONE session -- so this list is a seed that should widen with evidence, and
#      must NOT be frequency-thresholded, or real-but-rare public commands would be bucketed.
#
# ONE WORD DECLINED, recorded rather than quietly dropped: that survey lists `run` as public
# and this list omits it. There is no standard `run` executable; it is `cargo run`/`uv run`
# as a subword, a shell alias, or -- the case that matters -- a local `./run` script. It was
# also one of the ambiguous bare words the first survey bucketed. Under the asymmetry above
# an ambiguous word belongs in `(other)`, so it stays out until something names it as a
# published tool.
_PUBLIC_COMMANDS = frozenset("""
apt awk bash cat cd claude command cp curl date df diff echo env export find for gh git
grep head hostname id if journalctl jq ls mkdir mv pip pkill printenv printf ps pwd python
python3 read rm rmdir sed set sh sha256sum sleep ss sqlite3 sudo systemctl tail test time
timeout tmux touch until uv wc which whoami
free pgrep setsid type uptime

aws mysql npm pytest

basename chmod chown cut dirname du gunzip gzip kill ln md5sum mktemp nc nohup realpath
rsync scp seq sort ssh stat tar tee tr uniq unzip wait wget xargs zip
cargo cmake docker go gradle java make mvn node npx perl pnpm ruby rustc yarn
case do done elif else esac eval exec fi function local return shift source then trap
unset while
""".split())


def _tokenise(cmd):
    """Split a command into shell words, QUOTE-AWARE, with `&&`/`;`/`|` as their own
    tokens even when unspaced (`cd /x&&make`). Returns None if the string does not
    tokenise (an unbalanced quote) -- the caller must then not peel anything.

    Quote-awareness is the whole point and is load-bearing for PRIVACY, not tidiness: a
    peel step that does not know where a quoted value ENDS can stop in the middle of one
    and hand the REMAINDER of a secret to the classifier, which then publishes a word of
    it as a cluster key. Tokenising the WHOLE string once, before any peeling, makes
    `VAR="abcd efgh"` a single token that is either consumed entirely or not at all.
    """
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _bash_first_word(cmd):
    """The command's OWN first word -- basename, after peeling leading `cd ...&&` chains and
    `VAR=` env prefixes -- or None if the string does not reduce to a plain command word.

    Split out of `bash_cluster` so the allowlist gate and the `(other)` fingerprint derive
    from the SAME word. Two separate derivations of "the first word" could disagree, and a
    gate that passed a word the fingerprint never saw (or vice versa) would make the
    distinct-count a statement about a different string than the one being judged.

    Two properties make the word itself safe to derive, both positive-controlled in
    `--self-test`:

      * the leading `cd ... &&` chains and `VAR=value` env prefixes are peeled in ONE
        loop that retries BOTH shapes until neither matches, so they compose in either
        order (`CI=1 cd /repo && npm test` reduces to `npm`, not to `cd`). Two separate
        single-pass loops could only ever strip them in one fixed order.
      * peeling happens over quote-aware TOKENS (`_tokenise`), so a quoted value is one
        token -- consumed whole or left whole. If the string does not tokenise at all we
        peel NOTHING and fall back to the first whitespace word of the ORIGINAL string,
        which is by construction the command's own first word rather than anything from
        inside a value.

    Returning the word is NOT the same as publishing it -- that is `bash_cluster`'s job and
    it gates this through `_PUBLIC_COMMANDS`. Nothing may print this function's return value.
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    toks = _tokenise(cmd)
    if toks is None:
        # Unbalanced quoting: classify the original's first word WITHOUT peeling. Peeling
        # here is what used to truncate a quoted value mid-way and leak the rest.
        head = cmd.strip().split()
        toks = head[:1]
    else:
        i = 0
        while i < len(toks):                    # peel BOTH shapes, in any order, to a fixed point
            if _ENV_ASSIGN.match(toks[i]):      # `VAR=value` -- one whole token
                i += 1
                continue
            if toks[i] == "cd":                 # `cd <dir> && ` -- up to the chaining `&&`
                j = i + 1
                while j < len(toks) and toks[j] not in _CHAIN_OPS:
                    j += 1
                if j > i + 1 and j < len(toks) and toks[j] == "&&":
                    i = j + 1
                    continue
            break
        toks = toks[i:]
    if not toks:
        return None
    first = toks[0].split("/")[-1]              # /usr/bin/python3 -> python3
    return first if _CMD_WORD.match(first) else None


def bash_cluster(cmd):
    """Cluster a Bash command by its first real token -- a cheap, MECHANICAL heuristic,
    not a parse of what the command means.

    `git commit` and `git push` both cluster under `git`; that is deliberate (the baseline
    is first-token-after-cd-chains-and-env-prefixes).

    THE RETURN VALUE IS A CLOSED VOCABULARY: a word from `_PUBLIC_COMMANDS`, `(other)`, or
    `(unparsed)`. Nothing else can come out of here, whatever goes in. That is the whole
    privacy property, because the key is PUBLISHED (text report, `--json` digest) and
    `--anonymise` deliberately does NOT touch it -- it hashes transcript paths and manifest
    labels, not evidence. There is no filter downstream; safety has to be a property of the
    derivation, and the derivation ends here.

    Three gates, in order, each positive-controlled in `--self-test`:

      1. the word is derived quote-awarely and to a fixed point (`_bash_first_word`), so it
         cannot be a fragment of a quoted VALUE -- the bug that published `efgh` out of
         `AWS_SECRET_ACCESS_KEY="abcd efgh ijkl"`.
      2. it must be a plain command word at all (`_CMD_WORD`), else `(unparsed)`. On the
         unbalanced-quote path this is the only thing standing between the output and a
         fragment of a value, so there declining to classify IS the control.
      3. it must be a PUBLIC command (`_PUBLIC_COMMANDS`), else `(other)`. Gates 1 and 2 are
         shape tests and a local name can satisfy both: a `poll_ci.sh`-shaped script name
         and a `<verb>-<ticket>-<slug>`-shaped worktree name are well-formed words that
         were published verbatim before this gate existed (ARI#11).

    `(other)` is deliberately one bucket and carries no identity. How MANY distinct
    unrecognized commands it covers is reported separately (`bash_other_distinct`), which
    keeps the aggregate-cost signal readable -- one dominant unknown versus twenty scattered
    ones -- without naming any of them.
    """
    w = _bash_first_word(cmd)
    if w is None:
        return UNPARSED
    return w if w in _PUBLIC_COMMANDS else OTHER


def bash_other_fingerprint(cmd):
    """A stable, non-reversible id for a command that bucketed into `(other)`, so distinct
    ones can be COUNTED -- or None when the command did not bucket.

    NEVER PUBLISHED. Only `len()` of a set of these is (see `bash_other_distinct`). It is a
    full-strength digest and not `anonymise`'s truncation precisely so that it is useless as
    a lookup even if some future change does print it: the point is to count, and counting
    needs only that two different words differ.
    """
    w = _bash_first_word(cmd)
    if w is None or w in _PUBLIC_COMMANDS:
        return None
    return hashlib.sha256(w.encode("utf-8", "surrogateescape")).hexdigest()


def bash_other_distinct(turns):
    """How many DISTINCT unrecognized commands these turns ran -- cardinality WITHOUT
    identity. This is what keeps `(other)` honest: a single bucket holding 400 turns reads
    very differently depending on whether it is one script run 400 times or 40 scripts run
    ten times each, and that difference is reportable without naming one of them."""
    fps = set()
    for t in turns:
        fps |= t.get("bash_other_fp") or set()
    return len(fps)


def anonymise(tag):
    """Hash an identifier the same way the other instruments do: `sess-<8 hex>`.

    Applied only to transcript paths and caller-supplied manifest labels -- the things
    that can identify a session. Tool names, subagent TYPES, and every token count are
    left untouched: they are the evidence, not the identifier.
    """
    if not tag:
        return tag
    return "sess-" + hashlib.sha256(str(tag).encode()).hexdigest()[:8]


def num(v):
    """A usage field that is absent (None) or non-numeric contributes 0 to a sum, but the
    turn only counts as usage-bearing if SOME field was numeric -- see collect_turns."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def collect_turns(lines):
    """Reduce a transcript's lines to one record per logical assistant turn.

    Keyed by `message.id` so a turn split across a thinking entry and several tool_use
    entries -- all repeating the same `usage` -- is counted ONCE, with its tool names
    unioned and its tool CALLS counted by distinct `tool_use` id. Returns (turns, stats).

    Also walks the `user` entries, which the token breakdown has no use for but the loop
    flags are delimited by: each turn is stamped with the index of the span it falls in
    (`span`), advanced only by a REAL user turn -- see user_exclusion_reason. Turns before
    the first real user turn carry span 0.
    """
    turns = {}            # message-id -> turn record (insertion order preserved)
    order = []
    stats = dict(bad_json=0, assistant_entries=0, user_entries=0,
                 real_user_turns=0, user_excluded={})
    span = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            stats["bad_json"] += 1
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "user":
            stats["user_entries"] += 1
            why = user_exclusion_reason(d)
            if why:
                stats["user_excluded"][why] = stats["user_excluded"].get(why, 0) + 1
            else:
                stats["real_user_turns"] += 1
                span += 1
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        stats["assistant_entries"] += 1
        # message.id is the API response id and the right dedup key. Fall back to the
        # per-request/entry id so a turn missing it still counts as its own turn rather
        # than merging with another.
        mid = msg.get("id") or d.get("requestId") or d.get("uuid") \
            or ("_noid", stats["assistant_entries"])

        names, call_ids, idless = set(), set(), 0
        bash, bash_other = set(), set()
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                # structural match: block type AND the exact name field, never a substring
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    nm = b.get("name")
                    if isinstance(nm, str) and nm:
                        names.add(nm)
                    # Bash is the only tool whose `input.command` we read: cluster by it.
                    if nm == "Bash":
                        inp = b.get("input")
                        cmdline = inp.get("command") if isinstance(inp, dict) else None
                        bash.add(bash_cluster(cmdline))
                        # fingerprint, not the word: this is how `(other)` stays countable
                        # without the run holding on to a single local name anywhere.
                        fp = bash_other_fingerprint(cmdline)
                        if fp:
                            bash_other.add(fp)
                    # a call is counted even when its NAME is missing: it happened, and
                    # counting only named calls would let a missing label manufacture a
                    # "no tool call" streak (see no_tool_streaks).
                    bid = b.get("id")
                    if isinstance(bid, str) and bid:
                        call_ids.add(bid)
                    else:
                        idless += 1

        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None

        t = turns.get(mid)
        if t is None:
            t = dict(tools=set(), bash=set(), bash_other_fp=set(),
                     usage=None, has_usage=False,
                     sidechain=False, agent=None,
                     call_ids=set(), calls_idless=0, span=span)
            turns[mid] = t
            order.append(mid)
        t["tools"] |= names
        t["bash"] |= bash
        t["bash_other_fp"] |= bash_other
        t["call_ids"] |= call_ids
        t["calls_idless"] += idless
        if d.get("isSidechain"):
            t["sidechain"] = True
        if not t["agent"] and isinstance(d.get("attributionAgent"), str):
            t["agent"] = d.get("attributionAgent")
        if usage is not None and not t["has_usage"]:
            vals = {k: usage.get(k) for k in USAGE_FIELDS}
            if any(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in vals.values()):
                t["usage"] = vals
                t["has_usage"] = True
    return [turns[m] for m in order], stats


def totals_of(turns):
    """Sum each usage field over the usage-bearing turns (None -> 0)."""
    acc = {k: 0 for k in USAGE_FIELDS}
    n = 0
    for t in turns:
        if not t["has_usage"]:
            continue
        n += 1
        for k in USAGE_FIELDS:
            acc[k] += num(t["usage"].get(k))
    acc["turns"] = n
    return acc


def ratio(acc):
    """cache_read : output. The overhead a session pays re-reading its own cached prefix
    per unit of new output. None when there is no output to divide by (not 0 -- an
    undefined ratio and a zero ratio are different facts)."""
    o = acc.get("output_tokens", 0)
    return (acc.get("cache_read_input_tokens", 0) / o) if o else None


def group(turns, keyfn):
    out = {}
    for t in turns:
        if not t["has_usage"]:
            continue
        out.setdefault(keyfn(t), []).append(t)
    return {k: totals_of(v) for k, v in out.items()}


def tool_key(t):
    return "+".join(sorted(t["tools"])) if t["tools"] else NO_TOOL


def agent_key(t):
    if not t["sidechain"]:
        return MAIN
    return t["agent"] or NO_AGENT_TYPE


def bash_key(t):
    """The set of Bash command-clusters a turn ran (turn-grain, like tool_key). Only
    meaningful for turns that called Bash at all."""
    return "+".join(sorted(t["bash"])) if t["bash"] else None


def group_bash(turns):
    """Group the Bash-invoking turns by their command-cluster set."""
    return group([t for t in turns if t["bash"]], bash_key)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def coverage(turns):
    total = len(turns)
    ub = [t for t in turns if t["has_usage"]]
    # PER-FIELD presence, not just "has any field": a single renamed field leaves a turn
    # usage-bearing (the other three parse) while silently zeroing one whole dimension.
    present = {k: 0 for k in USAGE_FIELDS}
    full = 0
    for t in ub:
        got = sum(1 for k in USAGE_FIELDS if _is_num(t["usage"].get(k)))
        for k in USAGE_FIELDS:
            if _is_num(t["usage"].get(k)):
                present[k] += 1
        if got == len(USAGE_FIELDS):
            full += 1
    side = [t for t in turns if t["sidechain"]]
    side_no_type = sum(1 for t in side if not t["agent"])
    return dict(total_turns=total, usage_bearing=len(ub),
                without_usage=total - len(ub),
                fully_attributed=full, field_present=present,
                fields_entirely_absent=[k for k in USAGE_FIELDS if present[k] == 0],
                fields_partially_absent=[k for k in USAGE_FIELDS
                                         if 0 < present[k] < len(ub)],
                sidechain_turns=len(side), sidechain_without_type=side_no_type)


def refusal_reason(cov):
    """Return why this corpus cannot be measured, or None if it can.

    Two refusals, so a renamed field cannot pass as a measured zero: no usage AT ALL, or
    a whole canonical token dimension absent from every usage-bearing turn. A field absent
    from only SOME turns is a floor (a WARNING), not a refusal -- it was measured somewhere.
    """
    if cov["usage_bearing"] == 0:
        return (f"{cov['total_turns']} assistant turns found but NONE carried a recognised "
                f"usage field, so total=0 would be no measurement, not a free session")
    if cov["fields_entirely_absent"]:
        return (f"usage was found, but the field(s) "
                f"[{', '.join(cov['fields_entirely_absent'])}] are ABSENT from every "
                f"usage-bearing turn -- a renamed or unsupported field reads as a silent 0 "
                f"for that whole dimension, which is no measurement")
    return None


# ---------------------------------------------------------------------------
# LOOP FLAGS -- mechanical signals from transcript STRUCTURE. FLAGS, NOT VERDICTS.
# ---------------------------------------------------------------------------
# Why a `type: "user"` entry can fail to be a REAL user turn. Every reason here is
# structural -- a block type or an exact field, never a substring of the text -- and every
# one is COUNTED and PRINTED on each run, because an exclusion rule this load-bearing must
# be visible: get it wrong in the generous direction and every tool call ends a span, get
# it wrong in the strict direction and a relaunch artefact reads as the operator asking
# for more work.
USER_EXCLUSIONS = (
    ("isMeta",
     "isMeta:true -- a prompt the engine synthesised, e.g. the relaunch stub "
     "\"Continue from where you left off.\""),
    ("tool_result",
     "carries tool_result blocks only -- the engine handing a tool's output back, which "
     "is the overwhelming majority of user entries in a real transcript"),
    ("engine_injected",
     "promptSource names the engine itself (e.g. a <task-notification> injected when a "
     "background task ends), with no isMeta to mark it"),
)

# `promptSource` values that mean "the engine wrote this prompt, not a person". Engine-
# specific by nature: a transcript format without the field simply has no entry excluded
# this way, which is the floor direction (such an entry reads as a real user turn and
# splits a span) rather than the one that invents work.
ENGINE_PROMPT_SOURCES = ("system",)


def user_exclusion_reason(d):
    """Why this `user` entry is NOT operator input, or None if it is.

    First match wins and the order is the order of USER_EXCLUSIONS, so an isMeta entry
    that also carries tool_result blocks is counted once, under isMeta.
    """
    if d.get("isMeta") is True:
        return "isMeta"
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        # `kinds` empty (an empty content list) is deliberately NOT excluded: an entry we
        # cannot classify counts as a real user turn, which splits a span and LOWERS every
        # span's count. Erring toward under-flagging is the direction that cannot
        # manufacture a finding.
        if kinds and kinds <= {"tool_result"}:
            return "tool_result"
    if d.get("promptSource") in ENGINE_PROMPT_SOURCES:
        return "engine_injected"
    return None


def turn_calls(t):
    """How many tool calls one logical assistant turn made.

    NOT `len(t["tools"])`: that is the SET of tool names, so a turn that called Bash nine
    times would count as one. Counted by distinct `tool_use` block id, which is the
    engine's own per-call identity and survives the same split-turn trap `message.id`
    handles for usage (measured on 2319 real tool_use blocks: none missing an id, and no
    id repeated inside one turn). A block with no id cannot be deduped, so each such
    occurrence counts as its own call -- an idless block written twice would over-count,
    which is stated rather than silently assumed away.
    """
    return len(t["call_ids"]) + t["calls_idless"]


def quantile(xs, pct):
    """Nearest-rank (inverse-ECDF) quantile over an already-sorted list; None when empty.

    `pct` is an INTEGER percentile and the rank is computed with integer ceiling division,
    deliberately: the obvious float form `int(round(pct/100 * (n-1)))` picks its index by
    Python's banker's rounding at exact .5 and by whichever way `0.9 * n` last rounded in
    binary, so a p50 over two spans would depend on a tie-break convention no reader of
    the output could predict. This form has no floats in it.

    No interpolation either -- these are counts of tool calls, and a p90 of 20.5 calls is
    not a thing that happened. None for an empty list rather than 0: an undefined quantile
    and a measured zero are different facts, the same rule `ratio()` follows above.
    """
    if not xs:
        return None
    rank = -(-pct * len(xs) // 100)          # integer ceil(pct/100 * n)
    return xs[min(len(xs), max(1, rank)) - 1]


def spans_of(turns, real_user_turns):
    """One row per span between consecutive real user turns, in transcript order.

    EMPTY SPANS ARE INCLUDED. A user turn answered with no tool call at all is a span of
    0, and dropping those would quietly raise every quantile of the distribution and make
    a chatty session look busy. It is also exactly why a threshold of 0 is refused: it
    would flag them.

    Span 0 holds any assistant turns BEFORE the first real user turn (a transcript that
    opens mid-session, or a sidechain-only export). It is reported as the preamble and
    carried in the distribution, because the work in it is real work; it is simply not
    attributable to an operator request this file can see.
    """
    by = {}
    for t in turns:
        by.setdefault(t["span"], []).append(t)
    idxs = ([0] if 0 in by else []) + list(range(1, real_user_turns + 1))
    rows = []
    for i in idxs:
        ts = by.get(i, [])
        rows.append(dict(index=i, preamble=(i == 0),
                         turns=len(ts),
                         calls=sum(turn_calls(t) for t in ts)))
    return rows


def no_tool_streaks(turns):
    """Runs of consecutive assistant turns that called NO tool, per thread.

    Split main thread from sidechains rather than scanning the file in raw order: a
    subagent's turns are interleaved into the same transcript, so a main-thread text turn
    sitting next to a subagent's text turns would read as one long "talking" streak that
    no single thread ever had.

    A turn counts as no-tool when it made zero CALLS -- not when its tool-name set is
    empty. A `tool_use` block with no `name` therefore breaks a streak instead of
    extending it; it is a call that happened, and the alternative would invent a streak
    out of a missing label.

    Disclosed residual: two DIFFERENT subagents' turns landing adjacently inside the
    sidechain stream can merge into one apparent streak, so the sidechain figure is a
    ceiling. The main-thread figure, which is the one that matters for "a lot of talking
    without doing", has no such join.
    """
    out = {}
    for label, seq in ((MAIN, [t for t in turns if not t["sidechain"]]),
                       ("sidechains", [t for t in turns if t["sidechain"]])):
        runs, cur = [], 0
        for t in seq:
            if turn_calls(t) == 0:
                cur += 1
            else:
                if cur:
                    runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
        out[label] = dict(turns=len(seq), runs=runs)
    return out


def compute_flags(per_source, span_threshold, streak_threshold):
    """Aggregate both signals over one or more sources and decide what is flagged.

    PER SOURCE, then concatenated -- never over a merged turn list. Span indices restart
    at every transcript, so pooling turns from several files first would glue the tail of
    one session's span onto the head of another's and report a span neither one had.

    Each signal ends in exactly one of THREE states, never two:
      measured + a threshold set  -> flagged list (possibly empty == a real clean result)
      measured + no threshold     -> "not_computed" (the distribution is still reported)
      not measurable              -> "unmeasured", and nothing is flagged
    """
    spans, streaks, real_users, excluded, asst = [], {}, 0, {}, 0
    for label, turns, stats in per_source:
        real_users += stats["real_user_turns"]
        asst += len(turns)
        for k, v in stats["user_excluded"].items():
            excluded[k] = excluded.get(k, 0) + v
        for row in spans_of(turns, stats["real_user_turns"]):
            spans.append(dict(source=label, **row))
        for thread, rec in no_tool_streaks(turns).items():
            acc = streaks.setdefault(thread, dict(turns=0, runs=[]))
            acc["turns"] += rec["turns"]
            acc["runs"] += rec["runs"]

    calls = sorted(s["calls"] for s in spans)
    turns_per_span = sorted(s["turns"] for s in spans)
    # Signal 1 needs a boundary to measure between. With no real user turn there is none:
    # the file collapses into ONE span whose count beats any threshold, which is the most
    # convincing wrong answer this instrument could produce.
    s1_state = ("unmeasured" if real_users == 0
                else "not_computed" if span_threshold is None else "measured")
    s1 = dict(state=s1_state,
              real_user_turns=real_users,
              user_entries_excluded=excluded,
              spans=len(spans),
              threshold=span_threshold,
              calls_p50=quantile(calls, 50), calls_p90=quantile(calls, 90),
              calls_max=(calls[-1] if calls else None),
              turns_p50=quantile(turns_per_span, 50),
              turns_max=(turns_per_span[-1] if turns_per_span else None),
              rows=spans,
              # STRICTLY greater. A span of exactly the threshold is not flagged, in both
              # signals, and the near-miss control asserts both sides of that boundary.
              flagged=([s for s in spans if s["calls"] > span_threshold]
                       if s1_state == "measured" else []))
    longest = max([max(v["runs"]) for v in streaks.values() if v["runs"]] or [0])
    s3_state = ("unmeasured" if asst == 0
                else "not_computed" if streak_threshold is None else "measured")
    s3 = dict(state=s3_state, threshold=streak_threshold,
              assistant_turns=asst, longest=longest,
              per_thread={k: dict(turns=v["turns"],
                                  no_tool_turns=sum(v["runs"]),
                                  streaks=len(v["runs"]),
                                  longest=(max(v["runs"]) if v["runs"] else 0),
                                  flagged=([r for r in v["runs"] if r > streak_threshold]
                                           if s3_state == "measured" else []))
                         for k, v in streaks.items()})
    return dict(signal1_span_tool_calls=s1,
                # named in the digest, not left to be inferred from a missing key: an LLM
                # handed two signals out of three will reason about the two it can see
                signal2_repeated_invocations=dict(state="not_implemented",
                                                  note=SIGNAL2_DEFERRED),
                signal3_no_tool_streaks=s3)


# TODO(#4 signal 2, deferred pending #3): repeated / near-duplicate tool invocations --
# runs of the same tool+arguments with no intervening state change. Blocked on nothing
# technical; blocked on MEANING. The only duplicate key available in this file today is
# the tool NAME, and `turn_calls` above already shows why that is not enough: a real
# session calls Bash dozens of times with different commands, so name-matching would flag
# every Bash-heavy session as one long repetition and discriminate nothing -- the
# uniformity trap docs/method.md names. The command-level clustering that makes the key
# meaningful is #3's work (Bash-command clustering / agent cross-tabs); once it lands, the
# key becomes (cluster, argument-string) and this signal can be built on the span rows
# compute_flags already produces. Until then it is reported as NOT IMPLEMENTED, in the
# text report and in the --json digest, rather than shipped as a name-only approximation
# under the same name.
#
# The deferred signal is named in the output rather than left to be inferred from its
# absence. Two signals present and one missing with no explanation reads as two being all
# there is.
SIGNAL2_DEFERRED = (
    "NOT IMPLEMENTED -- repeated/near-duplicate invocations. Published work rates this "
    "the most frequent of 14 multi-agent failure modes (MAST FM-1.3, step repetition, "
    "15.7% over 1600+ annotated traces), so it is the next "
    "signal to add, not one that was judged unimportant. It is deferred rather than "
    "approximated: matching on tool NAME alone would report a Bash-heavy session as one "
    "long repetition, and the command-level clustering that makes it meaningful is being "
    "built separately. Nothing partial is reported under its name."
)


def _wrap(text, indent="    ", width=88):
    """Fold one of the prose explanations to a readable width.

    The explanations are long on purpose -- a flag nobody understands gets read as a
    verdict, which is the one thing this section must not become -- so they are wrapped
    rather than shortened.
    """
    return textwrap.fill(text, width=width,
                         initial_indent=indent, subsequent_indent=indent)


def print_flags(flags):
    s1 = flags["signal1_span_tool_calls"]
    s3 = flags["signal3_no_tool_streaks"]
    print("\nLOOP FLAGS")
    print(_wrap("Mechanical signals, computed from transcript STRUCTURE only -- turn "
                "boundaries and tool_use block counts -- never from judging what was "
                "said. THESE ARE FLAGS, NOT VERDICTS: a flagged span means an unusual "
                "amount of work by the threshold YOU set, which is a pointer to look. It "
                "is not a finding that the work was wasted, and no threshold here is a "
                "validated constant.", indent="  "))

    print(f"\n  real user turns (span boundaries): {s1['real_user_turns']}")
    if s1["user_entries_excluded"]:
        print("  user entries NOT counted as operator input, by reason:")
        for key, why in USER_EXCLUSIONS:
            n = s1["user_entries_excluded"].get(key, 0)
            if n:
                print(f"    {key}: {n}")
                print(_wrap(why, indent="      "))
    else:
        print("  user entries NOT counted as operator input: none")
        print(_wrap("No isMeta, tool_result-only or engine-injected user entry was seen "
                    "at all. For a real Claude Code transcript that is itself unusual -- "
                    "tool results come back as user entries -- so check that this is the "
                    "format you think it is.", indent="    "))

    print("\n  SIGNAL 1 -- tool calls per span between consecutive real user turns")
    if s1["state"] == "unmeasured":
        print(_wrap("UNMEASURED: not one real user turn was found, so this transcript has "
                    "no span boundaries at all. The whole file would read as ONE span and "
                    "would exceed any threshold, so NOTHING is flagged here: a dramatic "
                    "number about a session whose turns cannot be seen is worse than no "
                    "number. See --help-blind, blind mode 6."))
    else:
        print(f"    spans: {s1['spans']}  tool calls per span: "
              f"p50={s1['calls_p50']} p90={s1['calls_p90']} max={s1['calls_max']}"
              f"  (assistant turns per span: p50={s1['turns_p50']} "
              f"max={s1['turns_max']})")
        if s1["state"] == "not_computed":
            print("    threshold: NOT SET -> NOT COMPUTED")
            print(_wrap("Not a clean result and not a dirty one: no span was tested. No "
                        "default ships because no published threshold is grounded -- the "
                        "frameworks' own ceilings span 10 to 1000 (each read at first "
                        "hand; see docs/citations.md) and exist as cost backstops, not as "
                        "non-convergence detectors. Pick one from the distribution above "
                        "via --flag-span-tool-calls N or ARI_FLAG_SPAN_TOOL_CALLS, and "
                        "treat it as a starting point for recalibration.",
                        indent="      "))
        else:
            print(f"    threshold: > {s1['threshold']} tool calls -- caller-set, strictly "
                  f"greater, so a span of exactly {s1['threshold']} is NOT flagged")
            if s1["flagged"]:
                print(f"    FLAGGED spans: {len(s1['flagged'])} of {s1['spans']}")
                for s in s1["flagged"]:
                    where = ("preamble, before any user turn" if s["preamble"]
                             else f"span {s['index']}")
                    print(f"      {where}: calls={s['calls']} turns={s['turns']}")
            else:
                print(f"    FLAGGED spans: 0 of {s1['spans']} -- a MEASURED clean result "
                      f"against this threshold")

    print("\n  SIGNAL 2 -- repeated / near-duplicate invocations: NOT IMPLEMENTED")
    print(_wrap(SIGNAL2_DEFERRED, indent="      "))

    print("\n  SIGNAL 3 -- consecutive assistant turns that called no tool")
    if s3["state"] == "unmeasured":
        print("    UNMEASURED: no assistant turn was found at all.")
    else:
        print(f"    longest streak anywhere: {s3['longest']} turn(s)")
        for thread, rec in sorted(s3["per_thread"].items()):
            print(f"    {thread}: {rec['turns']} turns, {rec['no_tool_turns']} with no "
                  f"tool call, in {rec['streaks']} streak(s), longest={rec['longest']}")
        if s3["state"] == "not_computed":
            print("    threshold: NOT SET -> NOT COMPUTED")
            print(_wrap("No streak was tested. Same reason as signal 1 for shipping no "
                        "default; set one with --flag-no-tool-streak N or "
                        "ARI_FLAG_NO_TOOL_STREAK.", indent="      "))
        else:
            print(f"    threshold: > {s3['threshold']} consecutive turns -- caller-set, "
                  f"strictly greater")
            hits = [(th, r) for th, rec in sorted(s3["per_thread"].items())
                    for r in rec["flagged"]]
            if hits:
                print(f"    FLAGGED streaks: {len(hits)}")
                for th, r in hits:
                    print(f"      {th}: {r} consecutive turns with no tool call")
            else:
                print("    FLAGGED streaks: 0 -- a MEASURED clean result against this "
                      "threshold")


def print_user_entry_survey(sources_read, sources_na, per_source, args):
    """The census signal 1's span boundaries rest on, run on whatever corpus you have.

    Exists because the exclusion rule is the one place this feature can be quietly,
    catastrophically wrong in either direction, and no one should have to take the
    docstring's word for the shape of their own transcripts. Counts only -- no entry text
    is read, printed, or inspected beyond its block types and two exact fields.
    """
    print("USER-ENTRY CENSUS -- which `user` entries count as operator input, and why not "
          "if not.\nThis is what signal 1's span boundaries are built from; the exclusion "
          "rules are listed\nunder --help-blind. Counts only: no entry's text is read.")
    print("\nsources read:")
    for label in sources_read:
        print(f"  {anonymise(label) if args.anonymise else label}")
    if sources_na:
        print("sources UNREADABLE (reported, not counted as zero):")
        for label in sources_na:
            print(f"  n/a  {anonymise(label) if args.anonymise else label}")

    tot_entries = tot_real = 0
    excluded = {}
    for _, st in per_source:
        tot_entries += st["user_entries"]
        tot_real += st["real_user_turns"]
        for k, v in st["user_excluded"].items():
            excluded[k] = excluded.get(k, 0) + v
    print(f"\nuser entries: {tot_entries}")
    print(f"  counted as REAL user turns (span boundaries): {tot_real}")
    for key, why in USER_EXCLUSIONS:
        print(f"  excluded as {key}: {excluded.get(key, 0)}")
        print(_wrap(why, indent="    "))
    other = tot_entries - tot_real - sum(excluded.values())
    # Must be 0 by construction. Printed anyway: a classifier whose own arithmetic is not
    # checked is a classifier whose buckets can silently stop summing to its input.
    print(f"  unaccounted for (must be 0): {other}")

    print("\nper source (real user turns / user entries):")
    for label, st in per_source:
        print(f"  {st['real_user_turns']:>5} / {st['user_entries']:<6} "
              f"{anonymise(label) if args.anonymise else label}")

    if tot_real == 0:
        print("\nNOT ONE REAL USER TURN in this corpus. Signal 1 is UNMEASURABLE here -- "
              "see\n--help-blind, blind mode 6. This is the case where the whole "
              "transcript would\ncollapse into one span and exceed any threshold, so "
              "nothing is flagged instead.")

# ---------------------------------------------------------------------------
# threshold resolution: CLI flag, else env var, else NOT SET
# ---------------------------------------------------------------------------
def resolve_threshold(cli_value, env_name):
    """Return (threshold_or_None, error_or_None).

    An env var that is set but unparseable is an ERROR, not a fallback to "no threshold":
    the environment you believe you set is not the environment you got until something
    says so, and silently ignoring `ARI_FLAG_SPAN_TOOL_CALLS=twenty` would print NOT
    COMPUTED to someone who thinks they asked for a flag.

    A threshold below 1 is an ERROR too, from either source. 0 flags every span including
    the empty ones; a negative flags all of them unconditionally. A detector that fires on
    everything discriminates nothing, so it is refused rather than honoured.
    """
    src, raw = ("flag", cli_value)
    if cli_value is None:
        raw = os.environ.get(env_name)
        src = "env"
        if raw is None or raw == "":
            return None, None
        try:
            raw = int(str(raw).strip())
        except ValueError:
            return None, (f"{env_name}={os.environ.get(env_name)!r} is not an integer. An "
                          f"env var that cannot be parsed is refused, not quietly ignored "
                          f"back to 'no threshold'")
    if raw < 1:
        where = (f"{env_name}={raw}" if src == "env" else f"threshold {raw}")
        return None, (f"{where} is below 1. A threshold of 0 flags EVERY span and EVERY "
                      f"streak, including the empty ones -- a user turn answered with no "
                      f"tool call is a span of 0 -- and a negative threshold flags all of "
                      f"them unconditionally. A detector that fires on everything "
                      f"discriminates nothing")
    return raw, None


# ---------------------------------------------------------------------------
# subsession manifest: a caller-supplied tree of SEPARATE transcripts
# ---------------------------------------------------------------------------
def load_manifest(path):
    """Parse the manifest JSON: a list of {label, transcript_path, parent_label}.

    Returns (nodes, error). `error` is a human string when the file is unusable, in which
    case the caller refuses -- a malformed manifest is "cannot measure", not "measured
    nothing".
    """
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
    except OSError as e:
        return None, f"cannot read manifest {path!r}: {e}"
    except Exception as e:
        return None, f"manifest {path!r} is not valid JSON: {e}"
    return validate_manifest(data)


def validate_manifest(data):
    """Validate parsed manifest JSON (split out so the self-test exercises it without
    writing a file -- this instrument stays read-only)."""
    if not isinstance(data, list):
        return None, "manifest must be a JSON list of {label, transcript_path, parent_label}"
    nodes, seen = [], set()
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "label" not in item \
                or "transcript_path" not in item:
            return None, (f"manifest entry {i} must have at least 'label' and "
                          f"'transcript_path'")
        label = str(item["label"])
        # Duplicate labels are REFUSED, not merged: the rollup keys own-totals and the
        # parent/child map by label, so two nodes sharing one would silently collapse into
        # one -- a wrong number that still looks like a measurement.
        if label in seen:
            return None, (f"duplicate manifest label {label!r}: labels must be unique or "
                          f"the rollup silently merges those nodes into one")
        seen.add(label)
        parent = item.get("parent_label")
        nodes.append(dict(label=label,
                          path=str(item["transcript_path"]),
                          parent=(str(parent) if parent is not None else None)))
    # A parent_label naming no node would silently reparent to root; refuse instead.
    labels = {nd["label"] for nd in nodes}
    for nd in nodes:
        if nd["parent"] is not None and nd["parent"] not in labels:
            return None, (f"manifest node {nd['label']!r} names parent_label "
                          f"{nd['parent']!r}, which is not itself a node")
    return nodes, None


def rollup(nodes, own_totals):
    """Sum each node's own totals bottom-up to its parent.

    own_totals[label] is a totals dict, or None when that node's transcript was
    unreadable (reported n/a, contributing nothing -- never 0). Returns a list of
    (node, own, subtree, na) in input order; cycles are broken and reported via `na`.
    """
    children = {}
    for nd in nodes:
        children.setdefault(nd["parent"], []).append(nd["label"])
    by_label = {nd["label"]: nd for nd in nodes}

    def subtree(label, seen):
        if label in seen:                       # cycle guard
            return {k: 0 for k in USAGE_FIELDS}
        seen = seen | {label}
        acc = {k: 0 for k in USAGE_FIELDS}
        own = own_totals.get(label)
        if own is not None:
            for k in USAGE_FIELDS:
                acc[k] += own[k]
        for ch in children.get(label, []):
            sub = subtree(ch, seen)
            for k in USAGE_FIELDS:
                acc[k] += sub[k]
        return acc

    rows = []
    for nd in nodes:
        own = own_totals.get(nd["label"])
        rows.append((nd, own, subtree(nd["label"], set()), own is None))
    return rows


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def fmt(acc):
    return (f"in={acc['input_tokens']} out={acc['output_tokens']} "
            f"cache_create={acc['cache_creation_input_tokens']} "
            f"cache_read={acc['cache_read_input_tokens']}")


def group_table(gdict, colhead, indent="  "):
    """Print a breakdown table (turns/out/cache_read/cache_create + a key column), ranked
    by cache_read descending. Shared by every BY-X view so they read identically."""
    print(f"{indent}{'turns':>5} {'out':>8} {'cache_read':>11} {'cache_create':>12}  "
          f"{colhead}")
    if not gdict:
        print(f"{indent}(no usage-bearing turns)")
        return
    for key, acc in sorted(gdict.items(),
                           key=lambda kv: -kv[1]["cache_read_input_tokens"]):
        print(f"{indent}{acc['turns']:>5} {acc['output_tokens']:>8} "
              f"{acc['cache_read_input_tokens']:>11} "
              f"{acc['cache_creation_input_tokens']:>12}  {key}")


def print_report(sources_read, sources_na, turns, args, flags=None):
    tot = totals_of(turns)
    cov = coverage(turns)

    print("sources read:")
    for label in sources_read:
        print(f"  {anonymise(label) if args.anonymise else label}")
    if sources_na:
        print("sources UNREADABLE (reported, not counted as zero):")
        for label in sources_na:
            print(f"  n/a  {anonymise(label) if args.anonymise else label}")

    print(f"\nattribution coverage: {cov['usage_bearing']} of {cov['total_turns']} "
          f"assistant turns were usage-bearing; "
          f"{cov['without_usage']} carried no recognised usage field")
    if cov["without_usage"]:
        print(f"  WARNING: {cov['without_usage']} of {cov['total_turns']} turns had no "
              f"usage field and are ABSENT from every total below -- not counted as 0.")
    print("usage-field presence across usage-bearing turns (a field missing here is a "
          "silent 0, not a measured one):")
    for k in USAGE_FIELDS:
        flag = "" if cov["field_present"][k] == cov["usage_bearing"] else "  <-- INCOMPLETE"
        print(f"  {k}: {cov['field_present'][k]}/{cov['usage_bearing']}{flag}")
    if cov["fields_partially_absent"]:
        print(f"  WARNING: field(s) [{', '.join(cov['fields_partially_absent'])}] are "
              f"missing from some turns; their totals below are FLOORS, not measured 0s.")
    print(f"sidechain (subagent) turns: {cov['sidechain_turns']}"
          + (f", of which {cov['sidechain_without_type']} with no recorded subagent type"
             if cov["sidechain_without_type"] else ""))
    if not cov["sidechain_turns"]:
        # A bare 0 here reads as "nothing was delegated", which it does not mean: some
        # layouts put a subagent's turns in a SEPARATE transcript file, so a caller who
        # passed only session transcripts gets 0 however much delegation happened. Same
        # rule as every other zero in this instrument -- see --help-blind, blind mode 4b.
        print("  (0 means no sidechain turns in the transcripts PASSED, not that no "
              "subagent ran: some\n   layouts write a subagent's turns to a separate "
              "transcript file. See --help-blind, mode 4b.)")

    print(f"\nTOTAL over {tot['turns']} usage-bearing turns: {fmt(tot)}")
    r = ratio(tot)
    print(f"cache_read : output overhead = "
          + (f"{r:.2f}" if r is not None else "n/a (no output tokens)"))

    print("\nBY TOOL (turn-grain: a turn's usage is attributed to the SET of tools it "
          "called, not split per call):")
    group_table(group(turns, tool_key), "tools")

    bybash = group_bash(turns)
    if bybash:
        print("\nBY BASH COMMAND (Bash turns clustered by command's first token after "
              "stripping `cd ...&&` chains and `VAR=` prefixes; a heuristic, not a parse -- "
              "`git commit`/`git push` both cluster as `git`. Keys are a CLOSED vocabulary: "
              f"a public command word, `{OTHER}`, or `{UNPARSED}`):")
        group_table(bybash, "bash command cluster")
        if OTHER in bybash:
            n = bash_other_distinct(turns)
            print(f"    `{OTHER}` holds {n} distinct command(s) that are not on the public "
                  f"allowlist. They are counted, never named: a local script, branch or "
                  f"worktree name must not reach a published key, and `--anonymise` does "
                  f"not scrub cluster keys.")

    print("\nBY AGENT (same-session Task subagents separated from the main thread):")
    group_table(group(turns, agent_key), "agent")

    print(f"\nTOP {args.top} TURNS BY cache_read (the turns re-reading the most cached "
          f"context):")
    ranked = sorted((t for t in turns if t["has_usage"]),
                    key=lambda t: -num(t["usage"].get("cache_read_input_tokens")))[:args.top]
    print(f"  {'cache_read':>11} {'out':>8}  agent / tools")
    for t in ranked:
        print(f"  {num(t['usage'].get('cache_read_input_tokens')):>11} "
              f"{num(t['usage'].get('output_tokens')):>8}  "
              f"{agent_key(t)} / {tool_key(t)}")

    if flags is not None:
        print_flags(flags)


def print_rollup(rows, args):
    print("\nBY SUBSESSION (caller's manifest tree, summed bottom-up; own = this node's "
          "transcript, subtree = own + all descendants):")
    na = sum(1 for *_, is_na in rows if is_na)
    for nd, own, sub, is_na in rows:
        label = anonymise(nd["label"]) if args.anonymise else nd["label"]
        parent = nd["parent"]
        if parent and args.anonymise:
            parent = anonymise(parent)
        ptxt = f" (parent {parent})" if parent else " (root)"
        if is_na:
            print(f"  {label}{ptxt}: own=n/a (transcript unreadable), "
                  f"subtree={fmt(sub)}")
        else:
            print(f"  {label}{ptxt}: own[{fmt(own)}]  subtree[{fmt(sub)}]")
    if na:
        print(f"  {na} node(s) reported n/a -- their transcripts were unreadable and "
              f"contribute nothing to any parent sum (not 0).")


def _anonymise_sources(obj):
    """Hash every `source` field anywhere under the flags digest, recursively.

    Written as a walk rather than as a loop over the one list it is obvious to loop over,
    because that version shipped and leaked: the span rows and the FLAGGED rows are
    SEPARATE copies of the same records after the digest's round-trip through JSON, so
    anonymising `rows` left every flagged row carrying the raw transcript path -- in the
    one output mode whose entire purpose is that the path is not there. Found by grepping
    an `--anonymise` run for the string it had just been told to hide, which is now a
    self-test leg. A walk cannot be defeated by someone adding a third list.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "source" and isinstance(v, str):
                obj[k] = anonymise(v)
            else:
                _anonymise_sources(v)
    elif isinstance(obj, list):
        for v in obj:
            _anonymise_sources(v)


def _totals_row(label, turns, args):
    """One aligned totals row (turns/out/cache_read/cache_create/ratio/label), or an
    n/a row when the source was unreadable (turns is None)."""
    lab = anonymise(label) if args.anonymise else label
    if turns is None:
        return (f"  {'n/a':>5} {'n/a':>8} {'n/a':>11} {'n/a':>12} {'n/a':>7}  "
                f"{lab} (unreadable)")
    acc = totals_of(turns)
    r = ratio(acc)
    return (f"  {acc['turns']:>5} {acc['output_tokens']:>8} "
            f"{acc['cache_read_input_tokens']:>11} {acc['cache_creation_input_tokens']:>12} "
            f"{(f'{r:.1f}' if r is not None else 'n/a'):>7}  {lab}")


def print_per_source(sources_read, sources_na, per_path_turns, args):
    """Flat per-source table for a batch of positional transcripts -- which source cost
    what, with NO hierarchy (use --manifest for parent/child rollup)."""
    print("\nPER-SOURCE (flat batch -- each positional transcript on its own; no parent/"
          "child hierarchy was declared. Use --manifest to roll a real tree up):")
    print(f"  {'turns':>5} {'out':>8} {'cache_read':>11} {'cache_create':>12} {'cr/out':>7}"
          f"  source")
    for p in sources_read:
        print(_totals_row(p, per_path_turns.get(p, []), args))
    for p in sources_na:
        print(_totals_row(p, None, args))


def print_crosstabs(nodes, per_path_turns, args):
    """Agent-level cross-tabs for manifest mode: each node is one session/agent."""
    lab = lambda x: anonymise(x) if args.anonymise else x

    print("\nBY AGENT / SUBSESSION -- each manifest node's OWN total (its transcript only, "
          "not its subtree; one node = one session/agent):")
    print(f"  {'turns':>5} {'out':>8} {'cache_read':>11} {'cache_create':>12} {'cr/out':>7}"
          f"  node")
    for nd in nodes:
        print(_totals_row(nd["label"], per_path_turns.get(nd["path"]), args))

    print("\nBY AGENT x TOOL -- each node's OWN by-tool breakdown:")
    for nd in nodes:
        t = per_path_turns.get(nd["path"])
        if t is None:
            print(f"  {lab(nd['label'])}: n/a (transcript unreadable)")
            continue
        print(f"  {lab(nd['label'])}:")
        group_table(group(t, tool_key), "tools", indent="    ")

    # Same split again by Bash command cluster, so manifest mode is not blind to a
    # dimension the no-manifest report already prints. Nodes with no Bash turn are listed
    # as `none` rather than omitted: an absent row and a node that ran no Bash command
    # must not look the same.
    print("\nBY AGENT x BASH COMMAND -- each node's OWN Bash turns, clustered by "
          "command (same heuristic as the single-source report):")
    for nd in nodes:
        t = per_path_turns.get(nd["path"])
        if t is None:
            print(f"  {lab(nd['label'])}: n/a (transcript unreadable)")
            continue
        bb = group_bash(t)
        if not bb:
            print(f"  {lab(nd['label'])}: none (no Bash turns)")
            continue
        print(f"  {lab(nd['label'])}:")
        group_table(bb, "bash command cluster", indent="    ")

    # the whole group's tool profile, WITH every subsession summed in vs the root(s) alone
    all_nodes_turns = [x for nd in nodes
                       for x in (per_path_turns.get(nd["path"]) or [])]
    root_turns = [x for nd in nodes if nd["parent"] is None
                  for x in (per_path_turns.get(nd["path"]) or [])]
    print("\nGRAND TOTAL BY TOOL -- WITH agents (every node summed in: orchestrator + all "
          "subsessions):")
    group_table(group(all_nodes_turns, tool_key), "tools")
    print("\nGRAND TOTAL BY TOOL -- root node(s) ONLY (the orchestrator's own work, no "
          "delegated subsessions) -- compare to see how much of each tool is delegated:")
    group_table(group(root_turns, tool_key), "tools")


def _glist(gdict):
    """A group dict -> a JSON list ranked by cache_read descending."""
    return [dict(key=k, **{f: acc[f] for f in USAGE_FIELDS}, turns=acc["turns"])
            for k, acc in sorted(gdict.items(),
                                 key=lambda kv: -kv[1]["cache_read_input_tokens"])]


def _tdict(acc):
    return {**{f: acc[f] for f in USAGE_FIELDS}, "turns": acc["turns"]}


def build_digest(sources_read, sources_na, turns, per_path_turns, manifest_nodes,
                 rollup_rows, args, flags=None):
    tot = totals_of(turns)
    cov = coverage(turns)
    anon = lambda x: anonymise(x) if args.anonymise else x

    ranked = sorted((t for t in turns if t["has_usage"]),
                    key=lambda t: -num(t["usage"].get("cache_read_input_tokens")))[:args.top]
    digest = dict(
        measured=bool(tot["turns"]),
        sources_read=[anon(x) for x in sources_read],
        sources_unreadable=[anon(x) for x in sources_na],
        coverage=cov,
        totals=_tdict(tot),
        cache_read_to_output=ratio(tot),
        by_tool=_glist(group(turns, tool_key)),
        by_bash_command=_glist(group_bash(turns)),
        # cardinality of `(other)` WITHOUT identity -- see bash_other_distinct
        bash_other_distinct=bash_other_distinct(turns),
        by_agent=_glist(group(turns, agent_key)),
        top_by_cache_read=[dict(cache_read=num(t["usage"].get("cache_read_input_tokens")),
                                output=num(t["usage"].get("output_tokens")),
                                agent=agent_key(t), tools=sorted(t["tools"]),
                                bash=sorted(t["bash"]))
                           for t in ranked],
    )
    if manifest_nodes is not None:
        # agent-level cross-tabs: per-node totals, per-node by-tool, and grand by-tool
        # both WITH every node summed in and root-only.
        digest["by_agent_node"] = [
            dict(label=anon(nd["label"]), parent=anon(nd["parent"]) if nd["parent"] else None,
                 own=(None if per_path_turns.get(nd["path"]) is None
                      else _tdict(totals_of(per_path_turns[nd["path"]]))))
            for nd in manifest_nodes]
        digest["by_agent_tool"] = [
            dict(label=anon(nd["label"]),
                 by_tool=(None if per_path_turns.get(nd["path"]) is None
                          else _glist(group(per_path_turns[nd["path"]], tool_key))))
            for nd in manifest_nodes]
        all_nt = [x for nd in manifest_nodes
                  for x in (per_path_turns.get(nd["path"]) or [])]
        root_nt = [x for nd in manifest_nodes if nd["parent"] is None
                   for x in (per_path_turns.get(nd["path"]) or [])]
        digest["by_agent_bash"] = [
            dict(label=anon(nd["label"]),
                 by_bash_command=(None if per_path_turns.get(nd["path"]) is None
                                  else _glist(group_bash(per_path_turns[nd["path"]]))))
            for nd in manifest_nodes]
        digest["grand_by_tool_with_agents"] = _glist(group(all_nt, tool_key))
        digest["grand_by_tool_root_only"] = _glist(group(root_nt, tool_key))
    if flags is not None:
        # The span rows name their source, so they are anonymised with the same rule the
        # source list follows -- a span row is as identifying as the path it came from.
        f = json.loads(json.dumps(flags))
        if args.anonymise:
            _anonymise_sources(f)
        digest["loop_flags"] = f
        digest["loop_flags_are_not_verdicts"] = (
            "A flagged span or streak is a pointer to look, computed from transcript "
            "structure against a caller-set threshold. It is not a finding that the work "
            "was wasted, and no threshold here is a validated constant.")
    if rollup_rows is not None:
        digest["subsessions"] = [
            dict(label=anon(nd["label"]),
                 parent=anon(nd["parent"]) if nd["parent"] else None,
                 own=(None if is_na else {f: own[f] for f in USAGE_FIELDS}),
                 subtree={f: sub[f] for f in USAGE_FIELDS})
            for nd, own, sub, is_na in rollup_rows]
    elif len(sources_read) >= 2:
        # flat batch: per-source totals (no hierarchy declared)
        digest["per_source"] = [
            dict(source=anon(p), **_tdict(totals_of(per_path_turns.get(p, []))))
            for p in sources_read]
    return digest


# ---------------------------------------------------------------------------
# self-test: positive control (exact) + negative control (must be able to fail)
# ---------------------------------------------------------------------------
def _fixture_lines():
    """A synthetic transcript with hand-computed totals. Returned as JSONL strings so the
    self-test runs the SAME parser the file path would, with no file written.

    Deliberately includes: a turn split across thinking + two tool_use entries that all
    repeat one usage (the dedup trap); a multi-tool turn; a no-tool turn; and a two-turn
    sidechain carrying a subagent type.
    """
    def asst(mid, blocks, usage, side=False, agent=None):
        d = dict(type="assistant", isSidechain=side,
                 message=dict(id=mid, role="assistant", content=blocks, usage=usage))
        if agent:
            d["attributionAgent"] = agent
        return json.dumps(d)

    def u(i, o, cc, cr):
        return {"input_tokens": i, "output_tokens": o,
                "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}

    tu = lambda name: {"type": "tool_use", "name": name, "input": {}}
    tb = lambda cmd: {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
    th = {"type": "thinking", "thinking": "..."}
    tx = {"type": "text", "text": "ok"}

    return [
        json.dumps(dict(type="user", message=dict(role="user", content="hi"))),
        # M1: ONE turn, THREE entries, same message.id + same usage -> must count once
        asst("msg-main-0001", [th], u(10, 100, 1000, 5000)),
        asst("msg-main-0001", [tu("Read")], u(10, 100, 1000, 5000)),
        asst("msg-main-0001", [tu("Read")], u(10, 100, 1000, 5000)),
        # M2: multi-tool turn; the Bash call carries a command (cd-chain stripped -> `git`)
        asst("msg-main-0002", [tx, tb("cd repo && git status"), tu("Grep")],
             u(20, 200, 2000, 8000)),
        # M3: no tool call
        asst("msg-main-0003", [tx], u(5, 50, 500, 1000)),
        # M4: launches a subagent
        asst("msg-main-0004", [tu("Task")], u(8, 80, 800, 2000)),
        json.dumps(dict(type="queue-operation", operation="noop")),
        # sidechain: two turns, subagent type recorded
        asst("msg-side-0001", [tu("Grep")], u(4, 40, 400, 3000),
             side=True, agent="explore"),
        asst("msg-side-0002", [tx], u(2, 20, 200, 1500), side=True, agent="explore"),
    ]


# hand-computed from _fixture_lines (usage counted ONCE per message.id)
_EXPECT = dict(input_tokens=49, output_tokens=490,
               cache_creation_input_tokens=4900, cache_read_input_tokens=20500, turns=6)


def _flag_fixture_lines(span_calls, streak_len, tool_results_as_user_text=False):
    """A synthetic transcript built to a SPECIFIED span size and streak length.

    One builder for every flag control, so the positive, the negative and the near-miss
    differ only in the two numbers under test -- a control that has its own bespoke
    fixture can pass for reasons that have nothing to do with the number it claims to
    test.

    Shape, with span_calls=N and streak_len=S:
      real user turn      -> opens span 1
      N assistant turns, one tool call each, EACH followed by the engine's `tool_result`
        user entry, exactly as a real transcript writes it
      an `isMeta` relaunch stub ("Continue from where you left off.")
      S assistant turns with no tool call at all
      real user turn      -> opens span 2
      1 assistant turn with one tool call
    So: 2 spans, span 1 = N calls, span 2 = 1 call, longest main-thread no-tool streak = S.

    `tool_results_as_user_text` rewrites the engine's tool_result entries as plain text
    user entries -- the counterfactual that shows the exclusion rule is load-bearing: read
    them as operator input and span 1 shatters into N spans of one call each, so the
    positive control goes clean. A control that cannot be made to fail is decoration.
    """
    out = []
    n = [0]

    def usage():
        return {"input_tokens": 1, "output_tokens": 10,
                "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}

    def asst(blocks):
        n[0] += 1
        return json.dumps(dict(type="assistant", isSidechain=False,
                               message=dict(id=f"msg-flag-{n[0]:04d}", role="assistant",
                                            content=blocks, usage=usage())))

    def real_user(text):
        return json.dumps(dict(type="user", userType="external",
                               message=dict(role="user", content=text)))

    def tool_result(i):
        if tool_results_as_user_text:
            return json.dumps(dict(type="user", userType="external",
                                   message=dict(role="user", content=f"result {i}")))
        return json.dumps(dict(type="user", userType="external",
                               message=dict(role="user",
                                            content=[dict(type="tool_result",
                                                          tool_use_id=f"call-{i}",
                                                          content="ok")])))

    def call(i):
        return dict(type="tool_use", id=f"call-{i}", name="Bash", input={})

    text = dict(type="text", text="thinking out loud")

    out.append(real_user("do the thing"))
    for i in range(span_calls):
        out.append(asst([text, call(i)]))
        out.append(tool_result(i))
    out.append(json.dumps(dict(type="user", userType="external", isMeta=True,
                               message=dict(role="user",
                                            content=[dict(type="text",
                                                          text="Continue from where you "
                                                               "left off.")]))))
    for _ in range(streak_len):
        out.append(asst([text]))
    out.append(real_user("now do the other thing"))
    out.append(asst([text, call(9000)]))
    out.append(tool_result(9000))
    return out


def _flags_of(lines, span_threshold=None, streak_threshold=None):
    """Run the real collector and the real flag computation over fixture lines."""
    turns, stats = collect_turns(lines)
    return compute_flags([("fixture", turns, stats)], span_threshold, streak_threshold)

def _corrupt(lines):
    """The negative control: rename every usage field so no turn is usage-bearing.

    A correct instrument must flag this (coverage: 0 of M usage-bearing) and refuse. An
    instrument that treats a missing field as 0 would print total=0 and pass silently --
    which is the failure this control is built to catch.
    """
    out = []
    for l in lines:
        for f in USAGE_FIELDS:
            l = l.replace(f'"{f}"', f'"{f}_RENAMED"')
        out.append(l)
    return out


def _corrupt_one(lines, field):
    """Rename ONE usage field. The turns stay usage-bearing on the other three fields, so
    the all-or-nothing check misses it -- this is the case finding (a) was defeated by."""
    return [l.replace(f'"{field}"', f'"{field}_RENAMED"') for l in lines]


# Commands whose ENV-PREFIX VALUE is a multi-word secret. The value's words are the thing
# that must never reach a cluster key or any printed line; `_SECRET_WORDS` is every word
# of every value below, and the leak control asserts none of them appears ANYWHERE in the
# text report or the `--json` digest -- with and without `--anonymise`, which by design
# hashes transcript paths and manifest labels and does NOT touch cluster keys.
_SECRET_CMDS = [
    ('AWS_SECRET_ACCESS_KEY="abcd efgh ijkl" aws s3 ls', "aws"),
    ('TOKEN="swordfish hunter2" curl https://example.invalid', "curl"),
    ("API_KEY='alpha beta' python3 run.py", "python3"),
    # `deploy.sh` is a LOCAL script name. Before ARI#11 this line expected the key to be
    # the literal `deploy.sh` -- i.e. this control asserted the very publication that issue
    # is about, because its subject was the secret VALUE and the command NAME was treated as
    # harmless by assumption. It now expects `(other)`.
    ('SECRET="zulu quebec" ./deploy.sh', OTHER),
    ('PASSWORD="xyzzy plugh" mysql -u root', "mysql"),
    # UNBALANCED quote -- the shape that reaches the `toks is None` fallback, where no
    # peeling happens and the key comes from the ORIGINAL string's first whitespace word.
    # That word is `VAULT_KEY="grommet` here, which is a secret fragment, and the ONLY
    # thing that stops it being published is the `_CMD_WORD` test at the end of
    # `bash_cluster`. Every other entry above reduces to a real command word and so
    # exercises the peel but never that test; deleting the test left the self-test green.
    # Expected `(unparsed)`: on this path refusing to classify IS the privacy control.
    ('VAULT_KEY="grommet flange aws s3 ls', UNPARSED),
]
# Deliberately distinctive: a sentinel that is also ordinary English ("one", "two") would
# match the report's OWN prose and fail for a reason that has nothing to do with a leak.
# The baseline check below enforces that choice instead of trusting it.
_SECRET_WORDS = ["abcd", "efgh", "ijkl", "swordfish", "hunter2", "alpha", "beta",
                 "zulu", "quebec", "xyzzy", "plugh", "grommet", "flange"]


def _bash_asst(mid, cmd, usage):
    blk = ({"type": "tool_use", "name": "Bash", "input": {}} if cmd is None
           else {"type": "tool_use", "name": "Bash", "input": {"command": cmd}})
    return json.dumps(dict(type="assistant", isSidechain=False,
                           message=dict(id=mid, role="assistant", content=[blk],
                                        usage=usage)))


def _u(i, o, cc, cr):
    return {"input_tokens": i, "output_tokens": o,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}


def _bash_fixture_lines():
    """Known Bash commands for the clustering positive control: 2x git (one behind a
    `cd ...&&` chain), 1x pytest (behind a `VAR=` prefix), one with no command -- plus the
    two shapes whose ABSENCE here is how a privacy leak and a misclustering shipped:

      * a QUOTED, multi-word env value (`AWS_SECRET_ACCESS_KEY="abcd efgh ijkl" aws ...`).
        A peel step that is blind to quoting stops at the first space INSIDE the value and
        hands `efgh ijkl" aws s3 ls` to the classifier, publishing `efgh` -- a literal word
        of the secret -- as the cluster key. Must cluster as `aws`.
      * an env prefix BEFORE a `cd ...&&` chain (`CI=1 cd /repo && npm test`), the ordinary
        ordering. Peeling the two shapes in two separate fixed passes can only strip them
        in one order, so the `cd` chain survives and the turn clusters as `cd`. Must
        cluster as `npm`.

    Both are ordinary commands, not exotic ones; the old fixture simply never wrote one
    down, which is precisely why nothing failed when the heuristic was wrong about them.
    """
    return [
        _bash_asst("msg-b-1", "git status", _u(1, 10, 100, 1000)),
        _bash_asst("msg-b-2", "cd /repo && git push origin main", _u(1, 20, 200, 2000)),
        _bash_asst("msg-b-3", "VAR=1 pytest -q tests/", _u(1, 30, 300, 3000)),
        _bash_asst("msg-b-4", None, _u(1, 40, 400, 4000)),       # no command -> (unparsed)
        _bash_asst("msg-b-5", _SECRET_CMDS[0][0], _u(1, 50, 500, 5000)),   # quoted value
        _bash_asst("msg-b-6", "CI=1 cd /repo && npm test", _u(1, 60, 600, 6000)),  # env+cd
    ]


# Commands whose first WORD is a plausible internal name -- a worktree/branch name or a
# local script. Deliberately NOT secrets: the point of ARI#11 is that the previous round's
# controls were all about a secret VALUE, so a well-formed local NAME sailed through every
# one of them and was published verbatim as a cluster key. The shapes observed on real data
# were release/roast worktree names carrying a ticket id and a slug, and numbered local
# `poll_ci*.sh`-style scripts (ARI#11 has the actual strings; this repo does not repeat them).
# These fixtures are those shapes with an obviously-fake `hc9999` marker, so nobody mistakes
# a fixture for a real branch. Entry 3 is BOTH secret-shaped and internal-name-shaped, because
# the two guards must compose rather than each assume the other ran.
_INTERNAL_NAME_CMDS = [
    "./roast-hc9999-fake-example 2>&1",
    "./poll_ci9_hc9999fake.sh --once",
    'SECRET="zulu quebec" ./release-hc9999-fake-wire deploy',
    "cd /wt/hc9999-fake-worktree && ./hc9999-fake-runner --all",
]
# The literal strings that must never appear in any output, plus the marker they share.
# `hc9999` is the cheap one to grep for and cannot collide with the report's own prose.
_INTERNAL_NAME_WORDS = ["hc9999", "roast-hc9999-fake-example", "poll_ci9_hc9999fake.sh",
                        "release-hc9999-fake-wire", "hc9999-fake-runner",
                        "hc9999-fake-worktree"]
# Public commands that MUST keep their own key through the same fixture. Without these the
# leg would pass just as well against a "bucket everything" fix, which would close the leak
# by destroying the feature.
_PUBLIC_CONTROL_CMDS = [("git status", "git"), ("pytest -q tests/", "pytest"),
                        ("cd /repo && npm test", "npm")]


def _internal_name_fixture_lines():
    """One Bash turn per internal-name command, then one per public control."""
    out = [_bash_asst(f"msg-i-{n}", cmd, _u(1, 10, 100, 1000))
           for n, cmd in enumerate(_INTERNAL_NAME_CMDS)]
    out += [_bash_asst(f"msg-p-{n}", cmd, _u(1, 10, 100, 1000))
            for n, (cmd, _) in enumerate(_PUBLIC_CONTROL_CMDS)]
    return out


def _secret_fixture_lines():
    """One Bash turn per `_SECRET_CMDS` entry -- the leak control's own transcript.

    Opens with a REAL user turn so the transcript is loop-flag-shaped as well as
    secret-bearing: the span/streak rows are then computed over, and printed alongside,
    the very turns that carry the secrets. Without that turn signal 1 reads UNMEASURED and
    the leak control would be asserting over a loop_flags block with no rows in it -- that
    is, over none of the printed surface the loop flags added to this file.
    """
    user = json.dumps(dict(type="user", message=dict(role="user", content="go")))
    return [user] + [_bash_asst(f"msg-s-{n}", cmd, _u(1, 10, 100, 1000))
                     for n, (cmd, _) in enumerate(_SECRET_CMDS)]


def self_test():
    print("SELF-TEST [positive control: exact totals over a synthetic fixture]")
    turns, _ = collect_turns(_fixture_lines())
    tot = totals_of(turns)
    ok = True

    # the dedup is the load-bearing claim: M1's 3 entries must be one turn
    for f in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens", "turns"):
        got, exp = tot.get(f, tot.get("turns")), _EXPECT[f]
        got = tot[f] if f in tot else tot["turns"]
        mark = "OK" if got == exp else "MISMATCH"
        if got != exp:
            ok = False
        print(f"  {f:<32} got={got:<7} expected={exp:<7} {mark}")

    cov = coverage(turns)
    side = group(turns, agent_key)
    # main vs sidechain must be split, not merged
    main_out = side.get(MAIN, {}).get("output_tokens")
    exp_main = 430
    if main_out != exp_main:
        ok = False
    print(f"  {'main-thread output':<32} got={main_out} expected={exp_main} "
          f"{'OK' if main_out == exp_main else 'MISMATCH'}")
    sub_out = side.get("explore", {}).get("output_tokens")
    if sub_out != 60:
        ok = False
    print(f"  {'subagent explore output':<32} got={sub_out} expected=60 "
          f"{'OK' if sub_out == 60 else 'MISMATCH'}")
    bytool = group(turns, tool_key)
    # the no-tool bucket must merge M3 and the sidechain's text turn (2 turns)
    nt = bytool.get(NO_TOOL, {}).get("turns")
    if nt != 2:
        ok = False
    print(f"  {'(no tool call) turns':<32} got={nt} expected=2 "
          f"{'OK' if nt == 2 else 'MISMATCH'}")
    full_ok = (cov["fully_attributed"] == 6
               and all(cov["field_present"][k] == 6 for k in USAGE_FIELDS))
    if not full_ok:
        ok = False
    print(f"  coverage: {cov['usage_bearing']} of {cov['total_turns']} usage-bearing, "
          f"fully-attributed={cov['fully_attributed']} "
          f"{'OK' if full_ok else 'MISMATCH'}")

    print("\nSELF-TEST [negative control A: rename ALL usage fields -> no turn measurable]")
    ca = coverage(collect_turns(_corrupt(_fixture_lines()))[0])
    a_ok = ca["usage_bearing"] == 0 and refusal_reason(ca) is not None
    print(f"  usage-bearing now {ca['usage_bearing']} of {ca['total_turns']}; "
          f"refusal={'FIRED' if a_ok else 'DID NOT FIRE'}")
    if not a_ok:
        ok = False

    print("\nSELF-TEST [negative control B: rename ONE field (output_tokens) -- the case "
          "the all-or-nothing check passed silently]")
    cb = coverage(collect_turns(_corrupt_one(_fixture_lines(), "output_tokens"))[0])
    # still usage-bearing on the other three fields, but output_tokens absent everywhere,
    # and the run must refuse rather than report output=0
    b_ok = (cb["usage_bearing"] == 6 and cb["field_present"]["output_tokens"] == 0
            and "output_tokens" in cb["fields_entirely_absent"]
            and refusal_reason(cb) is not None)
    print(f"  usage-bearing still {cb['usage_bearing']}, but output_tokens present on "
          f"{cb['field_present']['output_tokens']}/{cb['usage_bearing']}; "
          f"refusal={'FIRED' if b_ok else 'DID NOT FIRE'}")
    if not b_ok:
        ok = False

    print("\nSELF-TEST [Bash clustering positive control: known commands must cluster "
          "exactly]")
    bt, _ = collect_turns(_bash_fixture_lines())
    bg = group_bash(bt)
    got = {k: v["turns"] for k, v in bg.items()}
    exp = {"git": 2, "pytest": 1, UNPARSED: 1, "aws": 1, "npm": 1}
    bash_ok = got == exp
    if not bash_ok:
        ok = False
    print(f"  clusters: got={got} expected={exp} {'OK' if bash_ok else 'MISMATCH'}")
    # Named separately from the dict compare above, because these two are the shapes the
    # fixture used to omit, and a reader should see them pass or fail BY NAME.
    quoted_ok = bash_cluster(_SECRET_CMDS[0][0]) == "aws"
    envcd_ok = bash_cluster("CI=1 cd /repo && npm test") == "npm"
    unbal_ok = bash_cluster('VAULT_KEY="grommet flange aws s3 ls') == UNPARSED
    if not (quoted_ok and envcd_ok and unbal_ok):
        ok = False
    print(f"  quoted multi-word env value -> real command (not a word of the value): "
          f"{'OK' if quoted_ok else 'MISMATCH'}")
    print(f"  env prefix BEFORE a `cd ...&&` chain -> real command (not `cd`): "
          f"{'OK' if envcd_ok else 'MISMATCH'}")
    print(f"  UNBALANCED quote -> (unparsed), never the raw first word of the original "
          f"(which holds a secret fragment): {'OK' if unbal_ok else 'MISMATCH'}")

    print("\nSELF-TEST [PRIVACY control: no fragment of a quoted secret value may reach "
          "any printed output -- cluster keys are NOT anonymised, so they must be safe "
          "by derivation]")
    leak_ok = True
    for cmd, want in _SECRET_CMDS:
        key = bash_cluster(cmd)
        if key != want:
            leak_ok = False
            print(f"  MISCLUSTERED {cmd!r} -> {key!r} (expected {want!r})")
    st, st_stats = collect_turns(_secret_fixture_lines())
    # same report shape, no secrets -- and a real user turn, so its flags populate too
    base, base_stats = collect_turns(
        [json.dumps(dict(type="user", message=dict(role="user", content="go")))]
        + _bash_fixture_lines()[:4])

    def _render(trns, stats, anon_flag):
        """Render EVERY printed surface this instrument has, so the assertion below covers
        all of it: text report + `--json` digest, each with the LOOP FLAGS section
        populated. The flags are included because they are a printed surface too -- they
        carry no cluster key and no command string, and this control is what keeps that
        true rather than something a reader has to re-derive from the flag code."""
        a = argparse.Namespace(top=5, anonymise=anon_flag, json=False)
        src = ["/tmp/secret-fixture.jsonl"]
        flg = compute_flags([(src[0], trns, stats)], 1, 1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_report(src, [], trns, a, flg)
        return (buf.getvalue() + json.dumps(
            build_digest(src, [], trns, {src[0]: trns}, None, None, a, flg))).lower()

    def _hits(text):
        return [w for w in _SECRET_WORDS
                if re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", text)]

    # The control must be able to tell a leak from its own prose: a sentinel that shows up
    # in a render containing NO secret at all would make this check meaningless.
    collisions = _hits(_render(base, base_stats, False))
    if collisions:
        leak_ok = False
        print(f"  CONTROL INVALID: sentinel word(s) {collisions} appear in a render with "
              f"no secret input -- choose distinctive sentinels")
    for anon_flag in (False, True):
        rendered = _render(st, st_stats, anon_flag)
        # (a) no whole command, and (b) no WORD of any secret value, anywhere
        for cmd, _want in _SECRET_CMDS:
            if cmd.lower() in rendered:
                leak_ok = False
                print(f"  FULL COMMAND LEAKED (anonymise={anon_flag}): {cmd!r}")
        for w in _hits(rendered):
            leak_ok = False
            print(f"  SECRET-VALUE WORD LEAKED (anonymise={anon_flag}): {w!r}")
    if not leak_ok:
        ok = False
    print(f"  {len(_SECRET_CMDS)} commands with quoted multi-word secret values, text "
          f"report + --json digest + loop-flags section, with and without --anonymise: "
          f"{'OK -- no value fragment printed' if leak_ok else 'LEAKED'}")

    print("\nSELF-TEST [PUBLISHED-VOCABULARY control (ARI#11): a well-formed LOCAL name -- "
          "branch, worktree, script -- must never reach a cluster key, and the public case "
          "must still keep its own key]")
    # This is a DIFFERENT gap from the secret-value control above, which already passed
    # while the leak was live: that one asks "did a fragment of a quoted VALUE escape",
    # and a local script NAME is not a value. The guard here is the allowlist, so this leg
    # has to show three things at once -- the names are bucketed, the names are absent from
    # every rendered surface, and the public commands are NOT collateral damage.
    vocab_ok = True
    for cmd in _INTERNAL_NAME_CMDS:
        key = bash_cluster(cmd)
        if key != OTHER:
            vocab_ok = False
            print(f"  NOT BUCKETED {cmd!r} -> {key!r} (expected {OTHER!r})")
    for cmd, want in _PUBLIC_CONTROL_CMDS:
        key = bash_cluster(cmd)
        if key != want:
            vocab_ok = False
            print(f"  PUBLIC COMMAND LOST {cmd!r} -> {key!r} (expected {want!r}) -- the fix "
                  f"must not buy safety by bucketing everything")
    it, it_stats = collect_turns(_internal_name_fixture_lines())
    # baseline first: these sentinels must not occur in a render that had no internal name
    # in it, or the leg could pass by matching the instrument's own prose.
    nbase, nbase_stats = collect_turns(
        [json.dumps(dict(type="user", message=dict(role="user", content="go")))]
        + [_bash_asst(f"msg-pc-{n}", c, _u(1, 10, 100, 1000))
           for n, (c, _) in enumerate(_PUBLIC_CONTROL_CMDS)])

    def _nhits(text):
        return [w for w in _INTERNAL_NAME_WORDS if w.lower() in text]

    ncollisions = _nhits(_render(nbase, nbase_stats, False))
    if ncollisions:
        vocab_ok = False
        print(f"  CONTROL INVALID: sentinel(s) {ncollisions} appear in a render containing "
              f"no internal name -- choose more distinctive fixtures")
    for anon_flag in (False, True):
        rendered = _render(it, it_stats, anon_flag)
        for w in _nhits(rendered):
            vocab_ok = False
            print(f"  LOCAL NAME LEAKED (anonymise={anon_flag}): {w!r}")
        # the invariant, not just the sample: EVERY key on every published surface has to be
        # a member of the closed vocabulary. A future gate that admits a fourth kind of key
        # fails here even if none of the fixtures above happens to exercise it.
        for key in group_bash(it):
            if not (key in _PUBLIC_COMMANDS or key in (OTHER, UNPARSED)):
                vocab_ok = False
                print(f"  KEY OUTSIDE CLOSED VOCABULARY (anonymise={anon_flag}): {key!r}")
    # `(other)` must stay countable, or the bucket silently merges distinct unknowns
    n_distinct = bash_other_distinct(it)
    if n_distinct != len(_INTERNAL_NAME_CMDS):
        vocab_ok = False
        print(f"  DISTINCT COUNT WRONG: got {n_distinct}, expected "
              f"{len(_INTERNAL_NAME_CMDS)} -- `{OTHER}` must not merge distinct commands")
    # the sentinels are not commands and must never be allowlisted (they would then be
    # publishable as if they were real command words)
    if UNPARSED in _PUBLIC_COMMANDS or OTHER in _PUBLIC_COMMANDS:
        vocab_ok = False
        print(f"  SENTINEL IN ALLOWLIST: {UNPARSED!r}/{OTHER!r} must not be public commands")
    if not vocab_ok:
        ok = False
    print(f"  {len(_INTERNAL_NAME_CMDS)} internal-name commands bucketed to `{OTHER}` and "
          f"absent from both output modes; {len(_PUBLIC_CONTROL_CMDS)} public commands kept "
          f"their own key; {n_distinct} distinct counted; every key inside the closed "
          f"vocabulary: {'OK' if vocab_ok else 'FAILED'}")

    print("\nSELF-TEST [manifest guards: duplicate label and dangling parent must REFUSE]")
    dup_ok = validate_manifest(
        [{"label": "x", "transcript_path": "a"},
         {"label": "x", "transcript_path": "b"}])[1] is not None
    unknown_ok = validate_manifest(
        [{"label": "x", "transcript_path": "a", "parent_label": "ghost"}])[1] is not None
    print(f"  duplicate-label refused={dup_ok}; dangling-parent refused={unknown_ok}")
    if not (dup_ok and unknown_ok):
        ok = False

    # -----------------------------------------------------------------------
    # LOOP FLAGS: positive, negative, near-miss, and the exclusion counterfactual
    # -----------------------------------------------------------------------
    SPAN_T, STREAK_T = 5, 3
    print(f"\nSELF-TEST [loop flags, POSITIVE control: a fixture built to exceed both "
          f"thresholds (span > {SPAN_T} calls, streak > {STREAK_T} turns)]")
    pos = _flags_of(_flag_fixture_lines(7, 4), SPAN_T, STREAK_T)
    p1, p3 = pos["signal1_span_tool_calls"], pos["signal3_no_tool_streaks"]
    # hand-computed from the builder: 2 real user turns, 2 spans of 7 and 1 calls, one
    # main-thread streak of 4, and 8 tool_result + 1 isMeta entries excluded
    checks = [
        ("real user turns", p1["real_user_turns"], 2),
        ("spans", p1["spans"], 2),
        ("span 1 calls", p1["rows"][0]["calls"], 7),
        ("span 2 calls", p1["rows"][1]["calls"], 1),
        ("calls p50 / p90", (p1["calls_p50"], p1["calls_p90"]), (1, 7)),
        ("excluded tool_result", p1["user_entries_excluded"].get("tool_result"), 8),
        ("excluded isMeta", p1["user_entries_excluded"].get("isMeta"), 1),
        ("signal 1 state", p1["state"], "measured"),
        ("spans FLAGGED", [r["calls"] for r in p1["flagged"]], [7]),
        ("signal 3 longest streak", p3["longest"], 4),
        ("streaks FLAGGED (main)", p3["per_thread"][MAIN]["flagged"], [4]),
        ("signal 2 state", pos["signal2_repeated_invocations"]["state"],
         "not_implemented"),
    ]
    for label, got, exp in checks:
        if got != exp:
            ok = False
        print(f"  {label:<32} got={str(got):<18} expected={str(exp):<18} "
              f"{'OK' if got == exp else 'MISMATCH'}")

    print(f"\nSELF-TEST [loop flags, NEGATIVE control: a fixture under BOTH thresholds "
          f"must report a MEASURED clean result, not 'not computed']")
    neg = _flags_of(_flag_fixture_lines(3, 1), SPAN_T, STREAK_T)
    n1, n3 = neg["signal1_span_tool_calls"], neg["signal3_no_tool_streaks"]
    neg_ok = (n1["state"] == "measured" and n1["flagged"] == []
              and n1["calls_max"] == 3 and n3["state"] == "measured"
              and n3["longest"] == 1 and n3["per_thread"][MAIN]["flagged"] == [])
    print(f"  max span={n1['calls_max']} (threshold {SPAN_T}), longest streak="
          f"{n3['longest']} (threshold {STREAK_T}); flagged spans="
          f"{len(n1['flagged'])} streaks={len(n3['per_thread'][MAIN]['flagged'])}; "
          f"both states measured={n1['state'] == n3['state'] == 'measured'} "
          f"{'OK' if neg_ok else 'MISMATCH'}")
    if not neg_ok:
        ok = False

    print("\nSELF-TEST [loop flags, NEAR-MISS boundary: EXACTLY the threshold must not "
          "flag, and exactly one more must -- both signals, both directions]")
    for name, exact, over in (("span calls", _flag_fixture_lines(SPAN_T, 0),
                               _flag_fixture_lines(SPAN_T + 1, 0)),
                              ("no-tool streak", _flag_fixture_lines(1, STREAK_T),
                               _flag_fixture_lines(1, STREAK_T + 1))):
        fe = _flags_of(exact, SPAN_T, STREAK_T)
        fo = _flags_of(over, SPAN_T, STREAK_T)
        if name == "span calls":
            at, above = len(fe["signal1_span_tool_calls"]["flagged"]), \
                len(fo["signal1_span_tool_calls"]["flagged"])
            n_at = fe["signal1_span_tool_calls"]["calls_max"]
            n_over = fo["signal1_span_tool_calls"]["calls_max"]
        else:
            at = len(fe["signal3_no_tool_streaks"]["per_thread"][MAIN]["flagged"])
            above = len(fo["signal3_no_tool_streaks"]["per_thread"][MAIN]["flagged"])
            n_at = fe["signal3_no_tool_streaks"]["longest"]
            n_over = fo["signal3_no_tool_streaks"]["longest"]
        leg_ok = (at == 0 and above == 1)
        if not leg_ok:
            ok = False
        print(f"  {name:<16} at={n_at} -> flagged {at} (must be 0);  "
              f"at={n_over} -> flagged {above} (must be 1)  "
              f"{'OK' if leg_ok else 'MISMATCH'}")

    print("\nSELF-TEST [loop flags, the exclusion rule is LOAD-BEARING: read the engine's "
          "own tool_result entries as operator input and the positive control goes clean]")
    cf = _flags_of(_flag_fixture_lines(7, 4, tool_results_as_user_text=True),
                   SPAN_T, STREAK_T)["signal1_span_tool_calls"]
    # Not a style point: with tool results counted as user turns, span 1 shatters into one
    # span per call, the max drops to 1, and the fixture built to exceed the threshold is
    # reported as clean. This is the control demonstrating it can fail.
    cf_ok = (cf["spans"] == 10 and cf["calls_max"] == 1 and cf["flagged"] == []
             and p1["spans"] == 2 and p1["calls_max"] == 7)
    print(f"  with the exclusion: spans={p1['spans']} max={p1['calls_max']} "
          f"flagged={len(p1['flagged'])}  |  without it: spans={cf['spans']} "
          f"max={cf['calls_max']} flagged={len(cf['flagged'])}  "
          f"{'OK -- the control can fail' if cf_ok else 'MISMATCH'}")
    if not cf_ok:
        ok = False

    print("\nSELF-TEST [loop flags: a transcript with NO real user turn must read "
          "UNMEASURED, and must flag NOTHING]")
    # every real user turn removed: only the engine's tool_result entries and the isMeta
    # relaunch stub remain, which is the sidechain-only / mid-session-fragment case
    headless = [l for l in _flag_fixture_lines(7, 4)
                if not (json.loads(l).get("type") == "user"
                        and user_exclusion_reason(json.loads(l)) is None)]
    hl = _flags_of(headless, SPAN_T, STREAK_T)["signal1_span_tool_calls"]
    hl_ok = (hl["real_user_turns"] == 0 and hl["state"] == "unmeasured"
             and hl["flagged"] == [])
    print(f"  real user turns={hl['real_user_turns']}, state={hl['state']}, "
          f"flagged={len(hl['flagged'])} (the single implicit span holds "
          f"{hl['rows'][0]['calls']} calls, which would have exceeded the threshold)  "
          f"{'OK' if hl_ok else 'MISMATCH'}")
    if not hl_ok:
        ok = False

    print("\nSELF-TEST [loop flags: a turn that calls ONE tool SEVERAL times is that many "
          "calls, and a split turn's repeated block ids are counted once]")
    # Found by mutating turn_calls to `len(t["tools"])`: the span fixture's turns each
    # call one tool once, so name-counting and call-counting agreed everywhere and the
    # whole point of counting calls went untested. These two turns disagree under that
    # mutation by construction.
    def _tu(i):
        return dict(type="tool_use", id=f"b-{i}", name="Bash", input={})
    multi = [json.dumps(dict(type="user", message=dict(role="user", content="go"))),
             # one turn, one tool name, THREE calls
             json.dumps(dict(type="assistant", message=dict(
                 id="msg-multi-1", role="assistant",
                 content=[_tu(1), _tu(2), _tu(3)],
                 usage=dict(input_tokens=1, output_tokens=1,
                            cache_creation_input_tokens=1,
                            cache_read_input_tokens=1)))),
             # ONE turn written as two entries that REPEAT the same block id: the split-
             # turn trap applied to call counting rather than to usage
             json.dumps(dict(type="assistant", message=dict(
                 id="msg-multi-2", role="assistant", content=[_tu(9)],
                 usage=dict(input_tokens=1, output_tokens=1,
                            cache_creation_input_tokens=1,
                            cache_read_input_tokens=1)))),
             json.dumps(dict(type="assistant", message=dict(
                 id="msg-multi-2", role="assistant", content=[_tu(9)],
                 usage=dict(input_tokens=1, output_tokens=1,
                            cache_creation_input_tokens=1,
                            cache_read_input_tokens=1))))]
    mt, _ = collect_turns(multi)
    mt_ok = (len(mt) == 2 and turn_calls(mt[0]) == 3 and mt[0]["tools"] == {"Bash"}
             and turn_calls(mt[1]) == 1)
    print(f"  turn 1: tool names={sorted(mt[0]['tools'])} calls={turn_calls(mt[0])} "
          f"(expected 3 -- name-counting would say 1); turn 2: one block id across two "
          f"entries -> calls={turn_calls(mt[1])} (expected 1)  "
          f"{'OK' if mt_ok else 'MISMATCH'}")
    if not mt_ok:
        ok = False

    print("\nSELF-TEST [loop flags: a no-tool streak must NOT run across the main thread "
          "and a sidechain, which are interleaved in one file]")
    # Found by mutating no_tool_streaks to scan raw transcript order: the span fixture has
    # no sidechain at all, so the split was unexercised. Here four no-tool turns alternate
    # main/sidechain/sidechain/main -- a raw-order scan reports ONE streak of 4, which no
    # thread ever had.
    def _txt(mid, side):
        return json.dumps(dict(type="assistant", isSidechain=side, message=dict(
            id=mid, role="assistant", content=[dict(type="text", text="talking")],
            usage=dict(input_tokens=1, output_tokens=1,
                       cache_creation_input_tokens=1, cache_read_input_tokens=1))))
    woven = [json.dumps(dict(type="user", message=dict(role="user", content="go"))),
             _txt("w1", False), _txt("w2", True), _txt("w3", True), _txt("w4", False)]
    wf = _flags_of(woven, None, 3)["signal3_no_tool_streaks"]
    w_ok = (wf["longest"] == 2
            and wf["per_thread"][MAIN]["longest"] == 2
            and wf["per_thread"]["sidechains"]["longest"] == 2
            and wf["per_thread"][MAIN]["flagged"] == []
            and wf["per_thread"]["sidechains"]["flagged"] == [])
    print(f"  main={wf['per_thread'][MAIN]['longest']} "
          f"sidechains={wf['per_thread']['sidechains']['longest']} "
          f"longest anywhere={wf['longest']} (expected 2/2/2; a raw-order scan would say "
          f"4 and flag it against a threshold of 3)  {'OK' if w_ok else 'MISMATCH'}")
    if not w_ok:
        ok = False

    print("\nSELF-TEST [loop flags: a user turn answered with NO tool call is a span of 0 "
          "and must stay in the distribution]")
    # Found by mutating spans_of to drop empty spans: nothing asserted their presence
    # directly. They are also the whole reason a threshold of 0 is refused, so dropping
    # them would quietly lift every quantile AND weaken that refusal's stated rationale.
    empty = _flags_of(_flag_fixture_lines(0, 0), SPAN_T, STREAK_T)[
        "signal1_span_tool_calls"]
    e_ok = (empty["spans"] == 2 and [r["calls"] for r in empty["rows"]] == [0, 1]
            and empty["calls_p50"] == 0 and empty["flagged"] == [])
    print(f"  spans={empty['spans']} calls per span="
          f"{[r['calls'] for r in empty['rows']]} p50={empty['calls_p50']} "
          f"flagged={len(empty['flagged'])} (expected 2 spans, [0, 1], p50=0, none "
          f"flagged)  {'OK' if e_ok else 'MISMATCH'}")
    if not e_ok:
        ok = False

    print("\nSELF-TEST [loop flags: the user-entry classifier, entry by entry, including "
          "the engine-injected case no fixture above contains]")
    # Found by mutating the engine_injected rule away with no leg going red: the span
    # fixtures carry isMeta and tool_result entries but no promptSource one. Each row is a
    # shape measured in real transcripts on this box.
    cls_cases = [
        ("typed prompt, string content",
         dict(type="user", userType="external", promptSource="typed",
              message=dict(role="user", content="do the thing")), None),
        ("SDK prompt, list of text blocks",
         dict(type="user", userType="external", promptSource="sdk",
              message=dict(role="user", content=[dict(type="text", text="do it")])),
         None),
        ("engine's tool output coming back",
         dict(type="user", userType="external",
              message=dict(role="user",
                           content=[dict(type="tool_result", content="ok")])),
         "tool_result"),
        ("relaunch stub",
         dict(type="user", isMeta=True,
              message=dict(role="user", content=[dict(type="text",
                                                      text="Continue from where you "
                                                           "left off.")])), "isMeta"),
        ("engine-injected task notification",
         dict(type="user", userType="external", promptSource="system",
              message=dict(role="user", content="<task-notification>...")),
         "engine_injected"),
        ("isMeta AND tool_result -> counted once, under isMeta",
         dict(type="user", isMeta=True,
              message=dict(role="user",
                           content=[dict(type="tool_result", content="ok")])), "isMeta"),
        ("unclassifiable (empty content) -> treated as REAL, which SPLITS a span and so "
         "lowers every count rather than inventing work",
         dict(type="user", message=dict(role="user", content=[])), None),
    ]
    for label, entry, exp in cls_cases:
        got = user_exclusion_reason(entry)
        if got != exp:
            ok = False
        print(f"  {'real user turn' if got is None else got:<16} "
              f"expected={'real user turn' if exp is None else exp:<16} "
              f"{'OK' if got == exp else 'MISMATCH'}  {label}")

    print("\nSELF-TEST [loop flags: spans are computed PER SOURCE -- pooling two "
          "transcripts' turns first would report a span neither session had]")
    # Span indices restart at every transcript, so a merged turn list glues source A's
    # span 1 onto source B's. Demonstrated both ways below rather than asserted.
    ta, sa = collect_turns(_flag_fixture_lines(7, 0))
    tb, sb = collect_turns(_flag_fixture_lines(7, 0))
    per = compute_flags([("A", ta, sa), ("B", tb, sb)], SPAN_T, STREAK_T)[
        "signal1_span_tool_calls"]
    pooled = spans_of(ta + tb, sa["real_user_turns"] + sb["real_user_turns"])
    two_ok = ([r["calls"] for r in per["rows"]] == [7, 1, 7, 1]
              and per["spans"] == 4 and per["calls_max"] == 7
              and [r["calls"] for r in pooled] == [14, 2, 0, 0])
    print(f"  per source: spans={per['spans']} calls="
          f"{[r['calls'] for r in per['rows']]} max={per['calls_max']}  |  pooled "
          f"first: calls={[r['calls'] for r in pooled]} -- a 14-call span that never "
          f"happened  {'OK' if two_ok else 'MISMATCH'}")
    if not two_ok:
        ok = False

    print("\nSELF-TEST [loop flags: --anonymise must leave NO raw source path anywhere "
          "in the digest -- the mode whose only job is that the path is not there]")
    # This shipped broken: `flagged` holds separate copies of the span rows after the
    # digest's JSON round-trip, so anonymising `rows` alone left every flagged row's raw
    # path in the output. Asserted over the WHOLE serialised digest, not per field, so a
    # future list cannot reintroduce it.
    anon_args = argparse.Namespace(anonymise=True, top=2)
    anon_turns, anon_stats = collect_turns(_flag_fixture_lines(7, 4))
    leaky = "transcript-path-that-must-not-survive.jsonl"
    # TWO sources, not one, so the flat-batch `per_source` rows are in the digest this
    # asserts over. That key arrived with the cross-tabs/batch work and carries a `source`
    # field of its own, so a one-source leg would have left the merged digest's only NEW
    # path-bearing list unchecked -- exactly the "a future list reintroduces it" hole the
    # walk above was written against.
    leaky2 = "second-transcript-path-that-must-not-survive.jsonl"
    anon_turns2, anon_stats2 = collect_turns(_flag_fixture_lines(7, 4))
    anon_digest = json.dumps(build_digest(
        [leaky, leaky2], [], anon_turns + anon_turns2,
        {leaky: anon_turns, leaky2: anon_turns2}, None, None, anon_args,
        compute_flags([(leaky, anon_turns, anon_stats),
                       (leaky2, anon_turns2, anon_stats2)], SPAN_T, STREAK_T)))
    a_ok = (leaky not in anon_digest and leaky2 not in anon_digest
            and anonymise(leaky) in anon_digest and anonymise(leaky2) in anon_digest)
    print(f"  raw path present={leaky in anon_digest or leaky2 in anon_digest} (must be "
          f"False), hashed form present={anonymise(leaky) in anon_digest and anonymise(leaky2) in anon_digest}"
          f" (must be True)  {'OK' if a_ok else 'MISMATCH'}")
    if not a_ok:
        ok = False

    print("\nSELF-TEST [loop flags: an unusable threshold must REFUSE -- 0, negative, and "
          "an env var that is set but unparseable]")
    refusals = [("--flag 0", resolve_threshold(0, "ARI_UNSET_FOR_SELFTEST")),
                ("--flag -1", resolve_threshold(-1, "ARI_UNSET_FOR_SELFTEST")),
                ("flag 1 (valid)", resolve_threshold(1, "ARI_UNSET_FOR_SELFTEST")),
                ("nothing set", resolve_threshold(None, "ARI_UNSET_FOR_SELFTEST"))]
    # the env legs set and restore ONE variable of their own in this process; no file is
    # written, and the name is this test's own, never one a caller might be relying on
    for raw in ("twenty", "0", "-4", "7"):
        os.environ["ARI_SELFTEST_THRESHOLD"] = raw
        refusals.append((f"env={raw!r}",
                         resolve_threshold(None, "ARI_SELFTEST_THRESHOLD")))
    os.environ.pop("ARI_SELFTEST_THRESHOLD", None)
    expected = [None, None, 1, None, None, None, None, 7]   # value, or None if refused
    must_err = [True, True, False, False, True, True, True, False]
    for (label, (val, err)), exp_val, exp_err in zip(refusals, expected, must_err):
        leg_ok = (val == exp_val) and (bool(err) == exp_err)
        if not leg_ok:
            ok = False
        print(f"  {label:<16} -> threshold={val} refused={bool(err)} "
              f"(expected threshold={exp_val} refused={exp_err}) "
              f"{'OK' if leg_ok else 'MISMATCH'}")

    print("\nSELF-TEST [loop flags: with NO threshold set, both signals must say NOT "
          "COMPUTED -- never 'clean' -- while still reporting the distribution]")
    nt = _flags_of(_flag_fixture_lines(7, 4))
    nt_ok = (nt["signal1_span_tool_calls"]["state"] == "not_computed"
             and nt["signal3_no_tool_streaks"]["state"] == "not_computed"
             and nt["signal1_span_tool_calls"]["flagged"] == []
             and nt["signal1_span_tool_calls"]["calls_max"] == 7
             and nt["signal3_no_tool_streaks"]["longest"] == 4)
    print(f"  states={nt['signal1_span_tool_calls']['state']}/"
          f"{nt['signal3_no_tool_streaks']['state']}, distribution still reported "
          f"(max={nt['signal1_span_tool_calls']['calls_max']}, longest streak="
          f"{nt['signal3_no_tool_streaks']['longest']}) "
          f"{'OK' if nt_ok else 'MISMATCH'}")
    if not nt_ok:
        ok = False

    print()
    if ok:
        print("SELF-TEST PASSED -- totals exact, Bash clustering exact, no secret-value "
              "fragment reached any output;\nboth usage negative controls, both manifest "
              "guards, and every loop-flag control fired (including\nthe counterfactual "
              "that makes the flags' exclusion rule falsifiable).")
        return 0
    print("SELF-TEST FAILED -- do NOT believe this instrument's numbers.")
    return 3


def main():
    ap = argparse.ArgumentParser(
        description="Per-tool / per-subagent / per-subsession token breakdown from real "
                    "Claude Code transcripts (read-only).",
        epilog="Manifest format: a JSON list of "
               "{label, transcript_path, parent_label}.\n"
               "Loop-flag thresholds have NO DEFAULT and may be set by env var instead of "
               "flag:\n"
               "  ARI_FLAG_SPAN_TOOL_CALLS, ARI_FLAG_NO_TOOL_STREAK (both refuse a value "
               "below 1).\n"
               "Run --help-blind for this instrument's own failure modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcripts", nargs="*",
                    help="one or more transcript .jsonl paths; give 2+ for a flat "
                         "per-source breakdown without needing a manifest")
    ap.add_argument("--manifest", metavar="PATH",
                    help="JSON manifest describing a tree of SEPARATE sessions to roll up "
                         "(adds agent-level cross-tabs: per-node totals, per-node by-tool, "
                         "and grand by-tool with vs without subsessions)")
    ap.add_argument("--json", action="store_true",
                    help="emit a compact digest for LLM review instead of the text report")
    ap.add_argument("--top", type=int, default=5, metavar="N",
                    help="how many costliest-by-cache-read turns to list (default 5)")
    ap.add_argument("--flag-span-tool-calls", type=int, default=None, metavar="N",
                    help="FLAG each span between consecutive real user turns that made "
                         "MORE THAN N tool calls (strictly greater). Env: "
                         "ARI_FLAG_SPAN_TOOL_CALLS. NO DEFAULT -- unset reports the "
                         "distribution and says NOT COMPUTED, because no published "
                         "threshold is grounded (framework ceilings span 10 to 1000; "
                         "sources in docs/citations.md). Whatever you pass is a starting "
                         "point, not a validated constant")
    ap.add_argument("--flag-no-tool-streak", type=int, default=None, metavar="N",
                    help="FLAG each run of MORE THAN N consecutive assistant turns that "
                         "called no tool at all. Env: ARI_FLAG_NO_TOOL_STREAK. Same "
                         "no-default posture as above (docs/citations.md)")
    ap.add_argument("--survey-user-entries", action="store_true",
                    help="print the census this instrument's span boundaries rest on -- "
                         "every `user` entry by whether it is operator input, and why not "
                         "if not -- then exit. Run it on your own corpus before trusting "
                         "signal 1 on it. Text only: --json is ignored, and says so")
    ap.add_argument("--anonymise", action="store_true",
                    help="hash transcript paths and manifest labels before printing "
                         "(for published output); never alters a token count")
    ap.add_argument("--self-test", action="store_true",
                    help="run the positive and negative controls and exit")
    ap.add_argument("--help-blind", action="store_true",
                    help="print what this instrument prints when it cannot see, and exit")
    args = ap.parse_args()

    if args.help_blind:
        print(__doc__)
        return 0
    if args.self_test:
        return self_test()

    # Resolved BEFORE any file is opened, so an unusable threshold refuses on its own
    # terms rather than after a run that looks like it worked.
    span_threshold, err = resolve_threshold(args.flag_span_tool_calls,
                                            "ARI_FLAG_SPAN_TOOL_CALLS")
    if err:
        print(f"REFUSING: {err}. See --help-blind, blind mode 7.", file=sys.stderr)
        return 2
    streak_threshold, err = resolve_threshold(args.flag_no_tool_streak,
                                              "ARI_FLAG_NO_TOOL_STREAK")
    if err:
        print(f"REFUSING: {err}. See --help-blind, blind mode 7.", file=sys.stderr)
        return 2

    # assemble the set of sources: positional paths + manifest transcripts
    manifest_nodes = None
    if args.manifest:
        manifest_nodes, err = load_manifest(args.manifest)
        if err:
            print(f"REFUSING: {err}. A manifest that cannot be parsed is 'cannot "
                  f"measure', not 'measured nothing'. See --help-blind.", file=sys.stderr)
            return 2

    paths = list(args.transcripts)
    if manifest_nodes:
        for nd in manifest_nodes:
            if nd["path"] not in paths:
                paths.append(nd["path"])

    if not paths:
        print("REFUSING: no transcript given. Pass one or more .jsonl paths and/or "
              "--manifest. See --help-blind.", file=sys.stderr)
        return 2

    # read every source; an unreadable one is tracked as n/a, never counted as 0
    all_turns = []
    sources_read, sources_na = [], []
    own_totals = {}                       # per manifest label, for the rollup
    per_path_turns = {}
    per_path_stats = {}
    for p in paths:
        try:
            with open(p, "rb") as fh:
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            sources_na.append(p)
            continue
        turns, stats = collect_turns(lines)
        per_path_turns[p] = turns
        # Kept PER PATH, not merged: span indices restart at every transcript, so the
        # flags must be computed per source and concatenated (see compute_flags).
        per_path_stats[p] = stats
        all_turns.extend(turns)
        sources_read.append(p)

    if manifest_nodes:
        for nd in manifest_nodes:
            t = per_path_turns.get(nd["path"])
            own_totals[nd["label"]] = totals_of(t) if t is not None else None

    if not sources_read:
        print("REFUSING: not one source was readable, so a zero here would mean 'wrong "
              "path', not 'free session'. See --help-blind.", file=sys.stderr)
        return 2

    if args.survey_user_entries:
        if args.json:
            # Said rather than silently dropped: a caller who asked for machine output and
            # got prose should be told which flag won, not left to diff it.
            print("NOTE: --survey-user-entries is text only; --json is ignored for it.")
        print_user_entry_survey(sources_read, sources_na,
                               [(p, per_path_stats[p]) for p in sources_read], args)
        return 0

    flags = compute_flags([(p, per_path_turns[p], per_path_stats[p])
                           for p in sources_read], span_threshold, streak_threshold)

    cov = coverage(all_turns)
    reason = refusal_reason(cov)
    rollup_rows = rollup(manifest_nodes, own_totals) if manifest_nodes else None
    if reason:
        msg = f"REFUSING: {reason}. See --help-blind."
        if args.json:
            digest = build_digest(sources_read, sources_na, all_turns, per_path_turns,
                                  manifest_nodes, rollup_rows, args, flags)
            digest["measured"] = False
            print(json.dumps(digest, indent=2))
            print(msg, file=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(build_digest(sources_read, sources_na, all_turns, per_path_turns,
                                      manifest_nodes, rollup_rows, args, flags), indent=2))
        return 0

    print_report(sources_read, sources_na, all_turns, args, flags)
    if manifest_nodes is not None:
        print_crosstabs(manifest_nodes, per_path_turns, args)
        print_rollup(rollup_rows, args)
    elif len(sources_read) >= 2:
        print_per_source(sources_read, sources_na, per_path_turns, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
