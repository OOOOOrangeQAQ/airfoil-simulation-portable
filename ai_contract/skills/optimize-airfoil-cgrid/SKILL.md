---
name: optimize-airfoil-cgrid
description: Design, generate, diagnose, adapt, and qualify two-dimensional structured C-type airfoil meshes for Ansys Fluent. Use when an airfoil workflow is waiting in MESH, when creating or repairing a C-grid, when choosing boundary-layer or wake distributions, or when Fluent reports poor orthogonal quality, skewness, aspect ratio, abrupt size change, invalid cells, or mesh-related convergence trouble.
---

# Optimize Airfoil C-grid

Treat mesh generation as a bounded, evidence-driven design task. Do not use one
fixed cell count or accept a candidate from one global quality number.

## Required workflow

1. Read `mesh_brief.json`, the candidate schema, all prior attempt results, and
   the supplied geometry and quality plots.
2. Read [method.md](references/method.md) before designing the first candidate.
3. Read [acceptance.md](references/acceptance.md) and
   [diagnostics.md](references/diagnostics.md) before judging any candidate.
4. Read [adaptation.md](references/adaptation.md) after a failed or warning
   candidate. Read [candidate-contract.md](references/candidate-contract.md)
   before writing a proposal or acceptance decision.
5. Submit one complete candidate with `mesh-evaluate`. Inspect Python and real
   Fluent evidence, including the 100-iteration pilot.
6. Compare every eligible candidate as a Pareto set. Never rank only by minimum
   orthogonal quality, maximum aspect ratio, or cell count.
7. Stop AI redesign after `max_candidates`. Accept an eligible non-dominated
   AI candidate when one exists. If every AI candidate is ineligible, read
   [fallback.md](references/fallback.md) and run the one unchanged fixed-grid
   fallback. Never invoke that fallback early or silently.
8. Apply the same evidence review and explicit Pareto acceptance to an eligible
   fallback. If it fails, leave the run in `MESH` and report the unresolved
   limitation; do not retry it or weaken a gate.

## Candidate design rules

- Derive the first wall-normal height from the flow condition, turbulence
  model, and target y-plus in the brief. Do not replace it with a memorized
  absolute height.
- Allocate nodes independently to the leading edge, upper boundary layer,
  lower boundary layer, trailing edge, C-wrap, wake inlet, wake core, wake
  outlet, and far field.
- Permit high aspect ratio only where the long direction follows the intended
  wall-tangent or wake-streamwise physics and orthogonality remains sound.
- Keep changes in surface spacing, wall-normal spacing, and wake spacing
  gradual. Treat abrupt neighboring-cell size change as a separate defect even
  when individual cell shapes appear acceptable.
- Judge the distribution, prevalence, direction, and clustering of poor cells.
  Never let a good global average hide a weak regional band.
- Preserve a closed wall, paired C-cut lines, pure quadrilateral topology, and
  the required `airfoil`, `velocity_inlet`, and `pressure_outlet` zones.
- Record why every changed parameter should improve a named region. Do not
  perform unstructured parameter guessing.

## Source adaptation

Prefer parameter changes. If the available parameterization cannot address the
observed region, modify only the run-isolated mesh workspace allowed by the
brief. Run compilation and focused mesh tests before evaluating the patched
generator. Never modify solver, optimizer, execution, or canonical package
source during a production mesh run. Keep the generated source diff and hash as
part of the candidate evidence.

## Acceptance

Reject every structural or Fluent hard failure. Treat the Fluent minimum
orthogonal-quality safety floor as necessary but not sufficient. A candidate
with soft warnings may be accepted only when its Fluent pilot passes and the
decision explains the regional tradeoff. Treat all diagnostic bands as
calibratable soft evidence rather than universal CFD limits. If full baseline
calculation later reports a mesh-classified failure or a material actual-y-plus
mismatch, return to `MESH` and use the remaining candidate budget.

Do not claim grid independence from a single accepted mesh. Grid convergence is
a later scientific qualification.
