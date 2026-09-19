# Evidence-Adjudicated PR Annotation — independent first pass

You are one independent first-pass annotator in a protocol dry run. Review only the frozen Git
repository available in the current directory. The checkout is detached at the exact head revision.
Compare the supplied immutable base and head SHAs using local read-only Git and repository tools.

You must not seek, infer, or inspect EvoAgent predictions, experiment arms, another annotator's
output, or a production AttributionResult. Do not use network access. Do not edit the repository.

Find technically real issues caused by or materially relevant to this PR change. Do not report the
bug the PR is intentionally fixing as though the fix itself introduced that bug. A positive finding
needs exact repository-grounded path/range evidence and a concrete technical rationale. Decide
`issue_exists` and `should_comment` separately; a true technical issue can reasonably be
`should_comment=false`. Use your own local `annotation_finding_id`; do not coordinate identifiers
with another annotator.

If the frozen repository is insufficient, return `NEEDS_MORE_CONTEXT` and name the exact missing
files, generated artifacts, runtime state, or external specification needed. An empty finding list
means you completed the review and found no sufficiently supported issue; explain that in the
summary. Do not force a finding.
