# Targeted Evolution Pilot

> PIPELINE_VALIDATION_NOT_MODEL_INTELLIGENCE

| Metric | Naive | Targeted |
| --- | ---: | ---: |
| Attribution Accuracy | 1.0 | 1.0 |
| Routing Accuracy | 0.3333 | 1.0 |
| Wrong-Surface Evolution Rate | 1.0 | 0.0 |
| Evolution Attempts | 12 | 4 |
| Source Failure Resolution Rate | 0.3333 | 1.0 |
| Regression Rejection Rate | 0.0833 | 0.25 |
| Validated Evolution Rate | 0.25 | 0.75 |
| LLM Calls | 552 | 464 |
| Total Tokens | None | None |
| Latency ms | None | None |

## Development gates

- `skill_gap_attribution_at_least_3_of_4`: **PASS**
- `targeted_non_skill_wrong_evolution_at_most_1_of_8`: **PASS**
- `validated_at_least_half_of_routed_skill_gaps`: **PASS**

## Interpretation guard

The scripted mode validates routing and evaluation plumbing. Its frozen attribution responses do not measure model intelligence.

Real-model results: `UNKNOWN / NOT RUN`.
