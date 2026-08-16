from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "airfoil_workflow" / "engine"
MANIFEST = ROOT / "ENGINE_MANIFEST.json"
INCLUDED_DIRS = ("airfoil_fluentmeshing", "scripts", "config")


def _engine_files() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDED_DIRS:
        base = ENGINE / name
        if base.is_dir():
            files.extend(
                path
                for path in base.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix.lower() in {".py", ".json"}
            )
    return sorted(files, key=lambda item: item.relative_to(ENGINE).as_posix().lower())


def build_manifest() -> dict:
    aggregate = hashlib.sha256()
    records: list[dict[str, object]] = []
    for path in _engine_files():
        relative = path.relative_to(ENGINE).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
        records.append({"path": relative, "sha256": digest, "size": path.stat().st_size})
    return {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "origin": "controlled reverse merge; source handoff retained separately and is not a runtime dependency",
        "patchset": "airfoil-simulation-portable-2",
        "bundled_hash": aggregate.hexdigest(),
        "bundled_file_count": len(records),
        "bundled_bytes": sum(int(item["size"]) for item in records),
        "files": records,
        "bundled_files": records,
        "patches": [
            {"id": "scoped-fluent-process-tree-cleanup", "reason": "Each local PyFluent session closes only its recorded Fluent, Cortex and MPI process tree"},
            {"id": "solver-evaluation-budget-and-fifth-profile", "reason": "The global evaluation ceiling is enforced and five-run requests have five distinct bounded profiles"},
            {"id": "fluent-251-quiet-mass-flow-readback", "reason": "Signed mass flow and steady-time readback avoid noisy or invalid PyFluent 0.40.2 settings paths"},
            {"id": "default-local-thickness-90-percent", "reason": "The default local thickness ratio is 0.90 while the independent area ratio remains 0.95"},
            {"id": "detached-engine-recovery-and-timeout", "reason": "CLI loss no longer kills the engine; live and dead PID recovery paths are explicit"},
            {"id": "bounded-lift-step-fail-fast", "reason": "Stalled or Cl-infeasible design steps roll back to the finite smaller-step retry ladder"},
            {"id": "nonzero-endpoint-anchor-clips", "reason": "Leading and trailing edge iso-clips use nonzero one-sided coordinate ranges"},
            {"id": "snapshot-heartbeats-and-linked-status", "reason": "Heartbeat snapshots stay outside the event chain and plan status links to authoritative run state"},
            {"id": "timestamped-unified-engine-log", "reason": "Engine stdout and Fluent transcripts are mirrored with timestamped source markers"},
            {"id": "ai-supervised-cgrid-candidates", "reason": "MESH pauses for bounded AI proposals, real Fluent pilots, Pareto comparison and explicit acceptance"},
            {"id": "research-continuation-uses-source-run-lift-gate", "reason": "Continuation inherits and enforces the qualified source lift gate"},
            {"id": "per-run-mesh-cell-budget", "reason": "Schema, candidate evaluation, engine, worker and result validation share the task-specific max_cells budget"},
            {"id": "signed-quarantine-only-experience-transfer", "reason": "Imported experience cannot become positive evidence before local requalification"},
            {"id": "single-authoritative-engine-tree", "reason": "CLI and fixed workers consume src/airfoil_workflow/engine without external source copying"},
            {"id": "candidate-geometry-ratio-gates", "reason": "Minimum area and local thickness ratios are enforced on quick and validation geometry audits"},
            {"id": "cli-only-surface", "reason": "Browser GUI, external AI provider, and legacy mesh-repair page were removed"},
        ],
        "excluded": ["Fluent binaries and license", "historic outputs", ".venv and caches", "credentials", "external source paths"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the immutable engine hash manifest.")
    parser.add_argument("--check", action="store_true", help="Fail if the committed manifest differs; do not write")
    args = parser.parse_args()
    data = build_manifest()
    if args.check:
        if not MANIFEST.is_file():
            raise SystemExit("ENGINE_MANIFEST.json is missing")
        prior = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if prior.get("bundled_hash") != data["bundled_hash"] or prior.get("bundled_files") != data["bundled_files"]:
            raise SystemExit("ENGINE_MANIFEST.json is stale")
    else:
        MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(data["bundled_hash"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
