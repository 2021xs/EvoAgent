# EvoAgent Evidence-Adjudicated Benchmark Guide v1

This protocol is frozen before the 6–10 PR dry run. It is not a benchmark result and must not be
described as human-labelled unless humans actually performed the recorded reviews.

## Frozen source and roles

- Every case binds an immutable base SHA, head SHA, and captured diff hash.
- Annotator A and Annotator B use separate run IDs and contexts against the same frozen source.
- Recommended model access is `FROZEN_REPOSITORY_READ_ONLY`; every access mode is recorded.
- First-pass annotators cannot see EvoAgent predictions, experiment arms, or peer annotations.
- An adjudicator may see A/B evidence and rationale, but not EvoAgent output or experiment arm.
- Original labels are append-only and never replaced by adjudication.

`annotator_kind` is `MODEL`, `HUMAN`, or `OBJECTIVE_EVIDENCE`. Model records freeze provider,
model revision, prompt identity, context/tool policy identities, run ID, timestamps, and blindness.
Null model fields are permitted only when they do not apply to human or objective-evidence records.
No API key or secret belongs in benchmark metadata.

## Repository-grounded decisions

Each finding makes two separate decisions:

1. `issue_exists`: the technical issue exists in the frozen revision.
2. `should_comment`: a developer-facing comment is useful and actionable for this PR.

`issue_exists=true, should_comment=false` remains valid. A positive model label requires concrete
repository evidence references/ranges and rationale. Empty findings do not prove a clean case;
independent completed reviews or directly resolving objective evidence must qualify the clean label.

## Evidence strength and agreement

`gold_evidence_level` is categorical provenance, not a calibrated confidence score:

- `OBJECTIVE`: directly resolving tests, compiler/runtime evidence, deterministic facts, advisories,
  or a fixing commit whose evidence specifically supports this issue formulation.
- `INDEPENDENT_MODEL_AGREEMENT`: two blind, separate model runs agree structurally.
- `MODEL_ADJUDICATED` / `HUMAN_ADJUDICATED`: disagreement resolved by a separately recorded blind
  adjudicator of that kind.
- `QUARANTINED`: unresolved or insufficient evidence; excluded from primary metrics.

Agreement is reported separately for technical existence, logical issue identity, taxonomy,
location, severity, and `should_comment`. Technical and comment-usefulness agreement are not
collapsed into JSON equality. Objective evidence takes precedence only where it directly resolves
the precise field; a later fix does not validate every inferred issue description.

## Attribution Gold

Attribution annotators evaluate observable failure layer, supported evolution surface, target Skill,
and status from frozen execution evidence. They cannot see the production AttributionResult being
scored. Deterministic trace evidence may establish an observable layer, but does not by itself prove
a semantic evolution route. Objective Attribution Gold must state which fields each evidence item
supports.

## Protocol dry run

The first 6–10 cases set `protocol_dry_run=true` and never enter final metrics. Use two independent
Codex/model runs and a separate adjudication run where necessary. Collect annotation time, tool
calls, available token usage, technical agreement/disagreement, `should_comment` disagreement,
quarantine rate, and missing-context rate. Do not call this the formal pilot.

## Permitted claim

Generic wording: **Evidence-Adjudicated Historical PR Benchmark**.

If model annotators are used, methodology must say so explicitly. `human-reviewed` or
`human-labelled` is permitted only when the recorded provenance shows that humans did that work.
