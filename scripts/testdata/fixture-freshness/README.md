# Test data for the fixture-freshness gate (ARI#22)

`no-bash-turns.pre-ari16.out` is **not** a synthetic drift. It is the real, byte-for-byte
content that `samples/measure_token_breakdown.no-bash-turns.out` actually had at commit
`ee705ad` on `issue-17-signal2-repeats` — the conflict-free-but-stale state that ARI#22 was
filed about. Commit `d0c721a` is the fix that replaced it.

It is kept here rather than regenerated from that SHA because the branch was squash-merged
(as `4a5315f`), so `ee705ad` is reachable only from a branch ref that will be deleted and is
eventually collectable. A test that depends on an unreferenced SHA surviving garbage
collection is a test that passes until it mysteriously cannot run.

Why the bytes are still a faithful replay rather than a re-enactment: the instrument at
`ee705ad` is **identical** to the one on `main` at the time this gate was written, so the
stale fixture is the only difference between that tree and a correct one. Dropping these
bytes onto a current checkout reproduces the original incident exactly.

It lives under `scripts/testdata/` and not under `samples/` on purpose. It is deliberately
WRONG output, and `samples/` is the published artefact set — the same hazard that
`samples/gate-evidence/` documents at length, where a file recording a wrong figure made that
wrong figure findable by a gate searching for it. Keeping known-wrong captures out of
`samples/` also means the freshness gate does not have to carve out an exception for its own
test data.
