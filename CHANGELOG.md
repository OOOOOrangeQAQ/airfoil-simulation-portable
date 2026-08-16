# Changelog

## 2.0.1 - 2026-08-16

- Promoted the reviewed `2.0.1rc1` AI-supervised C-grid workflow and `2.0.1rc2` observed-bug fixes to the stable software release.
- Added region-aware C-grid diagnostics for OQ/skewness distributions, upper/lower boundary layers, local area degeneration, wall-normal alignment and wake-direction alignment.
- Replaced fixed mesh retry selection with bounded AI candidate evaluation, a 100-iteration first-order Fluent pilot and explicit Pareto acceptance.
- Changed the default pressure-velocity coupling to `Coupled` and the default minimum local-thickness ratio to 90%.
- Fixed Fluent session cleanup, mass-flow/time readback, resume state, evaluation-budget enforcement, result classification and Chinese intent aliases.
- Kept scientific qualification at `PROVISIONAL`; stable software status does not claim mesh independence or production-qualified CFD evidence.

## 2.0.1rc2 - 2026-08-16 (unpublished bug-fix candidate)

- Lowered the default minimum local-thickness ratio from 95% to 90%; the minimum area ratio remains 95%.
- Recognized both `钝尾缘` and `钝后缘`, and accepted phrases such as `运行5次优化` as solver-evaluation budgets.
- Waited for each run-owned Fluent/MPI process tree to exit, with a scoped timeout/force fallback, preventing session accumulation and MPI shared-memory failures.
- Avoided the noisy Fluent 2025 R1 `pm/boundaries` mass-flow wrapper and corrected the steady/time physics readback path.
- Returned the post-execution run snapshot from `resume`, classified exhausted infeasible searches as budget exhaustion, and enforced the global solver-evaluation ceiling.
- Added a fifth standard reduced-step profile so a five-evaluation request can exhaust five distinct profiles when no candidate is accepted earlier.
- Treated the intentional AI-mesh checkpoint as a successful CLI command and separated objective-binding verification from design-step validity in audit output.

## 2.0.1rc1 - 2026-08-15 (GitHub pre-release candidate)

- Made Fluent's pressure-velocity coupling default to `Coupled` even when the configuration field is omitted; spatial upwind discretization remains independently configured.
- Isolated the engine process from the invoking CLI and added live-process reattachment, dead-PID recovery, cancellation, and a 300-second progress timeout.
- Kept high-frequency heartbeat data out of the append-only event chain and reduced the default heartbeat interval to 30 seconds.
- Rejected stalled or Cl-infeasible Fluent optimizer steps before candidate validation and continued with the configured smaller-step retry profile.
- Replaced zero-width leading/trailing-edge clip ranges with safe one-sided ranges.
- Switched Fluent session shutdown to an explicit graceful exit without PyFluent's automatic force-kill fallback.
- Linked plan status to the authoritative run status and unified engine stdout plus Fluent transcripts in `engine.log` with source timestamps.
- Added the portable `optimize-airfoil-cgrid` Skill and a run-scoped AI mesh brief, candidate schema, isolated mesh source workspace, patch evidence and source allow-list.
- Replaced the fixed 48,458-cell retry ladder with `WAITING_FOR_AI_MESH`, bounded candidate evaluation, a real 100-iteration first-order Fluent pilot, explicit Pareto acceptance and checkpointed resume.
- Made the default 80,000 cells a configurable per-run hard budget and preserved the old 48,458/80,000 request only as a soft-preference compatibility input.
- Expanded C-grid evidence with OQ/skewness percentiles and threshold prevalence, local area-degeneration ratios, separate upper/lower boundary-layer reports, wake-core reporting, and directional alignment diagnostics; kept every new band as calibratable soft evidence.

## 2.0.0 - 2026-08-15

- Removed all GUI pages, GUI providers, browser analysis features, and NiceGUI dependencies.
- Made the strict CLI workflow the only public interface.
- Upgraded the public contract to JobSpec 2.0 and removed unused configuration keys.
- Rejected unsupported `closure="both"` at the public contract boundary.
- Added strict run ID matching and a CWD-independent default state root.
- Forced UTF-8 subprocess I/O on Windows.
- Connected minimum area and local-thickness ratios to candidate-geometry gates.
- Preserved the 48,458-cell target and 80,000-cell hard safety limit.
- Added regression coverage for the audited failure modes.

## 1.0.0

- Initial portable workflow package with CLI and legacy GUI surfaces.
