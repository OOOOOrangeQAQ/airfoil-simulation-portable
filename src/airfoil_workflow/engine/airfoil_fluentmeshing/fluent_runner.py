from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def close_fluent_session(session: object | None, *, timeout: int = 30) -> None:
    """Stop transcript capture and close only the run-owned Fluent process tree.

    Fluent 2025 R1 can acknowledge a graceful PyFluent exit while Cortex and
    Intel MPI descendants remain alive.  On Windows, use the connection's own
    host/Cortex PIDs to find the top of that Fluent-only ancestry and clean it
    after the graceful request.  No process-name-wide termination is used.
    """
    if session is None:
        return
    properties = getattr(session, "connection_properties", None)
    owned_pids = {
        pid
        for pid in (
            getattr(properties, "fluent_host_pid", None),
            getattr(properties, "cortex_pid", None),
        )
        if isinstance(pid, int) and pid > 0
    }
    owned_tree_pids = _windows_fluent_tree_processes(owned_pids) if os.name == "nt" else []
    cleanup_file: Path | None = None
    if os.name == "nt" and properties is not None:
        cortex_pwd = getattr(properties, "cortex_pwd", None)
        cortex_host = getattr(properties, "cortex_host", None)
        fluent_host_pid = getattr(properties, "fluent_host_pid", None)
        if cortex_pwd and cortex_host and isinstance(fluent_host_pid, int):
            cleanup_file = Path(str(cortex_pwd)) / f"cleanup-fluent-{cortex_host}-{fluent_host_pid}.bat"
    transcript = getattr(session, "transcript", None)
    if transcript is not None:
        with contextlib.suppress(Exception):
            transcript.stop()
    with contextlib.suppress(Exception):
        session.exit(timeout=timeout, timeout_force=False, wait=False)
    if os.name == "nt":
        for pid in owned_tree_pids:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=30,
                )
        if cleanup_file is not None:
            with contextlib.suppress(OSError):
                cleanup_file.unlink()


def _windows_fluent_tree_processes(seed_pids: set[int]) -> list[int]:
    """Return the pre-exit Fluent-only ancestry and descendants for a session."""
    if not seed_pids or os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return sorted(seed_pids)
    table: dict[int, tuple[int, str]] = {}
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)
    try:
        success = ctypes.windll.kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), str(entry.szExeFile).lower())
            success = ctypes.windll.kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        ctypes.windll.kernel32.CloseHandle(snapshot)

    fluent_processes = {
        "fluent.exe",
        "cx2510.exe",
        "fl_mpi2510.exe",
        "hydra_pmi_proxy.exe",
        "hydra_service.exe",
        "mpiexec.exe",
        "cmd.exe",
    }
    owned_ancestry: set[int] = set()
    for seed in seed_pids:
        current = seed
        while current in table and current not in owned_ancestry:
            parent, executable = table[current]
            if executable not in fluent_processes:
                break
            owned_ancestry.add(current)
            current = parent
    # Capture descendants before requesting exit.  Intel MPI helpers can be
    # re-parented while Fluent shuts down; retaining their exact pre-exit PIDs
    # lets the scoped cleanup finish them even after that re-parenting.
    owned_tree = set(owned_ancestry)
    changed = True
    while changed:
        changed = False
        for pid, (parent, executable) in table.items():
            if parent in owned_tree and executable in fluent_processes and pid not in owned_tree:
                owned_tree.add(pid)
                changed = True
    roots = sorted(pid for pid in owned_tree if table.get(pid, (0, ""))[0] not in owned_tree)
    descendants = sorted(owned_tree.difference(roots))
    return roots + descendants if owned_tree else sorted(seed_pids)


def fluent_path_arg(fluent_exe: str | Path | None) -> dict:
    if fluent_exe:
        return {"fluent_path": str(fluent_exe)}
    return {}


def fluent_product_version_arg(product_version: str | None) -> dict:
    if product_version is None:
        return {}
    normalized = str(product_version).strip()
    if not normalized or normalized.lower() in {"auto", "default", "latest"}:
        return {}
    return {"product_version": normalized}


def first_matching_env(env: dict[str, str], prefixes: tuple[str, ...]) -> str | None:
    keys = [key for key in env if any(key.startswith(prefix) for prefix in prefixes)]
    def rank(key: str) -> tuple[int, str]:
        match = re.search(r"(\d+)$", key)
        return (int(match.group(1)) if match else -1, key)
    for key in sorted(keys, key=rank, reverse=True):
        value = env.get(key)
        if value:
            return value
    return None


def path_key_for_env(env: dict[str, str]) -> str:
    if os.name == "nt":
        return "Path"
    return "PATH" if "PATH" in env else "Path"


def prepend_existing_paths(env: dict[str, str], paths: list[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if not existing:
        return
    path_key = path_key_for_env(env)
    current = env.get(path_key, "")
    current_parts = [part for part in current.split(os.pathsep) if part]
    normalized_seen = {os.path.normcase(os.path.abspath(part)) for part in existing}
    deduped_current = [part for part in current_parts if os.path.normcase(os.path.abspath(part)) not in normalized_seen]
    env[path_key] = os.pathsep.join(existing + deduped_current)


@dataclass(frozen=True)
class FluentMeshingResult:
    status: str
    case_path: str | None
    transcript_path: str
    message: str
    import_attempts: list[str] | None = None


def prepare_fluent_env() -> dict[str, str]:
    env = dict(os.environ)
    if os.name == "nt":
        env.setdefault("SystemRoot", os.environ.get("SystemRoot", r"C:\WINDOWS"))
        env.setdefault("WINDIR", os.environ.get("WINDIR", env["SystemRoot"]))
        env.setdefault("SystemDrive", os.environ.get("SystemDrive", Path(env["SystemRoot"]).drive or "C:"))
    ansys_root = first_matching_env(env, ("AWP_ROOT", "ANSYS_ROOT"))
    fluent_root = env.get("FLUENT_ROOT") or env.get("FLUENT_INC")
    if ansys_root and not fluent_root:
        fluent_root = str(Path(ansys_root) / "fluent")
    if fluent_root:
        env.setdefault("FLUENT_ROOT", fluent_root)
        env.setdefault("FLUENT_INC", fluent_root)
        fluent_root_path = Path(fluent_root)
        bin_candidates = [fluent_root_path / "bin"]
        if os.name == "nt":
            bin_candidates.insert(0, fluent_root_path / "ntbin" / "win64")
        prepend_existing_paths(env, bin_candidates)
    return env


def run_fluent_meshing_import_check(
    *,
    mesh_path: str | Path,
    nastran_mesh_path: str | Path | None = None,
    tecplot_mesh_path: str | Path | None = None,
    case_path: str | Path,
    transcript_path: str | Path,
    fluent_exe: str | Path | None = None,
    product_version: str | None = None,
    timeout: int = 180,
    dry_run: bool = False,
    enable_tecplot_fallback: bool = False,
    prefer_native_quad_import: bool = False,
) -> FluentMeshingResult:
    mesh = Path(mesh_path)
    nastran_mesh = Path(nastran_mesh_path) if nastran_mesh_path else None
    tecplot_mesh = Path(tecplot_mesh_path) if tecplot_mesh_path else None
    case = Path(case_path)
    transcript = Path(transcript_path)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        transcript.write_text("DRY_RUN: Fluent Meshing was not launched.\n", encoding="utf-8")
        return FluentMeshingResult("DRY_RUN", None, str(transcript), "Fluent Meshing not launched", [])
    os.environ.update(prepare_fluent_env())
    try:
        import ansys.fluent.core as pyfluent
    except Exception as exc:
        transcript.write_text(f"IMPORT_PYFLUENT_FAILED: {type(exc).__name__}: {exc}\n", encoding="utf-8")
        return FluentMeshingResult("PYFLUENT_UNAVAILABLE", None, str(transcript), str(exc), [])

    session = None
    solver = None
    log_lines: list[str] = []
    try:
        session = pyfluent.launch_fluent(
            dimension=2,
            precision="double",
            processor_count=1,
            mode="meshing",
            ui_mode="no_gui",
            start_timeout=timeout,
            cleanup_on_exit=True,
            cwd=str(mesh.parent),
            **fluent_product_version_arg(product_version),
            **fluent_path_arg(fluent_exe),
        )
        log_lines.append(f"Fluent version: {session.get_fluent_version()}")
        session.transcript.start(file_name=str(transcript))

        imported = False
        errors: list[str] = []
        if prefer_native_quad_import:
            imported = try_native_mesh_import(session, mesh, log_lines, errors)

        if not imported and nastran_mesh is not None:
            try:
                log_lines.append("Trying TUI file/import/nastran/bulkdata.")
                short_nastran = mesh.parent / "fluent_import_nastran.bdf"
                shutil.copy2(nastran_mesh, short_nastran)
                session.tui.file.import_.nastran.bulkdata(short_nastran.name)
                imported = True
            except Exception as exc:
                errors.append(f"Nastran bulkdata import failed: {type(exc).__name__}: {exc}")

        if not imported and not prefer_native_quad_import:
            imported = try_native_mesh_import(session, mesh, log_lines, errors)

        if not imported and tecplot_mesh is not None and enable_tecplot_fallback:
            try:
                log_lines.append("Trying TUI file/import/tecplot.")
                short_tecplot = mesh.parent / "fluent_import_tecplot.dat"
                shutil.copy2(tecplot_mesh, short_tecplot)
                session.tui.file.import_.tecplot.mesh(short_tecplot.name)
                imported = True
            except Exception as exc:
                errors.append(f"Tecplot import failed: {type(exc).__name__}: {exc}")
        elif not imported and tecplot_mesh is not None:
            errors.append("Tecplot fallback skipped by default because it can hang/crash Fluent Meshing 2025 R1 in 2D mode.")

        if not imported:
            transcript.write_text("\n".join(log_lines + errors), encoding="utf-8")
            return FluentMeshingResult("IMPORT_FAILED", None, str(transcript), " | ".join(errors), log_lines + errors)

        with contextlib.suppress(Exception):
            session.meshing.CheckMesh()
        with contextlib.suppress(Exception):
            session.execute_tui("/mesh/quality")

        wrote = False
        write_errors: list[str] = []
        for label, action in [
            ("meshing.File.WriteCase", lambda: session.meshing.File.WriteCase(FileName=str(case))),
            ("meshing.Write2dMesh", lambda: session.meshing.Write2dMesh(FileName=str(case), SkipExport=False)),
            ("meshing.File.WriteMesh", lambda: session.meshing.File.WriteMesh(FileName=str(case.with_suffix(".msh")))),
        ]:
            try:
                log_lines.append(f"Trying {label}")
                action()
                wrote = True
                break
            except Exception as exc:
                write_errors.append(f"{label}: {type(exc).__name__}: {exc}")
        if not wrote:
            try:
                log_lines.append("Trying transfer_mesh_to_solvers then solver write_case.")
                import ansys.fluent.core as pyfluent

                solver = pyfluent.launch_fluent(
                    dimension=2,
                    precision="double",
                    processor_count=1,
                    mode="solver",
                    ui_mode="no_gui",
                    start_timeout=timeout,
                    cleanup_on_exit=True,
                    cwd=str(case.parent),
                    **fluent_product_version_arg(product_version),
                    **fluent_path_arg(fluent_exe),
                )
                session.transfer_mesh_to_solvers(
                    [solver],
                    file_type="case",
                    file_name_stem=str(case.with_suffix("")),
                    num_files_to_try=1,
                    clean_up_mesh_file=True,
                    overwrite_previous=True,
                )
                with contextlib.suppress(Exception):
                    solver.tui.mesh.modify_zones.sep_face_zone_region("default_exterior-1")
                with contextlib.suppress(Exception):
                    solver.tui.mesh.modify_zones.zone_name("default_exterior-1", "airfoil")
                with contextlib.suppress(Exception):
                    solver.tui.mesh.modify_zones.zone_name("default_exterior-1:005", "farfield")
                with contextlib.suppress(Exception):
                    solver.settings.setup.boundary_conditions.set_zone_type(
                        zone_list=["farfield"], new_type="pressure-far-field"
                    )
                with contextlib.suppress(Exception):
                    solver.execute_tui("/mesh/check")
                with contextlib.suppress(Exception):
                    solver.execute_tui("/mesh/quality")
                try:
                    solver.settings.file.write_case(file_name=str(case))
                except Exception:
                    solver.execute_tui(f'/file/write-case "{case}"')
                wrote = True
            except Exception as exc:
                write_errors.append(f"switch_to_solver/write_case: {type(exc).__name__}: {exc}")
        if not wrote:
            transcript.write_text("\n".join(log_lines + errors + write_errors), encoding="utf-8")
            return FluentMeshingResult("WRITE_FAILED", None, str(transcript), " | ".join(write_errors), log_lines + errors + write_errors)
        actual_case = first_existing_case(case)
        if actual_case is None:
            alt = case.with_suffix(".msh")
            if alt.exists():
                return FluentMeshingResult("PASS_MESH_ONLY", str(alt), str(transcript), "Fluent wrote mesh but not case", log_lines + errors)
            return FluentMeshingResult("WRITE_OUTPUT_MISSING", None, str(transcript), "No Fluent output file found", log_lines + errors)
        return FluentMeshingResult("PASS", str(actual_case), str(transcript), "Fluent Meshing import/check/write completed", log_lines + errors)
    except Exception as exc:
        transcript.write_text("\n".join(log_lines + [f"FATAL: {type(exc).__name__}: {exc}"]), encoding="utf-8")
        return FluentMeshingResult("FLUENT_FAILED", None, str(transcript), str(exc), log_lines + errors)
    finally:
        if solver is not None:
            with contextlib.suppress(Exception):
                close_fluent_session(solver)
        if session is not None:
            with contextlib.suppress(Exception):
                session.transcript.stop()
            close_fluent_session(session)


def run_fluent_solver_native_import_check(
    *,
    mesh_path: str | Path,
    case_path: str | Path,
    transcript_path: str | Path,
    fluent_exe: str | Path | None = None,
    product_version: str | None = None,
    timeout: int = 180,
    dry_run: bool = False,
) -> FluentMeshingResult:
    mesh = Path(mesh_path)
    case = Path(case_path)
    transcript = Path(transcript_path)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    attempts: list[str] = ["Trying Fluent Solver native quad read_mesh."]
    if dry_run:
        transcript.write_text("DRY_RUN: Fluent Solver native import was not launched.\n", encoding="utf-8")
        return FluentMeshingResult("DRY_RUN", None, str(transcript), "Fluent Solver native import not launched", attempts)
    os.environ.update(prepare_fluent_env())
    try:
        import ansys.fluent.core as pyfluent
    except Exception as exc:
        transcript.write_text(f"IMPORT_PYFLUENT_FAILED: {type(exc).__name__}: {exc}\n", encoding="utf-8")
        return FluentMeshingResult("PYFLUENT_UNAVAILABLE", None, str(transcript), str(exc), attempts)

    solver = None
    try:
        solver = pyfluent.launch_fluent(
            dimension=2,
            precision="double",
            processor_count=1,
            mode="solver",
            ui_mode="no_gui",
            additional_arguments="-g",
            start_timeout=timeout,
            cleanup_on_exit=True,
            cwd=str(mesh.parent),
            **fluent_product_version_arg(product_version),
            **fluent_path_arg(fluent_exe),
        )
        attempts.append(f"Fluent version: {solver.get_fluent_version()}")
        solver.transcript.start(file_name=str(transcript))
        solver.settings.file.read_mesh(file_name=str(mesh))
        attempts.append("Solver settings.file.read_mesh succeeded.")
        with contextlib.suppress(Exception):
            solver.execute_tui("/mesh/check")
        with contextlib.suppress(Exception):
            solver.execute_tui("/mesh/quality")
        try:
            solver.settings.file.write_case(file_name=str(case))
        except Exception:
            solver.execute_tui(f'/file/write-case "{case}"')
        actual_case = first_existing_case(case)
        if actual_case is None:
            return FluentMeshingResult(
                "WRITE_OUTPUT_MISSING",
                None,
                str(transcript),
                "Solver native import read mesh but no case file was written",
                attempts,
            )
        return FluentMeshingResult(
            "PASS",
            str(actual_case),
            str(transcript),
            "Fluent Solver native quad import/check/write completed",
            attempts,
        )
    except Exception as exc:
        attempts.append(f"Solver native import failed: {type(exc).__name__}: {exc}")
        transcript.write_text("\n".join(attempts), encoding="utf-8")
        return FluentMeshingResult("SOLVER_NATIVE_IMPORT_FAILED", None, str(transcript), str(exc), attempts)
    finally:
        if solver is not None:
            with contextlib.suppress(Exception):
                solver.transcript.stop()
            close_fluent_session(solver)


def write_result(path: str | Path, result: FluentMeshingResult) -> None:
    Path(path).write_text(json.dumps(result.__dict__, indent=2), encoding="utf-8")


def try_native_mesh_import(session, mesh: Path, log_lines: list[str], errors: list[str]) -> bool:
    try:
        log_lines.append("Trying meshing.File.ReadMesh native quad mesh.")
        session.meshing.File.ReadMesh(FileName=str(mesh))
        return True
    except Exception as exc:
        errors.append(f"File.ReadMesh failed: {type(exc).__name__}: {exc}")

    try:
        log_lines.append("Trying meshing.ImportGeometry(FileFormat='Mesh') native quad mesh.")
        ok = session.meshing.ImportGeometry(FileFormat="Mesh", MeshFileName=str(mesh), MeshUnit="m", AppendMesh=False)
        log_lines.append(f"ImportGeometry returned: {ok}")
        return True
    except Exception as exc:
        errors.append(f"ImportGeometry failed: {type(exc).__name__}: {exc}")
    return False


def first_existing_case(case: Path) -> Path | None:
    candidates = [
        case,
        Path(str(case) + ".h5"),
        case.with_suffix(case.suffix + ".h5"),
        case.with_name(case.stem + "_0" + case.suffix + ".h5"),
        case.with_name(case.stem + "_0" + case.suffix),
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None
