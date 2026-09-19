# Evidence-Adjudicated Historical PR Protocol Dry Run

`protocol_dry_run = true`  
`final_claim_eligible = false`

This package evaluates the annotation and adjudication protocol. It is not a final benchmark and must not be used for EvoAgent performance, README, or resume claims.

## Package layout

- `selection.json`: purposefully sampled public PR identities and pre-annotation rationale.
- `capture/`: immutable PR metadata, exact diffs, and self-contained Git tree bundles.
- `annotations/runs/`: independent blind A/B outputs, provenance, costs, and raw execution traces.
- `annotations/adjudications/`: independent adjudication for disagreement cases only.
- `results/`: provisional non-claim benchmark records, manifest, pairing/matcher audits, objective-evidence context, and the protocol report.

The source bundles contain deterministic synthetic root commits whose trees exactly match each original base/head revision. Original GitHub commit SHA values remain the provenance identity; `base_tree_sha` and `head_tree_sha` bind the portable snapshot content. This avoids depending on mutable PR heads or missing shallow-clone parent objects.

## Reproduction boundaries

Capture requires authenticated public GitHub CLI access:

```bash
python3 scripts/capture_protocol_dry_run.py \
  evaluation_data/protocol_dry_run_v1/selection.json \
  evaluation_data/protocol_dry_run_v1/capture
```

Prepare frozen read-only checkouts, then run annotation/adjudication only when new independent runs are intended:

```bash
python3 scripts/run_protocol_annotations.py \
  evaluation_data/protocol_dry_run_v1/capture \
  evaluation_data/protocol_dry_run_v1/annotations \
  --prepare-only
```

Regenerate deterministic analysis outputs from the retained annotations:

```bash
python3 scripts/analyze_protocol_dry_run.py
```

The existing run records used `gpt-5.6-sol`; both first-pass runs were blind to EvoAgent predictions, experiment arms, production attribution, and peer annotations. Adjudicators saw only the frozen repository plus A/B evidence and rationale.
