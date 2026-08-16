# Candidate contract

Submit a JSON object with:

```json
{
  "schema_version": 1,
  "parent_attempt": null,
  "source_mode": "builtin",
  "rationale": {
    "observed_regions": ["trailing_edge", "wake_inlet"],
    "hypothesis": "Describe the physical and geometric cause.",
    "expected_effect": "Describe the metrics and locations expected to improve."
  },
  "parameters": {
    "n_airfoil_side": 220,
    "n_bridge": 10,
    "radial_layers": 64,
    "wake_columns": 180,
    "farfield_distance": 15.0,
    "wake_length": 20.0,
    "growth_rate": 1.12,
    "bl_layers": 42,
    "cst_order": 6,
    "cst_regularization": 0.00001,
    "cst_cosine_weight": 0.65,
    "cst_le_power": 1.45,
    "wake_beta": 5.0,
    "wake_outer_beta": 0.8,
    "wake_center_width": 0.30,
    "outlet_center_cluster": 1.35,
    "outlet_match_blend": 0.90,
    "outlet_match_power": 0.85,
    "outlet_distribution_mode": "quality-adaptive",
    "inlet_arc_match_blend": 0.35,
    "smoothing_iterations": 8,
    "smoothing_relaxation": 0.20,
    "minimum_te_thickness": 0.00025
  }
}
```

Include every parameter required by `mesh_candidate.schema.json`; do not rely on
hidden generator defaults. Use `source_mode: "run_patch"` only after changing
allowed files in `mesh_agent_workspace/engine` and running the requested local
checks.

## Units, ranges, and derived quantities

- `farfield_distance`, `wake_length`, and `minimum_te_thickness` use input-chord
  multiples. Their inclusive ranges are `1..1000`, `1..5000`, and
  `1e-8..0.1` respectively.
- Integer ranges are: `n_airfoil_side 8..5000`, `n_bridge 2..1000`,
  `radial_layers 4..1000`, `wake_columns 2..5000`, `bl_layers 1..1000`,
  `cst_order 2..30`, and `smoothing_iterations 0..1000`.
- The inclusive ranges are `growth_rate 1..2`, `cst_regularization 0..1`,
  `cst_cosine_weight 0..1`, `cst_le_power 0.1..10`, `wake_beta 0..100`,
  `wake_outer_beta 0..100`, `wake_center_width 0.001..10`,
  `outlet_center_cluster 0..100`, `outlet_match_blend 0..1`,
  `outlet_match_power 0.01..10`, `inlet_arc_match_blend 0..1`, and
  `smoothing_relaxation 0..1`.
- Predicted cells are
  `(2*n_airfoil_side-2)*radial_layers +
  (2*radial_layers+max(2,n_bridge)-1)*wake_columns`. The actual generated count
  remains authoritative and must not exceed `max_cells`. `preferred_cells` is
  only a soft cost reference.
- The first-layer height is immutable candidate input derived from Reynolds
  number, turbulence model, target y-plus, and the brief. Total boundary-layer
  height is derived as `h1*N` when `growth_rate=1`, otherwise
  `h1*(growth_rate^N-1)/(growth_rate-1)`, with `N=bl_layers`.

`parent_attempt` records one lineage parent. A third candidate that combines
evidence from several attempts names the nearest parent there and cites all
compared attempts in `rationale`.

An acceptance decision must contain:

```json
{
  "rationale": "Why this candidate matches the local flow physics.",
  "pareto_comparison": "Why no evaluated eligible candidate dominates it.",
  "accepted_with_warning": true
}
```

Set `accepted_with_warning` to true only when the result contains soft warnings.
The workflow independently rejects an ineligible or dominated selection.

Do not submit the fixed fallback as a candidate JSON. After the brief reports
`fallback.status: AVAILABLE`, invoke `mesh-fallback`; its immutable parameters
and canonical generator are supplied by the workflow. Then audit and accept it
through the same `mesh-accept` decision contract. See [fallback.md](fallback.md).
