# Fixed C-grid fallback

The fixed fallback is a recovery path, not a competing design strategy and not
an automatic quality waiver.

## When it is available

Read `mesh-brief --run-id ...` and inspect `fallback.status`. Call
`mesh-fallback --run-id ...` exactly once only when the status is `AVAILABLE`.
That status requires both:

- all `max_candidates` AI candidate slots have been consumed; and
- no AI candidate is eligible for acceptance.

Do not call it while an AI slot remains or while an eligible AI candidate can
still be judged. The workflow enforces both rules.

## What remains fixed

The fallback uses the canonical packaged C-grid implementation and its legacy
48,458-cell parameter set:

- `n_airfoil_side=214`, `n_bridge=10`, `radial_layers=58`;
- `wake_columns=190`, `bl_layers=42`, `growth_rate=1.12`;
- the remaining values recorded under
  `brief.generator.legacy_fixed_fallback.parameters`.

Do not edit these parameters. Run-scoped source patches are deliberately
ignored. Compare the recorded parameter hash and generator tree hash with the
brief so the result remains auditable.

## Audit and stopping rule

The fallback must pass every normal structural, cell-budget, Fluent-read,
minimum-OQ, and 100-iteration pilot gate. Inspect its regional evidence and
warnings exactly as for an AI candidate. It remains unaccepted until
`mesh-accept` records an explicit engineering and Pareto rationale.

If the fallback fails, stop in `MESH`. Do not run the former
`distribution_repair_48k` ladder, select by one minimum-OQ number, or route a
solver/optimizer failure into mesh repair.

The separate 25,182 / 44,098 / 77,340 meshes are a later GCI qualification
family. They are not fallback candidates and do not participate in this MESH
selection sequence.
