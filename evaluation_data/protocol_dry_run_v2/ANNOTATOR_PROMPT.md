# Independent evidence annotation — protocol dry run iteration 2

Use only the frozen local repository. Do not use network access, external/global skills, EvoAgent
outputs, experiment-arm information, production attribution, or peer annotations. Do not edit.

Begin with `git diff --stat refs/evoagent/base refs/evoagent/head` and the frozen diff. Inspect only
relevant bounded ranges and nearby tests/configuration. The full repository remains available for
on-demand grounding. Avoid repeated reads and whole-file output over 200 lines.

Return only the required JSON. Positive findings require exact evidence. Empty findings are valid
only after completing the bounded repository-grounded review. Request missing context explicitly.
