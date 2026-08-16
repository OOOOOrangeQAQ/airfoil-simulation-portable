from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import airfoil_fluentmeshing.adjoint_optimizer as optimizer_module

from airfoil_fluentmeshing.adjoint_optimizer import (
    Coefficients,
    FluentAdjointRunner,
    anchor_displacement_audit,
    assess_candidate,
    cgrid_dry_run_summary,
    create_unique_run_dir,
    conservative_replication_metrics,
    force_stability_state,
    force_stability_drift,
    lift_tradeoff_screening_profiles,
    select_best_candidate,
    design_convergence_state,
    design_completion_state,
    ensight_case_has_variables,
    ensure_ensight_case_alias,
    export_failure_status,
    reconcile_stage1_gates,
    repair_branch_guidance,
    render_optimization_report,
    resolve_input_path,
    run_cgrid_mesh,
    optimization_attempt_profiles,
    optimizer_runtime_objective_audit,
    performance_target_state,
    resolve_objective_binding_strategy,
    resolve_rbf_numerics,
    resolve_shape_anchor_ranges,
    resolve_thickness_constraint,
    thickness_geometry_audit,
    transcript_morphing_audit,
    write_json,
)
from airfoil_fluentmeshing.fluent_runner import fluent_product_version_arg, prepare_fluent_env
from airfoil_fluentmeshing.geometry import read_dat
from airfoil_fluentmeshing.entry_modes import (
    apply_overrides,
    build_settings_catalog,
    diagnose_run,
    parse_override,
    render_configuration_diff,
    render_settings_catalog,
    render_candidate_catalog,
)
from airfoil_fluentmeshing.optimization_profiles import (
    DEFAULT_PROFILE_ID,
    validate_removed_envelope_config,
    apply_candidate_preset,
    build_optimization_profile,
    candidate_preset_ids,
    validate_aerodynamic_controls,
    validate_control_points,
)
from scripts.cgrid.fluent_check_cgrid import parse_msh_counts
from scripts.cgrid.generate_cgrid import Point, gentle_end_blend, quality_adaptive_outlet_y
from scripts.run_airfoil_adjoint_optimization import apply_morphing_policy_overrides, main as optimization_cli_main
from scripts.run_lift_tradeoff_experiment import _render_report as render_lift_tradeoff_report


class TranscriptAuditTests(unittest.TestCase):
    def test_negative_min_cell_volume_fails_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "fluent.txt"
            transcript.write_text(
                "min          cell-volume: limit    0.00000e+00; current value   -1.82925e-06.\n",
                encoding="utf-8",
            )

            audit = transcript_morphing_audit(transcript)

        self.assertEqual(audit["status"], "FAIL")
        self.assertTrue(audit["invalid_morphing"])
        self.assertEqual(audit["negative_cell_volume_count"], 1)

    def test_positive_min_cell_volume_passes_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "fluent.txt"
            transcript.write_text(
                "min          cell-volume: limit    0.00000e+00; current value    5.79030e-09.\n",
                encoding="utf-8",
            )

            audit = transcript_morphing_audit(transcript)

        self.assertEqual(audit["status"], "PASS")
        self.assertFalse(audit["invalid_morphing"])

    def test_isolated_attempt_transcripts_do_not_poison_later_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "attempt_0" / "transcript.txt"
            second = Path(tmp) / "attempt_1" / "transcript.txt"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("min cell-volume: limit 0; current value -1e-8.\n", encoding="utf-8")
            second.write_text("min cell-volume: limit 0; current value 2e-8.\n", encoding="utf-8")

            first_audit = transcript_morphing_audit(first)
            second_audit = transcript_morphing_audit(second)

        self.assertTrue(first_audit["invalid_morphing"])
        self.assertFalse(second_audit["invalid_morphing"])

    def test_audit_records_morphing_displacement_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "fluent.txt"
            transcript.write_text(
                "Maximum boundary displacement: 4.8e-2\n"
                "Average boundary displacement: 1.2e-3\n",
                encoding="utf-8",
            )

            audit = transcript_morphing_audit(transcript)

        self.assertEqual(audit["maximum_reported_boundary_displacement"], 4.8e-2)
        self.assertEqual(audit["maximum_reported_average_boundary_displacement"], 1.2e-3)

    def test_audit_records_final_recovery_and_zero_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "fluent.txt"
            transcript.write_text(
                "min cell-volume: limit 0; current value -1e-8. min orthogonal-quality: limit 0.18; current value 0.0.\n"
                "min cell-volume: limit 0; current value 2e-8. min orthogonal-quality: limit 0.18; current value 0.21.\n"
                "scale of the current step size is reduced to 0.\n",
                encoding="utf-8",
            )
            audit = transcript_morphing_audit(transcript)

        self.assertEqual(audit["last_reported_cell_volume"], 2e-8)
        self.assertEqual(audit["last_reported_orthogonal_quality"], 0.21)
        self.assertTrue(audit["step_reduced_to_zero"])


class SequentialOptimizationPolicyTests(unittest.TestCase):
    class ObjectiveRows:
        def __init__(self, state):
            self.state = state

        def get_state(self):
            return self.state

    class DefinitionCollection:
        class Definition:
            pass

        def __init__(self, names):
            self.objects = {name: self.Definition() for name in names}
            self.create_calls = []

        def get_object_names(self):
            return list(self.objects)

        def create(self, name):
            self.create_calls.append(name)
            self.objects[name] = self.Definition()

        def __getitem__(self, name):
            return self.objects[name]

    def test_fluent_observable_names_override_stale_configured_order(self) -> None:
        cfg = {"advanced_settings": {"optimizer": {"objective_row_order": ["cl", "cd"]}}}
        runner = FluentAdjointRunner(cfg, Path("unused"), {})
        rows = self.ObjectiveRows([{"observable": "cd"}, {"observable": "cl"}])

        self.assertEqual(runner._optimizer_objective_indices(rows), (0, 1))
        self.assertEqual(runner.commands[-1]["source"], "fluent_observable_state")

    def test_configured_order_is_only_used_when_fluent_has_no_names(self) -> None:
        cfg = {"advanced_settings": {"optimizer": {"objective_row_order": ["cl", "cd"]}}}
        runner = FluentAdjointRunner(cfg, Path("unused"), {})
        rows = self.ObjectiveRows([{}, {}])

        self.assertEqual(runner._optimizer_objective_indices(rows), (1, 0))
        self.assertEqual(runner.commands[-1]["source"], "configured_order_fallback")

    def test_partial_fluent_objective_names_fail_instead_of_guessing(self) -> None:
        cfg = {"advanced_settings": {"optimizer": {"objective_row_order": ["cd", "cl"]}}}
        runner = FluentAdjointRunner(cfg, Path("unused"), {})
        rows = self.ObjectiveRows([{"observable": "cd"}, {}])

        with self.assertRaisesRegex(RuntimeError, "unambiguously"):
            runner._optimizer_objective_indices(rows)

    def test_optimizer_objective_readback_accepts_correct_mapping(self) -> None:
        runner = FluentAdjointRunner({}, Path("unused"), {})
        runner.commands.append({"label": "map optimizer objective rows", "source": "fluent_observable_state"})
        rows = self.ObjectiveRows(
            [
                {"observable": "cd", "goal": "step-size", "value": -1.0e-4, "value_as_percentage": True},
                {
                    "observable": "cl",
                    "goal": "bounded",
                    "lower_bound": 310.0,
                    "tolerance": 0.02,
                    "tolerance_as_percentage": True,
                },
            ]
        )

        resolution = runner._verify_optimizer_objectives(rows, 0, 1, -1.0e-4, 310.0)

        self.assertTrue(resolution["verified"])
        self.assertEqual((resolution["cd_index"], resolution["cl_index"]), (0, 1))

    def test_optimizer_objective_readback_fails_before_morphing(self) -> None:
        runner = FluentAdjointRunner({}, Path("unused"), {})
        rows = self.ObjectiveRows(
            [
                {"observable": "cd", "goal": "bounded", "value": -1.0e-4, "value_as_percentage": True},
                {"observable": "cl", "goal": "step-size", "lower_bound": 310.0},
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "before morphing"):
            runner._verify_optimizer_objectives(rows, 0, 1, -1.0e-4, 310.0)

    def test_fluent_251_pyfluent_040_uses_proven_runtime_reverse_binding(self) -> None:
        resolution = resolve_objective_binding_strategy(
            "auto",
            fluent_version="Ansys Fluent 2025 R1",
            pyfluent_version="0.40.2",
        )

        self.assertEqual(resolution["resolved"], "fluent-251-runtime-reverse")
        self.assertTrue(resolution["compatibility_proven"])

    def test_unknown_version_uses_settings_mapping_but_requires_runtime_verification(self) -> None:
        resolution = resolve_objective_binding_strategy(
            "auto",
            fluent_version="Ansys Fluent 2026 R1",
            pyfluent_version="0.45.0",
        )

        self.assertEqual(resolution["resolved"], "settings-observable")
        self.assertTrue(resolution["runtime_verification_required"])

    def test_lift_runtime_bound_uses_999_of_original_baseline(self) -> None:
        runner = FluentAdjointRunner(
            {"advanced_settings": {"optimizer": {"lift_runtime_bound_ratio": 0.999}}},
            Path("unused"),
            {"baseline_lift_force_total_force": 310.0, "lift_force_report_to_observable_factor": 1.0},
        )

        self.assertAlmostEqual(runner._minimum_allowed_lift_force(0.5), 309.69)

    def test_restored_observable_is_reused_instead_of_duplicated(self) -> None:
        runner = FluentAdjointRunner({}, Path("unused"), {})
        definitions = self.DefinitionCollection(["cd"])

        runner._create_force_observable(definitions, "cd", [1.0, 0.0])

        self.assertEqual(definitions.create_calls, [])
        self.assertEqual(definitions["cd"].walls, ["airfoil"])
        self.assertEqual(definitions["cd"].vector, [1.0, 0.0])

    def test_reverse_binding_readback_verifies_written_rows_without_trusting_labels(self) -> None:
        runner = FluentAdjointRunner({}, Path("unused"), {})
        rows = self.ObjectiveRows(
            [
                {
                    "observable": "cd",
                    "goal": "bounded",
                    "lower_bound": 310.0,
                    "tolerance": 0.02,
                    "tolerance_as_percentage": True,
                },
                {"observable": "cl", "goal": "step-size", "value": -1.0e-6, "value_as_percentage": True},
            ]
        )

        resolution = runner._verify_optimizer_objectives(
            rows,
            0,
            1,
            -1.0e-6,
            310.0,
            write_cd_index=1,
            write_cl_index=0,
        )

        self.assertTrue(resolution["verified"])
        self.assertEqual(resolution["binding_indices"], {"cd": 1, "cl": 0})

    def test_reverse_binding_readback_supports_two_step_size_objectives(self) -> None:
        runner = FluentAdjointRunner({}, Path("unused"), {})
        rows = self.ObjectiveRows(
            [
                {"observable": "cd", "goal": "step-size", "value": 0.05, "value_as_percentage": True},
                {"observable": "cl", "goal": "step-size", "value": -0.15, "value_as_percentage": True},
            ]
        )

        resolution = runner._verify_optimizer_objectives(
            rows,
            0,
            1,
            -0.15,
            310.0,
            objective_strategy="coupled-drag-lift-step",
            lift_step=0.05,
            write_cd_index=1,
            write_cl_index=0,
        )

        self.assertTrue(resolution["verified"])
        self.assertEqual(resolution["requested_steps_percent"], {"cd": -0.15, "cl": 0.05})


class OptimizerRuntimeBindingAuditTests(unittest.TestCase):
    def test_runtime_audit_rejects_lift_bound_applied_to_cd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "wrong.txt"
            transcript.write_text(
                "  0 | 1 | 0 | cl | Y | N | 3.119e+02 | undef | - | -\n"
                "  0 | 2 | 0 | cd | Y | N | 6.762e+00 | undef | -3.035e+02 | N\n"
                "Unfeasible constraints detected.\n",
                encoding="utf-8",
            )

            audit = optimizer_runtime_objective_audit(transcript, lift_lower_bound=310.303)

        self.assertEqual(audit["status"], "FAIL")
        self.assertFalse(audit["verified"])
        self.assertTrue(any("Cd" in error for error in audit["errors"]))

    def test_runtime_audit_accepts_bounded_feasible_cl_and_unbounded_cd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "correct.txt"
            transcript.write_text(
                "  0 | 1 | 0 | cl | Y | Y | 3.119e+02 | -1.259e+00 | - | Y\n"
                "  0 | 2 | 0 | cd | Y | Y | 6.762e+00 | -6.762e-03 | - | -\n",
                encoding="utf-8",
            )

            audit = optimizer_runtime_objective_audit(transcript, lift_lower_bound=310.303)

        self.assertEqual(audit["status"], "PASS")
        self.assertTrue(audit["verified"])


class ControlledLiftTradeoffTests(unittest.TestCase):
    @staticmethod
    def _attempt(name: str, cd_gain: float, ld_gain: float, lift_ratio: float = 0.999) -> dict:
        return {
            "profile": {"name": name},
            "candidate_gate": {
                "accepted": True,
                "relative_drag_improvement_from_previous": cd_gain,
                "relative_lift_to_drag_improvement_from_previous": ld_gain,
                "lift_ratio_to_original": lift_ratio,
            },
        }

    def test_screening_matrix_is_predeclared_eight_points(self) -> None:
        profiles = lift_tradeoff_screening_profiles()

        self.assertEqual([item["name"] for item in profiles], ["B1", "B2", "B3", "B4", "C1", "C2", "C3", "C4"])
        self.assertEqual([item["lift_runtime_bound_ratio"] for item in profiles[:4]], [0.9994, 0.9995, 0.9997, 1.0])
        self.assertTrue(all(item["lift_bound_tolerance_percent"] == 0.02 for item in profiles))
        self.assertEqual(
            [(item["drag_step_percent"], item["lift_step_percent"]) for item in profiles[4:]],
            [(-0.10, 0.01), (-0.10, 0.03), (-0.05, 0.01), (-0.05, 0.03)],
        )

    def test_direct_cycle_entry_records_pyfluent_version_for_binding(self) -> None:
        runner = FluentAdjointRunner({}, Path("unused"), {})

        result = runner._run_cycle_attempts(
            type("PyFluent", (), {"__version__": "0.40.2"})(),
            2,
            [],
            Coefficients(0.01, 0.5, "original"),
            Coefficients(0.0099, 0.5, "previous"),
            Path("case.cas.h5"),
            Path("data.dat.h5"),
        )

        self.assertEqual(result["attempts"], [])
        self.assertEqual(runner.runtime_resolution["versions"]["pyfluent"], "0.40.2")

    def test_best_of_cycle_uses_ld_inside_cd_tie_band(self) -> None:
        lower_cd_better_ld = self._attempt("A", 0.00020, 0.0010)
        slightly_more_cd = self._attempt("B", 0.00025, 0.0009)
        clearly_more_cd = self._attempt("C", 0.00040, 0.0008)

        self.assertIs(select_best_candidate([lower_cd_better_ld, slightly_more_cd]), lower_cd_better_ld)
        self.assertIs(select_best_candidate([lower_cd_better_ld, slightly_more_cd, clearly_more_cd]), clearly_more_cd)

    def test_replication_uses_worst_metrics_and_requires_both_passes(self) -> None:
        first = self._attempt("B2", 0.00030, 0.00020, 0.9996)
        repeated = self._attempt("B2-repeat", 0.00025, 0.00018, 0.9995)

        result = conservative_replication_metrics(first, repeated)

        self.assertTrue(result["stable"])
        self.assertAlmostEqual(result["conservative"]["cd_reduction_percentage_points"], 0.025)
        self.assertAlmostEqual(result["conservative"]["ld_improvement_percentage_points"], 0.018)
        self.assertAlmostEqual(result["conservative"]["lift_retention_percentage"], 99.95)
        repeated["candidate_gate"]["accepted"] = False
        self.assertFalse(conservative_replication_metrics(first, repeated)["stable"])

    def test_force_stability_requires_two_consecutive_blocks(self) -> None:
        samples = [
            {"cd": 0.0100000, "cl": 0.5000000},
            {"cd": 0.0100001, "cl": 0.5000030},
            {"cd": 0.01000011, "cl": 0.5000031},
            {"cd": 0.01000012, "cl": 0.5000032},
        ]

        state = force_stability_state(samples, relative_tolerance=5.0e-6, consecutive_blocks=2)

        self.assertTrue(state["stable"])
        self.assertEqual(state["status"], "PASS")

    def test_force_stability_forces_actual_iterations_and_restores_convergence_checks(self) -> None:
        class BoolSetting:
            def __init__(self, value: bool):
                self.value = value

            def __call__(self):
                return self.value

        class Equation:
            def __init__(self):
                self._check = BoolSetting(True)

            @property
            def check_convergence(self):
                return self._check

            @check_convergence.setter
            def check_convergence(self, value):
                self._check.value = bool(value)

        class Equations(dict):
            def keys(self):
                return super().keys()

        equations = Equations({"continuity": Equation(), "x-velocity": Equation()})

        class SchemeEval:
            iteration = 122

            def scheme_eval(self, expression):
                self.expression = expression
                return self.iteration

        scheme_eval = SchemeEval()

        class RunCalculation:
            calls = []

            def iterate(self, *, iter_count):
                self.calls.append(iter_count)
                if any(equations[name].check_convergence() for name in equations):
                    scheme_eval.iteration += 1
                else:
                    scheme_eval.iteration += iter_count

        solution = type(
            "Solution",
            (),
            {
                "monitor": type("Monitor", (), {"residual": type("Residual", (), {"equations": equations})()})(),
                "run_calculation": RunCalculation(),
            },
        )()
        solver = type("Solver", (), {"settings": type("Settings", (), {"solution": solution})(), "scheme_eval": scheme_eval})()
        runner = FluentAdjointRunner(
            {
                "optimization_run": {
                    "force_stability": {
                        "enabled": True,
                        "iterations_per_block": 10,
                        "max_iterations": 30,
                        "relative_tolerance": 5.0e-6,
                        "consecutive_blocks": 2,
                    }
                }
            },
            Path("unused"),
            {},
        )
        values = iter(
            [
                Coefficients(0.01000000, 0.50000000, "initial"),
                Coefficients(0.01000001, 0.50000001, "block-1"),
                Coefficients(0.01000002, 0.50000002, "block-2"),
            ]
        )
        runner._compute_coefficients = lambda *_args: next(values)

        audit, final = runner._stabilize_force_coefficients(solver, "test")

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["actual_iterations_completed"], 20)
        self.assertEqual([sample["actual_iterations"] for sample in audit["samples"]], [0, 10, 20])
        self.assertEqual(solution.run_calculation.calls, [10, 10])
        self.assertTrue(all(equations[name].check_convergence() for name in equations))
        self.assertEqual(final.source, "test_force_stability_tail_mean")
        self.assertAlmostEqual(final.cd, 0.01000001)

    def test_attempt_profile_overrides_lift_bound_and_tolerance(self) -> None:
        runner = FluentAdjointRunner(
            {"advanced_settings": {"optimizer": {"lift_runtime_bound_ratio": 0.999, "lift_bound_tolerance_percent": 2.0}}},
            Path("unused"),
            {"baseline_lift_force_total_force": 310.0},
        )
        profile = {
            "objective_strategy": "drag-with-lift-bound",
            "lift_runtime_bound_ratio": 0.9995,
            "lift_bound_tolerance_percent": 0.02,
        }

        runner._activate_profile(profile, Coefficients(0.01, 0.5, "test"))

        self.assertAlmostEqual(runner.context["lift_runtime_bound_ratio"], 0.9995)
        self.assertAlmostEqual(runner.context["lift_bound_tolerance_percent"], 0.02)
        self.assertAlmostEqual(runner.context["minimum_allowed_lift_force"], 309.845)

    def test_bound_readback_rejects_fluent_default_two_percent(self) -> None:
        runner = FluentAdjointRunner({}, Path("unused"), {})
        rows = SequentialOptimizationPolicyTests.ObjectiveRows(
            [
                {"observable": "cd", "goal": "step-size", "value": -0.1, "value_as_percentage": True},
                {
                    "observable": "cl",
                    "goal": "bounded",
                    "lower_bound": 310.0,
                    "tolerance": 2.0,
                    "tolerance_as_percentage": True,
                },
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "bound tolerance"):
            runner._verify_optimizer_objectives(rows, 0, 1, -0.1, 310.0, lift_bound_tolerance_percent=0.02)

    def test_experiment_report_handles_route_without_winner(self) -> None:
        report = render_lift_tradeoff_report(
            {
                "status": "NO_STABLE_ROUTE",
                "source_checkpoint": {"case": "a.cas.h5", "case_sha256": "a", "data_sha256": "b"},
                "route_results": [
                    {"route": "lift-bound", "profile": {}, "replication": {"stable": False, "conservative": None}}
                ],
                "selected_route": None,
                "continuation": None,
                "eligible_for_default": False,
            }
        )

        self.assertIn("lift-bound / NONE", report)
        self.assertIn("NO_STABLE_ROUTE", report)

    def test_runtime_audit_accepts_coupled_drag_down_and_lift_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "coupled.txt"
            transcript.write_text(
                "  0 | 1 | 0 | cl | Y | Y | 3.119e+02 | 1.5595e-01 | - | -\n"
                "  0 | 2 | 0 | cd | Y | Y | 6.762e+00 | -1.0143e-02 | - | -\n",
                encoding="utf-8",
            )

            audit = optimizer_runtime_objective_audit(
                transcript,
                objective_strategy="coupled-drag-lift-step",
            )

        self.assertEqual(audit["status"], "PASS")
        self.assertGreater(float(audit["settings_summary_rows"]["cl"]["expected_change"]), 0.0)

    def test_rbf_constraint_solver_defaults_are_tighter_than_fluent_defaults(self) -> None:
        numerics = resolve_rbf_numerics({})

        self.assertEqual(numerics["max_iterations"], 10)
        self.assertEqual(numerics["max_subiteration"], 100)
        self.assertLess(numerics["linear_solver_tolerance"], 3.0e-3)

    def test_rbf_constraint_solver_rejects_nonpositive_values(self) -> None:
        with self.assertRaises(ValueError):
            resolve_rbf_numerics({"max_subiteration": 0})


class ShapeAnchorTests(unittest.TestCase):
    @staticmethod
    def geometry(points):
        return {
            "points": points,
            "metrics": {"xmin": 0.0, "xmax": 1.0, "chord": 1.0},
        }

    def test_anchor_ranges_fix_only_exact_edge_vertices(self) -> None:
        geometry = self.geometry([[0.0, 0.0], [0.003, 0.01], [0.5, 0.1], [0.997, 0.01], [1.0, 0.0]])

        resolution = resolve_shape_anchor_ranges(
            geometry,
            {"enabled": True, "mode": "endpoints-only"},
        )

        self.assertEqual(resolution["status"], "PASS")
        self.assertEqual(resolution["leading_edge_vertex_count"], 1)
        self.assertEqual(resolution["trailing_edge_vertex_count"], 1)

    def test_empty_anchor_fails_closed(self) -> None:
        geometry = self.geometry([[0.1, 0.0], [0.5, 0.1], [0.9, 0.0]])

        with self.assertRaisesRegex(RuntimeError, "contains no baseline vertices"):
            resolve_shape_anchor_ranges(geometry, {"enabled": True})

    def test_anchor_displacement_gate(self) -> None:
        baseline = self.geometry([[0.0, 0.0], [0.003, 0.01], [0.5, 0.1], [0.997, 0.01], [1.0, 0.0]])
        resolution = resolve_shape_anchor_ranges(baseline, {"enabled": True, "max_anchor_displacement_over_chord": 1.0e-5})
        candidate = self.geometry([[0.0, 0.0], [0.003, 0.01], [0.5, 0.2], [0.997, 0.01], [1.0 + 2.0e-5, 0.0]])

        audit = anchor_displacement_audit(baseline, candidate, resolution)

        self.assertEqual(audit["status"], "FAIL")
        self.assertGreater(audit["maximum_displacement_over_chord"], 1.0e-5)

    @staticmethod
    def thickness_geometry(upper_mid: float, lower_mid: float):
        points = [[1.0, 0.0], [0.5, upper_mid], [0.0, 0.0], [0.5, lower_mid]]
        return {
            "status": "PASS",
            "points": points,
            "metrics": {"xmin": 0.0, "xmax": 1.0, "chord": 1.0, "maximum_thickness": upper_mid - lower_mid},
        }

    def test_geometry_thickness_gate_accepts_wall_outside_inner_keepout(self) -> None:
        baseline = self.thickness_geometry(0.1, -0.1)
        candidate = self.thickness_geometry(0.095, -0.095)
        candidate["metrics"]["maximum_thickness"] = 0.19

        audit = thickness_geometry_audit(baseline, candidate, margin_percent=5.0)

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["violation_count"], 0)
        self.assertAlmostEqual(audit["clearance"], 0.01)

    def test_geometry_thickness_gate_rejects_local_thinning_violation(self) -> None:
        baseline = self.thickness_geometry(0.1, -0.1)
        candidate = self.thickness_geometry(0.075, -0.095)

        audit = thickness_geometry_audit(baseline, candidate, margin_percent=5.0)

        self.assertEqual(audit["status"], "FAIL")
        self.assertGreater(audit["violation_count"], 0)
        self.assertIn("candidate_violates_baseline_thickness_limit", audit["errors"])

    def test_thickness_constraint_is_direct_geometry_only(self) -> None:
        self.assertEqual(resolve_thickness_constraint({"enabled": True})["audit"], "direct-section-geometry")
        with self.assertRaises(ValueError):
            resolve_thickness_constraint({"mode": "fluent-envelope"})

    def test_profiles_append_finite_repair_ladder_after_standard_profiles(self) -> None:
        cfg = {
            "optimizer": {"design_iterations": 1},
            "advanced_settings": {"optimizer": {"drag_step_percent": -1e-4}},
            "retry_profiles": [{"name": "standard_retry", "drag_step_percent": -5e-5}],
            "optimization_run": {
                "repair_on_profile_exhaustion": True,
                "repair_profiles": [{"name": "repair", "drag_step_percent": -1e-6}],
            },
        }

        profiles = optimization_attempt_profiles(cfg)

        self.assertEqual([profile["tier"] for profile in profiles], ["standard", "standard", "repair"])
        self.assertEqual(profiles[-1]["drag_step_percent"], -1e-6)

    def test_strict_clean_policy_disables_recovery_and_uses_auto_quality(self) -> None:
        cfg = {
            "optimization_run": {"accept_recovered_attempts": True},
            "advanced_settings": {"optimizer": {"min_orthogonal_quality": "fluent-default"}},
        }

        apply_morphing_policy_overrides(
            cfg,
            accept_recovered_attempts=False,
            strict_clean_morphing=True,
        )

        self.assertFalse(cfg["optimization_run"]["accept_recovered_attempts"])
        self.assertTrue(cfg["optimization_run"]["strict_clean_morphing"])
        self.assertEqual(cfg["advanced_settings"]["optimizer"]["min_orthogonal_quality"], "auto")

    def test_strict_clean_and_recovery_flags_conflict(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts"):
            apply_morphing_policy_overrides(
                {},
                accept_recovered_attempts=True,
                strict_clean_morphing=True,
            )

    def test_recovered_candidate_is_accepted_only_when_final_evidence_passes(self) -> None:
        gate = assess_candidate(
            Coefficients(0.0100, 0.50, "original"),
            Coefficients(0.0100, 0.50, "previous"),
            Coefficients(0.0099, 0.499, "final"),
            {"invalid_morphing": True, "step_reduced_to_zero": False, "last_reported_cell_volume": 2e-8},
            {"status": "PASS", "minimum_orthogonal_quality": 0.20, "last_reported_cell_volume": 2e-8},
            lift_tolerance=0.005,
            accept_recovered=True,
            minimum_relative_drag_improvement=1e-8,
            required_orthogonal_quality=0.18,
        )

        self.assertEqual(gate["status"], "RECOVERED_PASS")
        self.assertTrue(gate["accepted"])

    def test_zero_step_recovery_is_rejected(self) -> None:
        gate = assess_candidate(
            Coefficients(0.0100, 0.50, "original"),
            Coefficients(0.0100, 0.50, "previous"),
            Coefficients(0.0099, 0.499, "final"),
            {"invalid_morphing": True, "step_reduced_to_zero": True, "last_reported_cell_volume": 2e-8},
            {"status": "PASS", "minimum_orthogonal_quality": 0.20, "last_reported_cell_volume": 2e-8},
            lift_tolerance=0.005,
            accept_recovered=True,
            minimum_relative_drag_improvement=1e-8,
            required_orthogonal_quality=0.18,
        )

        self.assertFalse(gate["accepted"])
        self.assertIn("fluent_step_reduced_to_zero", gate["reasons"])

    def test_strict_clean_policy_rejects_negative_history_after_valid_recovery(self) -> None:
        gate = assess_candidate(
            Coefficients(0.0100, 0.50, "original"),
            Coefficients(0.0100, 0.50, "previous"),
            Coefficients(0.0099, 0.50, "final"),
            {"invalid_morphing": True, "step_reduced_to_zero": False, "last_reported_cell_volume": 2e-8},
            {"status": "PASS", "minimum_orthogonal_quality": 0.20, "last_reported_cell_volume": 2e-8},
            lift_tolerance=0.005,
            accept_recovered=False,
            minimum_relative_drag_improvement=1e-8,
            required_orthogonal_quality=0.18,
        )

        self.assertFalse(gate["accepted"])
        self.assertIn("negative_volume_history_not_allowed", gate["reasons"])

    def test_design_tool_unreferenced_faces_do_not_replace_required_boundaries(self) -> None:
        gate = assess_candidate(
            Coefficients(0.0100, 0.50, "original"),
            Coefficients(0.0100, 0.50, "previous"),
            Coefficients(0.0099, 0.51, "final"),
            {"invalid_morphing": False, "step_reduced_to_zero": False},
            {
                "status": "PASS",
                "minimum_orthogonal_quality": 0.20,
                "negative_volume_in_validation": False,
                "unreferenced_faces": 5,
            },
            lift_tolerance=0.005,
            accept_recovered=True,
            minimum_relative_drag_improvement=1e-8,
            required_orthogonal_quality=0.18,
        )
        self.assertTrue(gate["accepted"])

    def test_two_small_improvements_trigger_convergence(self) -> None:
        state = design_convergence_state([0.002, 0.0004, 0.0003], 0.0005, 2)
        self.assertTrue(state["converged"])

    def test_candidate_accepts_exact_998_lift_ratio_when_ld_improves(self) -> None:
        gate = assess_candidate(
            Coefficients(0.0100, 0.500, "original"),
            Coefficients(0.0100, 0.500, "previous"),
            Coefficients(0.0099, 0.499, "final"),
            {"invalid_morphing": False, "step_reduced_to_zero": False, "last_reported_cell_volume": 2e-8},
            {"status": "PASS", "minimum_orthogonal_quality": 0.20, "last_reported_cell_volume": 2e-8},
            lift_tolerance=0.002,
            minimum_lift_ratio=0.998,
            require_lift_to_drag_improvement=True,
            accept_recovered=False,
            minimum_relative_drag_improvement=1e-8,
            required_orthogonal_quality=0.18,
        )

        self.assertTrue(gate["accepted"])
        self.assertAlmostEqual(gate["lift_ratio_to_original"], 0.998)

    def test_candidate_rejects_ld_regression_even_at_lift_boundary(self) -> None:
        gate = assess_candidate(
            Coefficients(0.0100, 0.500, "original"),
            Coefficients(0.0100, 0.500, "previous"),
            Coefficients(0.00999, 0.499, "final"),
            {"invalid_morphing": False, "step_reduced_to_zero": False, "last_reported_cell_volume": 2e-8},
            {"status": "PASS", "minimum_orthogonal_quality": 0.20, "last_reported_cell_volume": 2e-8},
            lift_tolerance=0.002,
            minimum_lift_ratio=0.998,
            require_lift_to_drag_improvement=True,
            accept_recovered=False,
            minimum_relative_drag_improvement=1e-8,
            required_orthogonal_quality=0.18,
        )

        self.assertEqual(gate["status"], "FAIL_LD_GATE")
        self.assertIn("lift_to_drag_not_improved", gate["reasons"])

    def test_performance_target_requires_all_three_cumulative_gates(self) -> None:
        state = performance_target_state(
            Coefficients(0.0100, 0.500, "original"),
            Coefficients(0.00995, 0.499, "final"),
            minimum_cumulative_cd_reduction=0.005,
            minimum_cumulative_ld_improvement=0.002,
            minimum_lift_ratio=0.998,
        )

        self.assertTrue(state["achieved"])
        self.assertTrue(all(state["gates"].values()))

    def test_ordinary_convergence_cannot_pass_before_performance_target(self) -> None:
        performance = performance_target_state(
            Coefficients(0.0100, 0.500, "original"),
            Coefficients(0.00996, 0.4995, "final"),
            minimum_cumulative_cd_reduction=0.005,
            minimum_cumulative_ld_improvement=0.002,
            minimum_lift_ratio=0.998,
        )
        completion = design_completion_state(
            performance_targets_enabled=True,
            performance=performance,
            convergence={"converged": True},
            accepted_step_limit_reached=False,
        )

        self.assertEqual(completion["status"], "INCOMPLETE_PERFORMANCE_TARGET")
        self.assertEqual(completion["action"], "STOP")

    def test_chinese_report_contains_baseline_and_final(self) -> None:
        report = render_optimization_report(
            {
                "status": "PASS",
                "run_dir": "run",
                "adjoint_result": {
                    "baseline": {"cd": 0.01, "cl": 0.5},
                    "final": {"cd": 0.009, "cl": 0.499},
                    "accepted_steps": [],
                    "attempts": [],
                },
            }
        )
        self.assertIn("升阻力对比", report)
        self.assertIn("0.009", report)


class EntryModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "flow": {"angle_of_attack_deg": 2.0, "velocity_m_s": 32.5},
            "iterations": {"flow": 1200},
            "advanced_settings": {"residual_criteria": {"continuity": 1e-5}},
            "retry_profiles": [],
        }

    def test_catalog_lists_all_major_advanced_groups(self) -> None:
        records = build_settings_catalog(self.cfg)
        paths = {record.path for record in records}
        rendered = render_settings_catalog(records)

        self.assertIn("flow.angle_of_attack_deg", paths)
        self.assertIn("iterations.flow", paths)
        self.assertIn("advanced_settings.residual_criteria.continuity", paths)
        self.assertIn("残差准则", rendered)

    def test_path_override_is_typed_and_diffed_without_touching_template(self) -> None:
        defaults = json.loads(json.dumps(self.cfg))
        allowed = {record.path for record in build_settings_catalog(self.cfg)}

        changes, selected = apply_overrides(self.cfg, ["iterations.flow=800"], allowed_paths=allowed)

        self.assertEqual(self.cfg["iterations"]["flow"], 800)
        self.assertEqual(defaults["iterations"]["flow"], 1200)
        self.assertEqual(selected, ["iterations.flow"])
        self.assertIn("1200 -> 800", render_configuration_diff(changes))

    def test_unknown_setting_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_overrides(self.cfg, ["unknown.path=1"], allowed_paths={"iterations.flow"})
        self.assertEqual(parse_override("flow.angle_of_attack_deg=3.5"), ("flow.angle_of_attack_deg", 3.5))

    def test_out_of_range_setting_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_overrides(
                self.cfg,
                ["flow.velocity_m_s=-1"],
                allowed_paths={"flow.velocity_m_s"},
            )

    def test_fluent_default_optimizer_quality_and_morpher_are_valid_overrides(self) -> None:
        cfg = {
            "advanced_settings": {
                "optimizer": {"min_orthogonal_quality": "auto"},
                "design_tool": {"morpher": {"method": "polynomials"}},
            }
        }
        allowed = {record.path for record in build_settings_catalog(cfg)}

        apply_overrides(
            cfg,
            [
                'advanced_settings.optimizer.min_orthogonal_quality="fluent-default"',
                'advanced_settings.design_tool.morpher.method="fluent-default"',
            ],
            allowed_paths=allowed,
        )

        self.assertEqual(cfg["advanced_settings"]["optimizer"]["min_orthogonal_quality"], "fluent-default")
        self.assertEqual(cfg["advanced_settings"]["design_tool"]["morpher"]["method"], "fluent-default")

    def test_debug_mode_is_read_only_and_requires_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "outputs" / "run_20260801_adjoint_optimization"
            run.mkdir(parents=True)
            (run / "optimization_summary.json").write_text(
                json.dumps(
                    {
                        "status": "FAIL_TRANSCRIPT_NEGATIVE_VOLUME",
                        "primary_mesh": {"minimum_orthogonal_quality": 0.25, "maximum_aspect_ratio": 49000},
                        "adjoint_result": {"transcript_audit": {"invalid_morphing": True}},
                    }
                ),
                encoding="utf-8",
            )

            report = diagnose_run(root, run)

        self.assertTrue(report["read_only"])
        self.assertTrue(report["authorization_required_for_changes"])
        self.assertTrue(any("负体积" in cause for cause in report["causes"]))

    def test_debug_pass_uses_clean_accepted_audit_without_hiding_failed_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "outputs" / "run_pass_adjoint_optimization"
            run.mkdir(parents=True)
            (run / "optimization_summary.json").write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "primary_mesh": {"minimum_orthogonal_quality": 0.31, "maximum_aspect_ratio": 800},
                        "adjoint_result": {
                            "attempts": [
                                {"index": 0, "status": "FAIL", "transcript_audit": {"invalid_morphing": True}},
                                {"index": 1, "status": "CLEAN_PASS", "transcript_audit": {"invalid_morphing": False}},
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = diagnose_run(root, run)

        self.assertFalse(report["transcript_audit"]["invalid_morphing"])
        self.assertEqual(report["invalid_attempt_count"], 1)
        self.assertIn("最终采用尝试无负体积", report["causes"][0])

    def test_recommended_default_profile_matches_template(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = json.loads((root / "src" / "airfoil_workflow" / "engine" / "config" / "adjoint_optimization.example.json").read_text(encoding="utf-8"))

        profile = build_optimization_profile(cfg)
        profiles = optimization_attempt_profiles(cfg)

        self.assertEqual(profile["id"], DEFAULT_PROFILE_ID)
        self.assertTrue(profile["matches_recommended_default"])
        self.assertEqual(profile["aerodynamic_controls"]["drag_step_percent"], -0.15)
        self.assertEqual(profile["aerodynamic_controls"]["lift_runtime_bound_percent"], 99.9)
        self.assertEqual(profile["aerodynamic_controls"]["minimum_lift_percent"], 99.8)
        self.assertEqual([item["drag_step_percent"] for item in profiles], [-0.15, -0.10, -0.05, -0.02, -0.01])

    def test_aerodynamic_controls_validate_order_and_warn_on_small_buffer(self) -> None:
        cfg = {
            "advanced_settings": {"optimizer": {"drag_step_percent": -0.15, "lift_runtime_bound_ratio": 0.9985}},
            "completion": {"minimum_lift_ratio": 0.998},
        }
        warnings = validate_aerodynamic_controls(cfg)
        self.assertTrue(any("0.1" in warning for warning in warnings))
        cfg["advanced_settings"]["optimizer"]["lift_runtime_bound_ratio"] = 0.997
        with self.assertRaises(ValueError):
            validate_aerodynamic_controls(cfg)

    def test_candidate_catalog_is_explicit_and_control_point_preset_uses_advanced_source(self) -> None:
        cfg = {
            "advanced_settings": {
                "optimizer": {"drag_step_percent": -0.15, "lift_runtime_bound_ratio": 0.999},
                "design_tool": {"x_control_points": 24, "y_control_points": 8},
            },
            "completion": {"minimum_lift_ratio": 0.998},
            "optimizer": {"x_control_points": 24, "y_control_points": 8},
            "retry_profiles": [{"name": "retry", "drag_step_percent": -0.1}],
        }

        apply_candidate_preset(cfg, "control-points-32x8")
        allowed = {record.path for record in build_settings_catalog(cfg)}
        apply_overrides(
            cfg,
            [
                "advanced_settings.design_tool.x_control_points=24",
                "advanced_settings.design_tool.y_control_points=12",
            ],
            allowed_paths=allowed,
        )
        profiles = optimization_attempt_profiles(cfg)

        self.assertIn("coupled-c4", candidate_preset_ids())
        self.assertIn("control-points-32x8", candidate_preset_ids())
        self.assertIn("unverified-hypothesis", render_candidate_catalog())
        self.assertEqual(cfg["advanced_settings"]["design_tool"]["x_control_points"], 24)
        self.assertEqual(profiles[0]["x_control_points"], 24)
        self.assertEqual(profiles[0]["y_control_points"], 12)
        self.assertEqual(cfg["retry_profiles"], [])

    def test_control_points_are_positive_and_advanced_source_is_validated(self) -> None:
        cfg = {"advanced_settings": {"design_tool": {"x_control_points": 24, "y_control_points": 8}}}
        self.assertEqual(validate_control_points(cfg), {"x": 24, "y": 8})
        cfg["advanced_settings"]["design_tool"]["x_control_points"] = 0
        with self.assertRaises(ValueError):
            validate_control_points(cfg)

    def test_normal_cli_percent_controls_resolve_without_launching_fluent(self) -> None:
        stdout = StringIO()
        argv = [
            "run_airfoil_adjoint_optimization.py",
            "--mode",
            "normal",
            "--no-interactive",
            "--show-settings",
            "--drag-step-percent",
            "-0.2",
            "--lift-bound-percent",
            "99.7",
            "--minimum-lift-percent",
            "99.5",
        ]
        with patch("sys.argv", argv), redirect_stdout(stdout):
            result = optimization_cli_main()

        rendered = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("advanced_settings.optimizer.drag_step_percent = -0.2", rendered)
        self.assertIn("advanced_settings.optimizer.lift_runtime_bound_ratio = 0.997", rendered)
        self.assertIn("completion.minimum_lift_ratio = 0.995", rendered)

    def test_candidate_and_control_points_are_rejected_in_normal_mode(self) -> None:
        for extra in (("--candidate-preset", "coupled-c4"), ("--x-control-points", "32")):
            stderr = StringIO()
            argv = ["run_airfoil_adjoint_optimization.py", "--mode", "normal", "--no-interactive", *extra]
            with patch("sys.argv", argv), redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                optimization_cli_main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("advanced", stderr.getvalue().lower())

    def test_debug_reports_force_drift_and_local_optimum_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "outputs" / "run_local_optimum"
            run.mkdir(parents=True)
            attempts = []
            for index in range(2):
                attempts.append(
                    {
                        "index": index,
                        "status": "FAIL_CANDIDATE_GATE",
                        "transcript_audit": {"invalid_morphing": False},
                        "candidate_gate": {"reasons": ["lift_to_drag_not_improved"]},
                        "validation": {
                            "negative_volume_in_validation": False,
                            "force_stability": {
                                "status": "PASS",
                                "samples": [
                                    {"cd": 0.0104, "cl": 0.4820},
                                    {"cd": 0.01041, "cl": 0.48201},
                                ],
                            },
                        },
                    }
                )
            (run / "optimization_summary.json").write_text(
                json.dumps(
                    {
                        "status": "INCOMPLETE_PERFORMANCE_TARGET",
                        "primary_mesh": {"minimum_orthogonal_quality": 0.31, "maximum_aspect_ratio": 800},
                        "adjoint_result": {"attempts": attempts},
                    }
                ),
                encoding="utf-8",
            )
            (run / "resolved_config.json").write_text("{}", encoding="utf-8")

            report = diagnose_run(root, run)

        self.assertTrue(report["force_coefficient_drift_warning"])
        self.assertEqual(report["local_optimum_assessment"]["status"], "LIKELY_LOCAL_OPTIMUM_AT_DESIGN_POINT")


class ForceStabilityEvidenceTests(unittest.TestCase):
    def test_force_stability_drift_uses_first_and_last_samples(self) -> None:
        drift = force_stability_drift(
            [
                {"cd": 0.01, "cl": 0.5},
                {"cd": 0.0099, "cl": 0.5005},
            ]
        )
        self.assertAlmostEqual(drift["cd"], 0.01)
        self.assertAlmostEqual(drift["cl"], 0.001)


class CGridQualityAdaptiveTests(unittest.TestCase):
    def test_portable_naca64_414_fixture_is_readable_and_closed(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "examples" / "naca64_414_pointwise.dat"
        points = read_dat(fixture)

        self.assertEqual(len(points), 101)
        self.assertAlmostEqual(points[0].x, points[-1].x, places=8)
        self.assertAlmostEqual(points[0].y, points[-1].y, places=8)

    def test_gentle_blend_is_monotone_with_zero_end_slopes(self) -> None:
        values = [gentle_end_blend(index / 100.0) for index in range(101)]
        self.assertEqual(values[0], 0.0)
        self.assertEqual(values[-1], 1.0)
        self.assertTrue(all(a <= b for a, b in zip(values, values[1:])))
        self.assertLess(values[1] - values[0], values[50] - values[49])

    def test_quality_adaptive_outlet_preserves_order_and_endpoints(self) -> None:
        left = [Point(1.0, y) for y in (10.0, 2.0, 0.01, 0.0, -0.01, -2.0, -10.0)]
        outlet = quality_adaptive_outlet_y(
            left,
            half_height=10.0,
            center_y=0.0,
            cluster_strength=1.35,
            match_blend=0.90,
            match_power=0.85,
        )

        self.assertEqual(outlet[0], left[0].y)
        self.assertEqual(outlet[-1], left[-1].y)
        self.assertTrue(all(a > b for a, b in zip(outlet, outlet[1:])))

    def test_quality_retry_above_cell_budget_is_rejected_before_generation(self) -> None:
        cfg = {
            "mesh": {
                "cgrid": {
                    "quality_retry_profiles": [
                        {"name": "extra_resolution", "radial_layers": 128}
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            side_effects = [RuntimeError("C-grid quality gate failed: low quality")]
            calls: list[dict] = []
            original = optimizer_module._run_cgrid_mesh_once

            def fake_once(*args, **kwargs):
                calls.append(kwargs)
                result = side_effects.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

            optimizer_module._run_cgrid_mesh_once = fake_once
            try:
                with self.assertRaisesRegex(RuntimeError, "fixed automatic profiles are disabled"):
                    run_cgrid_mesh(cfg, output_dir, {}, dry_run=False)
            finally:
                optimizer_module._run_cgrid_mesh_once = original

        self.assertEqual(len(calls), 0)


class EnsightExportTests(unittest.TestCase):
    def test_encas_is_accepted_and_aliased_to_case_when_variables_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "baseline_ensight"
            prefix.with_suffix(".encas").write_text(
                "\n".join(
                    [
                        "FORMAT",
                        "type: ensight gold",
                        "VARIABLE",
                        "scalar per node: pressure baseline_ensight.scl1",
                        "scalar per node: velocity_magnitude baseline_ensight.scl2",
                        "scalar per node: x_velocity baseline_ensight.scl3",
                        "scalar per node: y_velocity baseline_ensight.scl4",
                        "vector per node: velocity baseline_ensight.vel",
                    ]
                ),
                encoding="utf-8",
            )

            alias = ensure_ensight_case_alias(prefix)

            self.assertEqual(alias, prefix.with_suffix(".case"))
            self.assertTrue(prefix.with_suffix(".case").exists())
            self.assertTrue(
                ensight_case_has_variables(
                    prefix,
                    ["pressure", "velocity-magnitude", "x-velocity", "y-velocity"],
                    require_all=True,
                )
            )

    def test_velocity_only_ensight_does_not_satisfy_flow_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "baseline_ensight"
            prefix.with_suffix(".encas").write_text(
                "VARIABLE\nvector per node: velocity baseline_ensight.vel\n",
                encoding="utf-8",
            )

            self.assertFalse(
                ensight_case_has_variables(
                    prefix,
                    ["pressure", "velocity-magnitude", "x-velocity", "y-velocity"],
                    require_all=True,
                )
            )

    def test_disallowed_sensitivity_entries_are_unavailable_not_required_failure(self) -> None:
        self.assertEqual(
            export_failure_status(
                "settings.file.export.ensight_gold: RuntimeError: Values contain disallowed entries",
                required=False,
                optional_unavailable_if_disallowed=True,
            ),
            "SKIP_UNAVAILABLE",
        )
        self.assertEqual(
            export_failure_status("expected files were not found", required=True, optional_unavailable_if_disallowed=True),
            "FAIL",
        )



class Stage1GateReconciliationTests(unittest.TestCase):
    def test_removed_envelope_config_has_explicit_migration_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "迁移错误"):
            validate_removed_envelope_config({"envelope": {"margin_percent": 5.0}})

    def test_cgrid_evidence_replaces_dry_run_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            stage1_dir = run_dir / "stage1" / "run_20260730_fluentmeshing_stage1"
            stage1_dir.mkdir(parents=True)
            summary_path = stage1_dir / "summary.json"
            summary_path.write_text("{}", encoding="utf-8")
            (stage1_dir / "gate_summary.json").write_text(
                json.dumps(
                    {
                        "blunt_fluent_quad_cell_gate": "SKIP",
                        "blunt_fluent_quality_gate": "SKIP",
                        "blunt_fluent_meshing_gate": "DRY_RUN",
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "resolved_config.json").write_text("{}", encoding="utf-8")

            reconciliation = reconcile_stage1_gates(
                summary_path,
                run_dir,
                {
                    "fluent_quadrilateral_cells": 75372,
                    "fluent_triangular_cells": 0,
                    "minimum_orthogonal_quality": 0.305401,
                    "maximum_aspect_ratio": 123199.0,
                },
                {"commands": []},
                dry_run=False,
            )
            updated = json.loads((stage1_dir / "gate_summary.json").read_text(encoding="utf-8"))

        self.assertEqual(reconciliation["status"], "PASS")
        self.assertEqual(updated["blunt_fluent_quad_cell_gate"], "PASS")
        self.assertEqual(updated["blunt_fluent_quality_gate"], "PASS")
        self.assertEqual(updated["blunt_fluent_meshing_gate"], "PASS")
        self.assertIn("supersedes", reconciliation["update_reasons"]["blunt_fluent_meshing_gate"])

    def test_failed_run_points_to_repair_prompt_folder(self) -> None:
        guidance = repair_branch_guidance(
            "FAIL_TRANSCRIPT_NEGATIVE_VOLUME",
            {"failures": [{"label": "negative volume", "error": "negative volume cell"}]},
            {"maximum_aspect_ratio": 49279.4},
        )

        self.assertTrue(guidance["triggered"])
        self.assertTrue(guidance["read_only_when_triggered"])
        self.assertEqual(Path(guidance["entrypoint"]).name, "REPAIR_ROUTER_PROMPT.md")
        self.assertIn("repair_prompts", Path(guidance["entrypoint"]).parts)
        self.assertTrue(any(path.endswith("NEGATIVE_VOLUME_REPAIR_SKILL.md") for path in guidance["recommended_files"]))
        self.assertTrue(any(path.endswith("C型网格生成与优化标准.md") for path in guidance["recommended_files"]))

    def test_cgrid_dry_run_summary_defaults_triangular_cells_to_zero(self) -> None:
        summary = cgrid_dry_run_summary(
            {"nodes": 10, "quadrilateral_cells": 4},
            cgrid_input=Path("input.dat"),
            mesh_path=Path("mesh.msh"),
            case_path=Path("mesh.msh"),
            summary_path=Path("cgrid_summary.json"),
        )

        self.assertEqual(summary["triangular_cells"], 0)
        self.assertEqual(summary["status"], "DRY_RUN")


class CGridFluentSummaryTests(unittest.TestCase):
    def test_velocity_inlet_pressure_outlet_does_not_expect_pressure_farfield(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mesh = Path(tmp) / "mesh.msh"
            mesh.write_text(
                "\n".join(
                    [
                        "(10 (0 1 a 0 2))",
                        "(12 (0 1 4 0))",
                        "(12 (2 1 4 1 3))",
                        "(13 (0 1 c 0))",
                        "(13 (4 1 2 0 3))",
                        "(13 (5 3 8 0 a))",
                        "(13 (6 9 c 0 5))",
                        "(45 (4 wall airfoil)())",
                        "(45 (5 velocity-inlet velocity_inlet)())",
                        "(45 (6 pressure-outlet pressure_outlet)())",
                    ]
                ),
                encoding="ascii",
            )

            counts = parse_msh_counts(mesh)

        self.assertEqual(counts["fluent_quadrilateral_cells"], 4)
        self.assertEqual(counts["fluent_triangular_cells"], 0)
        self.assertTrue(counts["has_velocity_inlet"])
        self.assertTrue(counts["has_pressure_outlet"])
        self.assertFalse(counts["has_pressure_far_field"])
        self.assertEqual(counts["external_boundary_topology"], "velocity-inlet-pressure-outlet")
        self.assertFalse(counts["pressure_far_field_expected"])


class PortabilityTests(unittest.TestCase):
    def test_write_json_replaces_nonfinite_evidence_with_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            write_json(path, {"failed_cd": float("nan"), "values": [float("inf"), 1.0]})
            text = path.read_text(encoding="utf-8")
            parsed = json.loads(text, parse_constant=lambda value: self.fail(f"non-standard JSON constant: {value}"))

        self.assertIsNone(parsed["failed_cd"])
        self.assertEqual(parsed["values"], [None, 1.0])

    def test_promoted_export_manifest_points_only_to_official_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            staged = run / "accepted_steps" / "step_03" / "staged_exports" / "optimized"
            staged.mkdir(parents=True)
            (staged / "optimized.cas.h5").write_text("case", encoding="utf-8")
            write_json(
                staged / "export_manifest.json",
                {
                    "case": str(staged / "optimized.cas.h5"),
                    "files": [str(staged / "optimized.cas.h5")],
                },
            )
            FluentAdjointRunner({}, run, {}, dry_run=True)._promote_staged_exports(staged)
            manifest = json.loads((run / "exports" / "optimized" / "export_manifest.json").read_text(encoding="utf-8"))

        self.assertNotIn("staged_exports", json.dumps(manifest))
        self.assertIn(str(run / "exports" / "optimized"), manifest["case"])

    def test_project_relative_input_resolves_when_called_from_other_cwd(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                resolved = resolve_input_path("examples/naca0012.dat")
            finally:
                os.chdir(original_cwd)

        self.assertTrue(resolved.exists())
        self.assertEqual(resolved.name, "naca0012.dat")

    def test_prepare_fluent_env_discovers_versioned_ansys_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ansys_root = Path(tmp) / "ansys" / "v260"
            fluent_root = ansys_root / "fluent"
            (fluent_root / "bin").mkdir(parents=True)
            if os.name == "nt":
                (fluent_root / "ntbin" / "win64").mkdir(parents=True)
                path_key = "Path"
            else:
                path_key = "PATH"

            old_env = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update({"AWP_ROOT99": str(Path(tmp) / "old"), "AWP_ROOT260": str(ansys_root), path_key: ""})
                env = prepare_fluent_env()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

        self.assertEqual(Path(env["FLUENT_ROOT"]), fluent_root)
        self.assertEqual(Path(env["FLUENT_INC"]), fluent_root)
        if os.name == "nt":
            self.assertIn("SystemDrive", env)
        self.assertIn(str(fluent_root / "bin"), env.get(path_key, ""))

    def test_auto_product_version_omits_pinned_pyfluent_version(self) -> None:
        self.assertEqual(fluent_product_version_arg("auto"), {})
        self.assertEqual(fluent_product_version_arg(None), {})
        self.assertEqual(fluent_product_version_arg("25.1.0"), {"product_version": "25.1.0"})

    def test_unique_run_dir_handles_same_second_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = create_unique_run_dir(root, "adjoint_optimization")
            second = create_unique_run_dir(root, "adjoint_optimization")

        self.assertNotEqual(first, second)
        self.assertTrue(second.name.endswith("_01"))


if __name__ == "__main__":
    unittest.main()
