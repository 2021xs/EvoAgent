# Targeted Evolution Pilot

This is a development pilot, not the final EvoAgent benchmark.

It compares two routing policies over the same twelve controlled missed-issue cases:

- **Naive:** attempt Skill evolution whenever the historical assignment has exactly one Skill.
- **Targeted:** attempt only when the production attribution result is `SUPPORTED`, identifies
  `SKILL_GUIDANCE_GAP`, and routes to that same Skill.

After routing, both arms call the same experiment-local function and therefore use the same
production `SkillPatchGenerator`, one-candidate policy, source replay, and regression gates.
Every case/arm uses a fresh temporary SQLite database. No candidate is promoted.

## Case controls

The case labels are frozen development-fixture labels. They are not inferred from experiment
results. The distribution is:

| Gold root cause | Cases | Intended control |
| --- | ---: | --- |
| `SKILL_GUIDANCE_GAP` | 4 | Evidence is present and the bundled Skill lacks the named local check. |
| `CONTEXT_EVIDENCE_MISSING` | 3 | The persisted managed-context fixture intentionally omits the required line. |
| `MODEL_REASONING_FAILURE` | 3 | Evidence is present and bundled guidance explicitly covers the reasoning pattern. |
| `INSUFFICIENT_EVIDENCE` | 2 | The snapshot is complete enough to associate one Skill, but not to support a causal diagnosis. |

The context-missing manipulation is a controlled checkpoint fixture. It does not claim that the
current `ContextManager` naturally drops those exact examples.

## Deterministic protocol dry-run

The default mode uses a scripted model to validate plumbing. It deliberately returns each frozen
root-cause label. For downstream replay, Skill-gap candidates fix the source case; non-Skill-gap
candidates do not. One Skill-gap candidate deliberately creates a clean-holdout false positive so
the existing regression gate rejects it.

Therefore dry-run attribution accuracy is reported only as **PIPELINE VALIDATION**. It is not
evidence of model intelligence or production review quality.
Scripted call counts are reported, but token and latency values are left unavailable rather than
presented as real provider measurements.

```bash
PYTHONPATH=.:tests .venv/bin/python scripts/run_targeted_evolution_pilot.py
```

## Real-model pilot

When an EvoAgent model is configured, run one non-retried attempt per case/arm:

```bash
PYTHONPATH=.:tests .venv/bin/python \
  scripts/run_targeted_evolution_pilot.py --real-model
```

Real-model results are written separately and compare the observed production attribution against
the same frozen gold labels. A baseline that detects the expected issue is retained as an observed
protocol mismatch rather than silently rerun.

## Metric definitions

- **Attribution Accuracy:** exact match of first divergence and root cause to frozen gold.
- **Routing Accuracy:** evolve/no-evolve decision equals `gold_should_evolve`.
- **Wrong-Surface Evolution Rate:** attempted evolutions among the eight non-Skill-gap cases.
- **Source Failure Resolution Rate:** attempts whose source replay changes baseline FN to candidate TP.
- **Regression Rejection Rate:** source-fixed attempts rejected by protected validation/holdout gates,
  divided by all attempts.
- **Validated Evolution Rate:** `ready_for_promotion` candidates divided by all attempts.

The suggested thresholds are development signals only; this pilot performs no significance tests.
