# EvoAgent Evidence-Adjudication Guide v2

This is protocol-dry-run methodology, never final-claim evidence.

Review the frozen base-to-head change with the full repository available read-only. Start from the
changed-file list and diff, then read only bounded relevant ranges on demand. Do not load external
review skills or unrelated repository files. Do not print whole files when a focused range or search
answers the question. Re-reading is allowed only when required to resolve evidence.

First-pass annotators are blind to EvoAgent predictions, experiment arms, production attribution,
and peer annotations. Findings must be introduced by, or materially relevant to, the frozen PR.
Do not report the bug intentionally fixed by the PR as a newly introduced problem.

For each positive finding, record an independent local ID, raw category, optional canonical CWE,
exact path/range evidence, technical rationale, severity, and a separate `should_comment` decision.
Taxonomy is later normalized deterministically; do not coordinate wording with another annotator.

If repository evidence is insufficient, request exact missing context. Do not force findings or
clean labels. Objective evidence supports only its declared scope; a regression test for one
behavior does not establish that the whole PR is clean.
