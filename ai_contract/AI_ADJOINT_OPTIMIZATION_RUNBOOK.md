# AI Adjoint Optimization Runbook

This is the trusted AI operating contract for this package.

## Mode Gate

Unless the user already selected a mode, ask for one before editing or launching:

```text
normal    operate the supported default workflow
advanced  expose full per-run settings and research candidates
debug     read-only diagnosis; repair requires separate authorization
```

Normal and advanced operation must not read `ai_contract\debug` or
`ai_contract\repair_prompts`. A failed normal run records evidence and suggests
debug mode; it does not repair itself.

In debug mode, read `ai_contract\debug\TEST_HISTORY.md` before interpreting
historical runs. Read the repair router only after explicit repair authority.
The debug-only rule is an AI workflow policy, not an OS file permission.

## Source Of Truth

Use these sources in order:

1. `config\adjoint_optimization.example.json` for executable defaults.
2. `README.md` for workflow and status definitions.
3. `USER_GUIDE.md` for supported user commands.
4. Current run summaries and transcripts.
5. Debug history only in debug mode.

Do not trust deleted historical paths or old prose over the current config.

Current normal defaults include Cd `-0.15%`, Fluent Cl bound `99.9%`, final
Cl gate `99.8%`, C-grid primary mesh, and at most one accepted design step.

## Required Normal Workflow

1. Resolve the DAT, run name, mode, and operating inputs.
2. Compute atmosphere, Reynolds number and first-layer height.
3. Fit the airfoil with trailing-edge-preserving CST.
4. Generate the quadrilateral C-grid. O-grid is diagnostic only. After every
   AI C-grid candidate is ineligible, the unchanged fixed C-grid may be invoked
   once through the controlled fallback contract.
5. Read/check/write the mesh in Fluent and reconcile Stage1 gates.
6. Solve baseline flow and save mandatory baseline case/data/Tecplot/Ensight.
7. Close the baseline solver.
8. Start every optimization attempt from the immutable baseline checkpoint in
   a fresh Fluent session and isolated directory.
9. Configure named Cd/Cl observables, verify runtime objective binding, create
   anchors and the default geometry thickness gate, then run one adjoint step.
10. Reject invalid topology, anchor drift, thickness crossing, zero step, or
    disallowed negative-volume history before promotion.
11. Re-open the candidate in a new Fluent session, stabilize forces, verify
    positive volume, required quality, Cd, Cl and L/D gates, and export it.
12. Promote only an accepted candidate and write the summary/report.

Thickness protection is a direct section-by-section comparison of baseline and
candidate geometry. No bounding surface mesh is generated or imported.

## Non-negotiable Rules

- Use `py -3.12`, never bare `python` on this machine.
- Keep optimization adjoint-only; no genetic or manual fallback.
- Do not call, translate, copy or modify an external journal workflow.
- Do not hard-code an input DAT or Fluent executable in source/config.
- Failed attempts cannot write official optimized exports.
- Never describe `SCREENING_PASS` or `INCOMPLETE_PERFORMANCE_TARGET` as a
  completed production PASS.
- Keep baseline and optimized Tecplot exports airfoil-surface-only.

## Debug Evidence Maintenance

Before deleting generated tests or runs, append a dated entry to
`ai_contract\debug\TEST_HISTORY.md` containing:

- input and relevant configuration;
- exact status and completion reason;
- key grid and aerodynamic metrics;
- failure reason without promotion-by-inference;
- retained file/hash locations;
- cleanup disposition.

The history is append-only. Corrections must add a new correction entry rather
than silently rewriting earlier evidence.

Research screening, continuation, resume, final-checkpoint validation and
re-audit scripts are advanced/debug tools and are not part of normal execution.

## Repair Routing

After the user authorizes repair in debug mode, read:

```text
ai_contract\repair_prompts\skills\REPAIR_ROUTER_PROMPT.md
```

Then load only the recommended repair material. Re-run the smallest relevant
test, append the outcome to TEST_HISTORY, and require the normal gates again.
