# Bounded Critic Evidence Challenge Pilot

This is a controlled development pilot, not a final benchmark.

It compares:

- **One-shot Critic:** Critic Pass 1 is final; the experiment-only reviewer subclass disables routing.
- **Bounded challenge:** the production `REQUEST_EVIDENCE → EVIDENCE_RESPONSE → Critic Final`
  path is available, with at most one round.

Both arms use the same frozen Worker Finding, Critic response policy, Lead decision, FindingGate,
case order, repository files and role budgets. Each case and arm gets a fresh temporary repository
and SQLite store.

## Controlled cases

| Category | Count | Gold behavior |
| --- | ---: | --- |
| `missing_support` | 4 | Pass 1 wrongly rejects a plausible Finding. One repository fact supports Accept. |
| `counter_evidence` | 3 | Pass 1 wrongly accepts a misleading Finding. One repository fact supports Reject. |
| `sufficient` | 3 | The Candidate projection already supports the correct verdict; no challenge is needed. |

Every repository fact has a unique `PILOT_EVIDENCE:<case_id>` marker. The marker is prohibited
from the diff and Candidate fields. A challenge counts as useful only when the production evidence
Worker returns an evidence ref whose actual tool output contains that marker. A scripted label alone
cannot make a challenge useful.

## Deterministic protocol run

```bash
PYTHONPATH=.:tests .venv/bin/python scripts/run_bounded_evidence_pilot.py
```

This mode deliberately scripts the frozen Worker Finding, Critic verdicts and evidence summary.
Repository evidence is still obtained through the production Worker tool loop and the production
typed-message/checkpoint protocol.

Results must be labelled:

```text
PROTOCOL / EXPERIMENT-PLUMBING VALIDATION
```

They are not evidence of model intelligence or real-world review quality.
Scripted LLM call counts are reported, but token and latency fields remain unavailable (`null`)
instead of presenting fixture accounting as provider cost.

## Real-model pilot

Configure the normal EvoAgent model environment, then run exactly one pass:

```bash
PYTHONPATH=.:tests .venv/bin/python \
  scripts/run_bounded_evidence_pilot.py --real-model
```

Lead decisions and initial Worker Findings remain frozen so both arms receive identical inputs.
Only Critic Pass 1, evidence-only Worker investigation and Critic Final use the configured model.
The script does not retry an unfavorable result. If no model is configured, the real-model output is
written as `UNKNOWN / NOT RUN`; scripted results are not substituted.

## Metrics

- **Final Critic Verdict Accuracy:** final verdict equals frozen gold.
- **Challenge Trigger Rate:** challenged cases divided by all cases.
- **Useful Challenge Rate:** challenged cases returning a new marker-backed repository evidence ref.
- **Verdict Correction Rate:** challenged cases changing a wrong Pass-1 verdict into gold.
- **Unnecessary Challenge Rate:** challenges among the three sufficient-evidence cases.
- **Extra LLM Calls/Tokens/Latency:** bounded total minus one-shot total.

Lead Final and Gate outputs are compared structurally for every paired case. Any mismatch makes the
pilot command fail.

## Outputs

Default deterministic outputs:

```text
output/bounded-evidence-pilot/
  summary.md
  summary.json
  case_results.jsonl
```

Real-model mode uses `real-model-` filename prefixes in the same directory so it cannot overwrite
the deterministic dry-run.
