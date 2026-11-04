Verbatim payloads and protocol traces, kept as EVIDENCE, and excluded from
`scripts/check_figures.sh`'s artefact search for the same reason `samples/fixtures/` and
`samples/gate-evidence/` are.

An artefact is a thing an instrument PRINTED. A file in here is a thing a client or a server
SENT — arbitrary JSON, full of numbers nobody here chose. Searching it to satisfy "this prose
figure exists in an artefact" makes any value occurring anywhere in a captured payload
permanently quotable.

That is not hypothetical. `drive_dsh.live-events-redacted.out` was committed as evidence for a
notification COUNT, and a review immediately used it to launder a wrong figure into README's
engine table: a `max_tokens` value the client sent appears inside it, so the table could claim
that as its wire-byte figure with the whole preflight green.

Note what that cost to write down. An earlier draft of this file, and of README, QUOTED the value
-- which forced an exemption for it in `check_figures.sh`, and the exemption re-opened the very
hole the paragraph was describing: with the number exempt, the table could carry it again and the
gate said nothing. Verified on a clone, with every other fix in place. So the value is described
here and never written. A laundered figure is the one kind of defect this repository's own
"quote the wrong number beside its correction" idiom cannot be used on. The figures these files
exist to evidence are pinned explicitly in `check_figures.sh`'s PAIRS instead, which is a claim
about one string in one file rather than a search for a number.
