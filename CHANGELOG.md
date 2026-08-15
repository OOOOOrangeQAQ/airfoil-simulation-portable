# Changelog

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
