# Changelog

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
