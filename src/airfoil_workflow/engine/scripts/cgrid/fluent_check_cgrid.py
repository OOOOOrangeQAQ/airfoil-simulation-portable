from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


_FLOAT_TOKEN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"


def detect_invalid_volume_evidence(fluent_output: str) -> tuple[bool, bool, float | None]:
    """Detect actual invalid volumes without matching exponent/newline text.

    The old zero-plus-whitespace expression could join the final zero of a
    coordinate such as ``7.500000e+00`` to the next ``Volume statistics``
    heading.  Prefer Fluent's numeric minimum-volume report, augmented only by
    unambiguous diagnostic phrases.
    """
    match = re.search(rf"minimum\s+volume[^:\n]*:\s*({_FLOAT_TOKEN})", fluent_output, re.IGNORECASE)
    minimum = float(match.group(1)) if match else None
    explicit_negative = bool(re.search(r"\bnegative\s+(?:cell\s+)?volumes?\b", fluent_output, re.IGNORECASE))
    explicit_zero = bool(
        re.search(
            r"\bzero\s+(?:cell\s+)?volumes?\b|\bcells?\s+with\s+zero\s+volume\b",
            fluent_output,
            re.IGNORECASE,
        )
    )
    return explicit_negative or (minimum is not None and minimum < 0.0), explicit_zero or minimum == 0.0, minimum
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT))
from airfoil_fluentmeshing.fluent_runner import close_fluent_session, fluent_path_arg, fluent_product_version_arg, prepare_fluent_env
from airfoil_fluentmeshing.mesh_policy import MAX_CELLS


def _requires_path_bridge(*paths: Path | None) -> bool:
    return any(path is not None and not str(path).isascii() for path in paths)


def _bridge_root() -> Path:
    configured = os.environ.get("AIRFOIL_FLUENT_BRIDGE_ROOT")
    candidates = [
        Path(configured) if configured else None,
        Path(os.environ.get("PUBLIC") or os.environ.get("ProgramData") or r"C:\ProgramData") / "AirfoilFluentBridge",
        Path(os.environ.get("SystemDrive", "C:")) / "AirfoilFluentBridge",
    ]
    errors: list[str] = []
    for candidate in candidates:
        if candidate is None or not str(candidate).isascii():
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            return candidate
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError(
        "Fluent cannot read the non-ASCII project path and no writable ASCII bridge directory is available. "
        "Set AIRFOIL_FLUENT_BRIDGE_ROOT to a writable ASCII-only directory. " + "; ".join(errors)
    )


@contextmanager
def fluent_file_paths(mesh: Path, case: Path, tecplot: Path | None):
    transcript = case.with_suffix(".trn")
    if not _requires_path_bridge(mesh, case, transcript, tecplot):
        yield {"mesh": mesh, "case": case, "transcript": transcript, "tecplot": tecplot, "bridged": False}
        return
    root = _bridge_root()
    # A just-closed Fluent process can retain the transcript handle briefly.
    # Clear stale jobs opportunistically, but never let bridge cleanup override
    # a valid CFD result.
    for stale in root.glob("job_*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    bridge = Path(tempfile.mkdtemp(prefix="job_", dir=root))
    paths = {
        "mesh": bridge / "input.msh",
        "case": bridge / "output.cas.h5",
        "transcript": bridge / "output.cas.trn",
        "tecplot": bridge / "airfoil.plt" if tecplot else None,
        "bridged": True,
    }
    shutil.copy2(mesh, paths["mesh"])
    try:
        yield paths
    finally:
        for source, destination in (
            (paths["case"], case),
            (paths["transcript"], transcript),
            (paths["tecplot"], tecplot),
        ):
            if source is not None and destination is not None and source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        shutil.rmtree(bridge, ignore_errors=True)


def fluent_int(token: str) -> int:
    return int(token, 16)


def parse_msh_counts(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    counts: dict[str, object] = {}

    node_match = re.search(r"\(10\s+\(0\s+1\s+([0-9a-fA-F]+)\s+0\s+2\)\)", text)
    if node_match:
        counts["fluent_nodes"] = fluent_int(node_match.group(1))

    quad_cells = 0
    tri_cells = 0
    for cell_match in re.finditer(r"\(12\s+\(([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\)\)", text):
        zone = fluent_int(cell_match.group(1))
        if zone == 0:
            continue
        first = fluent_int(cell_match.group(2))
        last = fluent_int(cell_match.group(3))
        element_type = fluent_int(cell_match.group(5))
        n = last - first + 1
        if element_type == 3:
            quad_cells += n
        elif element_type == 1:
            tri_cells += n
    counts["fluent_quadrilateral_cells"] = quad_cells
    counts["fluent_triangular_cells"] = tri_cells

    zone_names: dict[int, str] = {}
    for zone_match in re.finditer(r"\(45\s+\(([0-9a-fA-F]+)\s+([^\s()]+)\s+([^\s()]+)\)\(\)\)", text):
        zone_names[fluent_int(zone_match.group(1))] = zone_match.group(2)

    face_counts: dict[str, int] = {}
    for face_match in re.finditer(r"\(13\s+\(([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\)", text):
        zone = fluent_int(face_match.group(1))
        if zone == 0:
            continue
        first = fluent_int(face_match.group(2))
        last = fluent_int(face_match.group(3))
        name = zone_names.get(zone)
        if name:
            face_counts[name] = last - first + 1
    for name, count in face_counts.items():
        counts[f"fluent_{name.replace('-', '_')}_faces"] = count
    has_velocity_inlet = "velocity-inlet" in zone_names.values()
    has_pressure_outlet = "pressure-outlet" in zone_names.values()
    has_pressure_far_field = "pressure-far-field" in zone_names.values()
    counts["has_velocity_inlet"] = has_velocity_inlet
    counts["has_pressure_outlet"] = has_pressure_outlet
    counts["has_pressure_far_field"] = has_pressure_far_field
    if has_velocity_inlet and has_pressure_outlet and not has_pressure_far_field:
        counts["external_boundary_topology"] = "velocity-inlet-pressure-outlet"
        counts["pressure_far_field_expected"] = False
    elif has_pressure_far_field:
        counts["external_boundary_topology"] = "pressure-far-field"
        counts["pressure_far_field_expected"] = True
    else:
        counts["external_boundary_topology"] = "unknown"
        counts["pressure_far_field_expected"] = None
    return counts


def parse_fluent_metric_location(label: str, text: str) -> dict[str, object] | None:
    pattern = (
        rf"{re.escape(label)}\s*=\s*([0-9.eE+-]+)\s+cell\s+([0-9]+).*?"
        rf"at location\s*\(\s*([0-9.eE+-]+)\s*,\s*([0-9.eE+-]+)"
    )
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return {
        "value": float(match.group(1)),
        "cell": int(match.group(2)),
        "x": float(match.group(3)),
        "y": float(match.group(4)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mesh", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--tecplot", type=Path, help="Optional airfoil-only Tecplot baseline export.")
    parser.add_argument("--fluent-exe", type=str, default=None)
    parser.add_argument("--product-version", type=str, default="auto")
    parser.add_argument("--processor-count", type=int, default=1)
    parser.add_argument("--minimum-orthogonal-quality", type=float, default=0.01)
    parser.add_argument("--target-orthogonal-quality", type=float, default=0.30)
    parser.add_argument("--maximum-skewness", type=float, default=0.98)
    parser.add_argument("--maximum-cells", type=int, default=MAX_CELLS)
    args = parser.parse_args()
    if args.maximum_cells <= 0:
        raise ValueError("--maximum-cells must be positive")

    transcript = args.case.with_suffix(".trn")
    env = prepare_fluent_env()
    os.environ.update(env)
    try:
        import ansys.fluent.core as pyfluent
    except Exception as exc:
        summary = {
            "status": "PYFLUENT_UNAVAILABLE",
            "mesh_path": str(args.mesh),
            "case_path": str(args.case),
            "error": f"{type(exc).__name__}: {exc}",
            **parse_msh_counts(args.mesh),
        }
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        transcript.write_text(f"IMPORT_PYFLUENT_FAILED: {summary['error']}\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return 1
    summary: dict[str, object] = {}
    with fluent_file_paths(args.mesh, args.case, args.tecplot) as fluent_paths:
        session = pyfluent.launch_fluent(
            dimension=2,
            precision="double",
            processor_count=args.processor_count,
            ui_mode="no_gui",
            additional_arguments="-g",
            start_timeout=180,
            cleanup_on_exit=True,
            **fluent_product_version_arg(args.product_version),
            **fluent_path_arg(args.fluent_exe),
        )
        try:
            solver = session
            solver.transcript.start(file_name=str(fluent_paths["transcript"]))
            console_buffer = io.StringIO()
            maximum_skewness_report = None
            maximum_skewness_error = None
            with redirect_stdout(console_buffer):
                solver.settings.file.read_mesh(file_name=str(fluent_paths["mesh"]))
                # Verbosity 3 exposes the zone, worst-cell position, squish and
                # expansion diagnostics needed to diagnose a rejected mesh.
                solver.tui.mesh.check_verbosity(3)
                solver.tui.mesh.check()
                solver.tui.mesh.quality()
                # Save the checked, uninitialized case first.  A temporary
                # initialization then activates Fluent volume reports so the
                # real cell-equiangle-skew maximum can be read back without
                # changing the delivered case file.
                solver.settings.file.write_case(file_name=str(fluent_paths["case"]))
                try:
                    solver.settings.solution.initialization.hybrid_initialize()
                    report_name = "mesh-skewness-check"
                    definitions = solver.settings.solution.report_definitions
                    definitions.volume.create(name=report_name)
                    report = definitions.volume[report_name]
                    report.report_type = "volume-max"
                    report.field = "cell-equiangle-skew"
                    report.cell_zones = ["fluid"]
                    computed = definitions.compute(report_defs=[report_name])
                    if computed and isinstance(computed[0], dict):
                        values = computed[0].get(report_name)
                        if isinstance(values, list) and values:
                            maximum_skewness_report = float(values[0])
                    definitions.volume.delete(name_list=[report_name])
                except Exception as exc:
                    maximum_skewness_error = f"{type(exc).__name__}: {exc}"
                if fluent_paths["tecplot"]:
                    solver.settings.file.export.tecplot(
                        file_name=str(fluent_paths["tecplot"]),
                        surfaces=["airfoil"],
                        cell_func_domain_export=[],
                    )
            console_text = console_buffer.getvalue()
            summary["case_path"] = str(args.case)
            summary["mesh_path"] = str(args.mesh)
            summary["fluent_version"] = str(solver.get_fluent_version())
            summary["path_bridge_used"] = bool(fluent_paths["bridged"])
            if args.tecplot:
                summary["tecplot_path"] = str(args.tecplot)
                summary["tecplot_surface_only"] = True
        finally:
            try:
                session.transcript.stop()
            except Exception:
                pass
            close_fluent_session(session)

    text = transcript.read_text(encoding="utf-8", errors="ignore") if transcript.exists() else ""
    fluent_output = "\n".join(part for part in [text, locals().get("console_text", "")] if part)
    summary["transcript_path"] = str(transcript)
    summary["transcript_excerpt"] = "\n".join(
        line for line in fluent_output.splitlines()
        if any(token in line.lower() for token in ["quadrilateral", "triangular", "velocity-inlet", "pressure-outlet", "wall", "orthogonal", "aspect"])
    )[-8000:]

    mesh_counts = parse_msh_counts(args.mesh)
    quad_match = re.search(r"([0-9]+)\s+quadrilateral", fluent_output, re.IGNORECASE)
    tri_match = re.search(r"([0-9]+)\s+triangular", fluent_output, re.IGNORECASE)
    oq_match = re.search(r"Minimum Orthogonal Quality\s*=\s*([0-9.eE+-]+)", fluent_output)
    ar_match = re.search(r"Maximum Aspect Ratio\s*=\s*([0-9.eE+-]+)", fluent_output)
    skew_match = re.search(r"Maximum(?: Cell)? Skewness\s*=\s*([0-9.eE+-]+)", fluent_output, re.IGNORECASE)
    squish_match = re.search(r"Maximum(?: Cell)? Squish(?: Index)?\s*=\s*([0-9.eE+-]+)", fluent_output, re.IGNORECASE)
    expansion_match = re.search(r"Minimum Expansion Ratio\s*=\s*([0-9.eE+-]+)", fluent_output, re.IGNORECASE)
    summary.update(mesh_counts)
    summary["fluent_quadrilateral_cells"] = int(quad_match.group(1)) if quad_match else mesh_counts.get("fluent_quadrilateral_cells")
    summary["fluent_triangular_cells"] = int(tri_match.group(1)) if tri_match else mesh_counts.get("fluent_triangular_cells")
    summary["minimum_orthogonal_quality"] = float(oq_match.group(1)) if oq_match else None
    summary["maximum_aspect_ratio"] = float(ar_match.group(1)) if ar_match else None
    summary["maximum_skewness"] = maximum_skewness_report if isinstance(locals().get("maximum_skewness_report"), (int, float)) else (float(skew_match.group(1)) if skew_match else None)
    summary["maximum_skewness_source"] = "Fluent cell-equiangle-skew volume maximum" if isinstance(summary["maximum_skewness"], (int, float)) else None
    summary["maximum_skewness_error"] = locals().get("maximum_skewness_error")
    summary["maximum_cell_squish"] = float(squish_match.group(1)) if squish_match else None
    summary["minimum_expansion_ratio"] = float(expansion_match.group(1)) if expansion_match else None
    summary["minimum_orthogonal_quality_cell"] = parse_fluent_metric_location("Minimum Orthogonal Quality", fluent_output)
    summary["maximum_aspect_ratio_cell"] = parse_fluent_metric_location("Maximum Aspect Ratio", fluent_output)
    invalid_volume, zero_volume, minimum_volume = detect_invalid_volume_evidence(fluent_output)
    summary["minimum_volume"] = minimum_volume
    degenerate = bool(re.search(r"\bdegenerate(?:d)?\s+(?:cell|face|element)", fluent_output, re.IGNORECASE))
    connectivity_error = bool(re.search(r"(?:invalid|bad|non-manifold)\s+(?:connectivity|cell|face|edge)", fluent_output, re.IGNORECASE))
    summary["negative_volume_detected"] = invalid_volume
    summary["zero_volume_detected"] = zero_volume
    summary["degenerate_cell_detected"] = degenerate
    summary["connectivity_error_detected"] = connectivity_error
    required_zones = bool(summary.get("has_velocity_inlet")) and bool(summary.get("has_pressure_outlet"))
    tecplot_ok = not args.tecplot or (args.tecplot.is_file() and args.tecplot.stat().st_size > 0)
    minimum_oq = summary.get("minimum_orthogonal_quality")
    hard_failures: list[str] = []
    warnings: list[str] = []
    if int(summary.get("fluent_quadrilateral_cells") or 0) <= 0:
        hard_failures.append("no_quadrilateral_cells")
    if int(summary.get("fluent_quadrilateral_cells") or 0) > args.maximum_cells:
        hard_failures.append("cell_count_above_hard_limit")
    if int(summary.get("fluent_triangular_cells") or 0) != 0:
        hard_failures.append("non_quadrilateral_cells")
    if not required_zones:
        hard_failures.append("required_boundary_zones_missing")
    if not isinstance(minimum_oq, (int, float)):
        hard_failures.append("orthogonal_quality_not_reported")
    elif float(minimum_oq) <= args.minimum_orthogonal_quality:
        hard_failures.append("orthogonal_quality_at_or_below_hard_limit")
    elif float(minimum_oq) < args.target_orthogonal_quality:
        warnings.append("orthogonal_quality_below_target")
    maximum_skewness = summary.get("maximum_skewness")
    if isinstance(maximum_skewness, (int, float)) and float(maximum_skewness) >= args.maximum_skewness:
        hard_failures.append("skewness_at_or_above_hard_limit")
    for failed, label in (
        (invalid_volume, "negative_volume"),
        (zero_volume, "zero_volume"),
        (degenerate, "degenerate_cell"),
        (connectivity_error, "connectivity_error"),
        (not tecplot_ok, "tecplot_export_failed"),
    ):
        if failed:
            hard_failures.append(label)
    summary["quality_policy"] = {
        "minimum_orthogonal_quality_hard": args.minimum_orthogonal_quality,
        "target_orthogonal_quality": args.target_orthogonal_quality,
        "maximum_skewness_hard": args.maximum_skewness,
        "maximum_cells_hard": args.maximum_cells,
    }
    summary["hard_failures"] = hard_failures
    summary["warnings"] = warnings
    summary["status"] = "FAIL" if hard_failures else ("WARNING" if warnings else "PASS")
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 1 if summary["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
