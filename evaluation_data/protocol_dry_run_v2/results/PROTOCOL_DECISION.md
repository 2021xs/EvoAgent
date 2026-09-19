# Evidence-Adjudication Protocol Dry Run v2 — decision record

`protocol_dry_run = true` · `final_claim_eligible = false` · `dry_run_iteration = 2`

This is methodology evidence, not an EvoAgent performance benchmark. The v1 capture,
annotation, adjudication, and result files were not changed. v2 interpretation is
derived separately. The six v2 cases were sampled and classified from frozen diffs
before any EvoAgent prediction was inspected. Two requested “multi-finding” buckets
were corrected to “multi-concern review stress”: inspecting a diff cannot establish
in advance that it contains two defects.

## Observations

| Measure | v1 (8 PR, 5 repositories) | v2 (6 PR, 4 repositories) |
| --- | ---: | ---: |
| First-pass annotations | 16 | 12 |
| Technical agreement | 6/8 | 5/6 |
| `should_comment` agreement | 7/8 | 5/6 |
| Full structured agreement | 6/8 | 5/6 |
| Cases requiring adjudication | 2/8 | 1/6 |
| Quarantine | 0/8 | 0/6 |
| First-pass missing-context requests | 0/16 | 0/12 |
| Mean input tokens per first pass | 164,421.69 | 98,023.25 |
| Mean tool calls per first pass | 5.25 | 4.5 |
| Mean unique files inspected | not instrumented | 4.17 |
| Mean repeat file reads | not instrumented | 3.67 |

These are different PR sets; the agreement and cost columns are descriptive, not
a paired comparison. v2 has zero two-sided paired positive issues and one one-sided
finding, upheld by a blind adjudicator. Hence the real alias/multi-finding pairing
stress remains untested. The adjudicator noted it did not independently inspect the
base-side object for that issue; do not upgrade it to objective gold on that basis.

The one same-frozen-case Flask #6096 control yielded 308,422 input tokens and 9
tool calls under v1 versus 168,816 and 11 under v2: −45.26% tokens, +2 calls.
The v1 first passes both reported no findings; the v2 first passes split 0/1
and requested no additional context. A subsequent independent adjudicator upheld
the v2 one-sided claim; an earlier attempt requested more context because the
synthetic snapshot refs were insufficiently explained. Both raw attempts are
preserved. This single stochastic control does not prove quality non-inferiority.

## Evidence and cost boundaries

- Frozen urllib3 #4960 includes regression assertions supporting its specific
  buffered-decoding behavior, but the snapshot lacks generated `urllib3._version`.
  The attempted local pytest run failed before test collection. It is *not* a
  runtime-confirmed objective finding or whole-case-clean proof.
- The trace measures aggregate input tokens, tool calls, inspected paths, repeat
  file reads, and tool-output characters. Provider source-level token allocation
  is unavailable and remains null. v2 had 44 repeat file reads and 433,149
  tool-result characters over 12 first passes; neither is a token estimate.
- Full frozen source remains available read-only, with small initial context and
  on-demand bounded file/search reads. Source bundles are the retained capture;
  ephemeral reconstructed Git checkouts are not benchmark artifacts.
- Capture now records the PR compare merge base rather than a mutable current
  base-branch tip. No v1 source/label was rewritten; the corrected policy applies
  to v2 and later captures only.

## Readiness gates

| Gate | Decision | Basis |
| --- | --- | --- |
| Protocol | **NEEDS ANOTHER REVISION** | Pairing and taxonomy stress absent; one adjudication has a base-read caveat. |
| Annotation cost | **TOO EXPENSIVE** | 98,023 mean tokens; extrapolating 96 first passes is ~9.41M input tokens before adjudication, without provider price evidence. |
| Taxonomy | **NORMALIZATION SUFFICIENT** for exact aliases only | Deterministic tests preserve raw/canonical labels and reject real category/CWE differences; no positive paired alias case in v2. |
| Issue pairing | **CURRENT APPROACH SUFFICIENT** provisionally | No observed ambiguity justifies redesign; no multi-positive pair tested. |
| Objective evidence | **SCOPE CONTRACT SUFFICIENT** | Validators prevent behavior-only tests becoming whole-case-clean gold; runtime execution not yet available. |
| Hybrid matcher | **NOT JUSTIFIED** | No observed paired matcher ambiguity; no LLM Judge was used. |

Before a formal pilot, repeat bounded same-case cost controls, capture genuinely
multi-positive independent annotations without selecting on EvoAgent output, verify
the base/head snapshot instructions during adjudication, and make objective test
build prerequisites explicit. Do not pool these dry runs into final metrics.
