# capability_suites

Public and hidden capability suites for TL-Ego external evaluation (Task 4).

## Layout

- `manifest.schema.json` — JSON Schema for frozen suite manifests
- `public/` — visible regression/benchmark tasks (candidates may read these)
- `hidden/` — hidden challenges (readable only by the evaluator, controlled by
  the governance root). Each hidden suite is pinned by a frozen manifest whose
  assets are SHA-256 hashes; the candidate never gets the asset contents.

## Rules

1. A candidate may read `public/` freely.
2. A candidate must NOT read `hidden/` — the evaluator verifies asset hashes
   against the frozen manifest before running, and rejects any tampering.
3. Only a hidden-suite run with duplicated capability gain may authorize
   `promote`.
