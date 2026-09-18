# Bounded Critic Evidence Challenge Pilot

> PROTOCOL_EXPERIMENT_PLUMBING_VALIDATION

| Metric | One-shot | Bounded |
| --- | ---: | ---: |
| Final Critic Verdict Accuracy | 0.3 | 1.0 |
| Challenge Trigger Rate | 0.0 | 0.7 |
| Useful Challenge Rate | None | 1.0 |
| Verdict Correction Rate | None | 1.0 |
| Unnecessary Challenge Rate | 0.0 | 0.0 |
| LLM Calls | 40 | 61 |
| Total Tokens | None | None |
| Model Latency ms | None | None |

## Comparison

- Extra LLM calls: `21`
- Extra total tokens: `None`
- Downstream Lead/Gate equivalent: `True`

## Interpretation guard

The scripted run validates protocol and experiment plumbing only. Frozen scripted verdicts are not evidence of Critic model quality.

Real-model results: `UNKNOWN / NOT RUN`.
