# EvoAgent Final Benchmark Methodology v3

Status: frozen methodology; no final performance claim is produced by this document.

## Conceptual model

A frozen historical PR branches into evidence seeds, supplementary blind A/B repository review, and the three experimental arms. Qualified seeds measure detection of known evidence-backed issues. Arm predictions are pooled without arm identity and independently adjudicated for technical validity and usefulness. The benchmark is open-world: neither seeds nor supplementary review are represented as an exhaustive list of every possible issue.

## SeededFinding and evidence qualification

`SeededFinding` v3 records the repository and affected base/head revisions; taxonomy and location where supportable; severity and `should_comment`; one bounded technical proposition; construction-only evidence; qualification status; independent verification provenance; and rationale. Admissible evidence is limited to regression tests, reproducers, fix commits with matching evidence, issues plus verified fixes, security advisories, deterministic runtime failures, or explicitly verified objective evidence. Titles, labels, commit messages, later source changes, and maintainer wording do not qualify by themselves. Evidence qualifies only the proposition it directly supports and never establishes whole-PR completeness.

The verifier receives the frozen repository, affected revision, candidate proposition, and supporting construction evidence. The verifier never receives an EvoAgent prediction, arm identity, or whether the seed was detected. Outcomes are `QUALIFIED`, `REJECTED`, `NEEDS_MORE_CONTEXT`, or `QUARANTINED`; only `QUALIFIED` and `should_comment=true` seeds enter Recall. Directly decisive objective evidence does not require ritual duplicate open-ended review.

## Future-information boundary

Future fixes, regression tests, issue reports, and advisories may be used only as benchmark-construction evidence. EvoAgent receives the frozen repository state and runtime context legitimately available at the reviewed revision. The v3 context validator rejects seed/evidence fields and requires `FROZEN_REPOSITORY_READ_ONLY`.

## Recall and matching

`Seeded Issue Recall = detected qualified seeded findings / all qualified seeded findings`. `High-Risk Seeded Issue Recall` uses high/critical qualified seeds. Reports include numerator, denominator, value, eligible seed count, and repository count. They never call this overall, exhaustive, or all-bug Recall. The existing deterministic one-to-one matcher supplies taxonomy/path/range matching. Multiple plausible edges are `AMBIGUOUS` and audited separately; no LLM matcher is used. `should_comment` remains an explicit seed attribute and is not used to hide a qualified seed from this frozen denominator.

## Prediction pooling, duplicates, and blind adjudication

After A/B/C finish, every emitted finding enters one pool. The adjudicator view removes arm and release identity, cross-arm emission information, and desired metric direction, then uses a recorded random order. Clearly identical logical findings share one adjudication; ambiguous equivalence remains separate. Each arm retains raw, logical, and duplicate counts and receives no repeated TP-like credit for duplicates.

Every unique prediction is judged against the frozen repository for `issue_exists`, `should_comment`, severity, location support, technical evidence/rationale, and usefulness rationale. Unresolved cases remain `NEEDS_MORE_CONTEXT` or `QUARANTINED`. An unmatched prediction is never an automatic FP: it is valid when blind adjudication returns `issue_exists=true AND should_comment=true`.

`Adjudicated Precision = valid commentable eligible adjudicated logical predictions / all primary-metric eligible adjudicated logical predictions`. Also report `False Positives / PR`, `Actionable Findings / PR`, and `High-Risk Valid Prediction Rate`.

## Supplementary review and qualified clean subset

Independent blind A/B repository review discovers additional issues, audits seed incompleteness, stresses pairing, measures reviewer agreement, and may support a qualified-clean subset. It is supplementary, not exhaustive Gold. Findings enter a later dataset version only through the same seed qualification process. After final freeze, discoveries can affect Precision adjudication but cannot alter Recall.

A case is `QUALIFIED_CLEAN` only after two independent completed blind reviews, no unresolved disagreement, no known historical seed, no later contradictory evidence, and an explicit completed-review rationale. Absence of seeds never implies clean. Clean PR Accuracy is secondary and restricted to this subset.

## Three-arm fairness and failures

A is baseline with no automatic evolution. B is benchmark-only unconditional `GLOBAL_PROMPT` evolution. C uses Attribution and EvolutionRouter for a supported target or no evolution. All arms share R0, model/provider/revision and generation config, tools and budgets, token/time/step limits, context and FindingGate policy, repository snapshots, controlled failure stream and order, candidate budget, promotion limit, operational gate, matcher, metric contract, and case order. Stores and runtime state are isolated.

The primary comparison is `CONTROLLED_SHARED_FAILURE_STREAM`: each arm receives the same frozen benchmark-generated failures in the same order. Fully closed-loop endogenous failure experiments must be separately named and reported. Attribution correctness, routing correctness, candidate improvement, and promotion outcome remain separate; F1 improvement alone does not prove attribution correctness.

`Wrong-Surface Evolution Rate = incorrectly routed adjudicated opportunities / all adjudicated attribution/evolution opportunities`. `Evolution Success Rate = opportunities whose source failure improves without protected regression / all adjudicated attribution/evolution opportunities`. `Regression Rate = candidates causing a protected regression / all generated candidates evaluated on protected cases`. Promotion and duplicate-candidate rates use generated candidates as their denominator.

## Statistics, report, and claims

Final inference uses a repository-cluster paired bootstrap with 10,000 iterations, one recorded seed, and 95% percentile intervals. Repository clusters are sampled with replacement; all their cases, seeds, or prediction outcomes are included, and global TP/FN or TP/FP is recomputed. Report point estimates, paired deltas, CI, repository count, PR count, seeded-finding count, and prediction count.

Headline metrics are Seeded Issue Recall, High-Risk Seeded Issue Recall, Adjudicated Precision, False Positives / PR, Actionable Findings / PR, Wrong-Surface Evolution Rate, Regression Rate, Evolution Success Rate, Total Tokens, LLM Calls, and Latency. Qualified Clean PR Accuracy, Severity Accuracy, and Location Accuracy are secondary. Cost records retain input/output tokens, calls, latency, provider cost when available, tool calls, and files inspected.

Permitted claim: “EvoAgent was evaluated on a repository-disjoint historical PR benchmark. Detection was measured against independently verified evidence-seeded issues, while additional model findings were evaluated through blind repository-grounded adjudication.” Model-based annotators must be disclosed as model-based. Claims of exhaustive labels, exhaustive PR Recall, or human-labelled data are forbidden unless independently true.

## Final-holdout freeze

Before opening the final holdout, freeze the cases, seeds, repository split, prediction-adjudication protocol, matcher, metrics, three-arm configuration, controlled failure stream, and run-manifest template. After opening it, do not modify seeds, matcher, denominators, prediction qualification, or candidate routing. A correctness flaw invalidates the run and requires a new benchmark version. Dry Run v1/v2 remain immutable v2 protocol evidence and are not transformed into v3 final Gold.
