"""Small deterministic Chinese/English intent parser for the safe v2 contract."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .jobspec import DEFAULT_MAX_CANDIDATES, DEFAULT_MAX_CELLS, JOB_SPEC_VERSION, sha256_file


class UnsafeIntentError(ValueError):
    pass


_UNSAFE = re.compile(
    r"(?:--set\b|powershell\b|cmd(?:\.exe)?\b|python(?:\.exe)?\s+-c\b|subprocess\b|os\.system\b|rm\s+-rf\b|del\s+/[qsf]\b|[;&|`]\s*(?:python|cmd|powershell))",
    re.IGNORECASE,
)
_NUM = r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"


def _first(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _percent_ratio(text: str, labels: str) -> float | None:
    value = _first(
        text,
        [
            rf"(?:{labels})\s*(?:不低于|至少|保持(?:在)?|>=|≥|at\s+least|minimum(?:\s+of)?)\s*{_NUM}\s*%",
            rf"{_NUM}\s*%\s*(?:的)?\s*(?:{labels})",
        ],
    )
    return value / 100.0 if value is not None else None


def _extract_dat(text: str) -> str | None:
    quoted = re.search(r"[\"']([^\"'\r\n]+?\.dat)[\"']", text, re.IGNORECASE)
    if quoted:
        return quoted.group(1).strip()
    windows = re.search(r"([A-Za-z]:\\[^\r\n,，;；]+?\.dat)\b", text, re.IGNORECASE)
    if windows:
        return windows.group(1).strip()
    generic = re.search(r"(?<!\w)([^\s,，;；]+\.dat)\b", text, re.IGNORECASE)
    return generic.group(1).strip() if generic else None


def _extract_requested_mesh_cells(text: str) -> int | None:
    patterns = [
        rf"(?:网格(?:数量|数|单元数)?|单元数)\s*(?:为|=|约|大约|目标)?\s*{_NUM}\s*(k|w|万)?(?:\s*cells?)?",
        rf"{_NUM}\s*(k|w|万)?\s*(?:cells?|个?网格|个?单元)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = float(match.group(1))
        suffix = (match.group(2) or "").lower()
        multiplier = 10_000 if suffix in {"w", "万"} else 1_000 if suffix == "k" else 1
        cells = value * multiplier
        if not cells.is_integer() or cells <= 0:
            raise ValueError("requested mesh cell count must resolve to a positive integer")
        return int(cells)
    return None


def parse_intent(text: str, saved_defaults: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("simulation sentence cannot be empty")
    if len(text) > 16_384:
        raise ValueError("simulation sentence is too long")
    if _UNSAFE.search(text):
        raise UnsafeIntentError("the sentence contains a forbidden command or code override")

    partial: dict[str, Any] = {
        "schema_version": JOB_SPEC_VERSION,
        "geometry": {"closure": "auto"},
        "flow": {},
        "constraints": {"minimum_area_ratio": 0.95},
        "objective": {"kind": "minimize_cd_feasibility_first"},
        "mesh": {
            "mode": "ai_supervised_cgrid",
            "preferred_cells": None,
            "max_cells": DEFAULT_MAX_CELLS,
            "max_candidates": DEFAULT_MAX_CANDIDATES,
        },
        "execution": {"dry_run": bool(re.search(r"(?:dry[ -]?run|试运行|只检查|不启动\s*fluent)", text, re.IGNORECASE))},
    }
    used_defaults: list[str] = []

    requested_cells = _extract_requested_mesh_cells(text)
    if requested_cells is not None:
        partial["mesh"]["preferred_cells"] = requested_cells
        # A stated cell request is a soft preference.  Raise the per-run budget
        # just enough to make the preference feasible instead of replacing it
        # with the old fixed 48,458-cell policy.
        partial["mesh"]["max_cells"] = max(DEFAULT_MAX_CELLS, requested_cells)

    path_text = _extract_dat(text)
    if path_text:
        path = Path(path_text).expanduser().resolve(strict=False)
        partial["geometry"]["airfoil_path"] = str(path)
        if path.is_file():
            partial["geometry"]["airfoil_sha256"] = sha256_file(path)

    lower = text.lower()
    if re.search(r"(?:原生|有限厚度|钝(?:尾|后)缘|blunt)", text, re.IGNORECASE):
        partial["geometry"]["closure"] = "blunt"
    elif re.search(r"(?:尖尾缘|sharp)", text, re.IGNORECASE):
        partial["geometry"]["closure"] = "sharp"

    chord = _first(text, [rf"(?:弦长|chord(?:\s+length)?)\s*(?:为|=|is)?\s*{_NUM}\s*(?:m|米)\b"])
    angle = _first(text, [rf"(?:攻角|迎角|aoa|angle\s+of\s+attack)\s*(?:为|=|is)?\s*{_NUM}\s*(?:°|deg(?:ree)?s?|度)?"])
    altitude = _first(text, [rf"(?:海拔|高度|altitude)\s*(?:为|=|is)?\s*{_NUM}\s*(?:m|米)\b"])
    velocity = _first(text, [rf"(?:速度|流速|velocity|speed)\s*(?:为|=|is)?\s*{_NUM}\s*(?:m/s|mps|米每秒)\b", rf"{_NUM}\s*m/s\b"])
    reynolds = _first(text, [rf"(?:雷诺数|reynolds(?:\s+number)?|\bRe)\s*(?:为|=|is)?\s*{_NUM}\b"])
    mach = _first(text, [rf"(?:马赫数|mach)\s*(?:为|=|is)?\s*{_NUM}\b"])
    temperature = _first(text, [rf"(?:温度|temperature)\s*(?:为|=|is)?\s*{_NUM}\s*(?:k|开尔文)\b"])
    for key, value in (("chord_m", chord), ("angle_of_attack_deg", angle), ("altitude_m", altitude)):
        if value is not None:
            partial["flow"][key] = value
    speeds = [("velocity_m_s", velocity), ("reynolds_number", reynolds), ("mach", mach)]
    provided_speeds = [(key, value) for key, value in speeds if value is not None]
    if len(provided_speeds) > 1:
        raise ValueError("provide only one of velocity, Reynolds number, or Mach number")
    if provided_speeds:
        partial["flow"][provided_speeds[0][0]] = provided_speeds[0][1]
    if temperature is not None:
        partial["flow"]["temperature_k"] = temperature

    thickness = _percent_ratio(text, r"(?:局部)?厚度|local\s+thickness")
    lift = _percent_ratio(text, r"升力|cl|lift")
    drag_percent = _first(
        text,
        [
            rf"(?:降阻|阻力降低|cd\s+reduction|reduce\s+(?:the\s+)?drag(?:\s+by)?)\s*(?:目标|至少|>=|≥|为|=|of)?\s*{_NUM}\s*%",
        ],
    )
    budget = _first(
        text,
        [
            rf"(?:最大|最多|max(?:imum)?)\s*{_NUM}\s*(?:次)?(?:优化|求解|评估|算例|solver\s+(?:runs|evaluations)|evaluations)",
            rf"(?:运行|执行)\s*{_NUM}\s*次(?:优化|求解|评估|算例)",
            rf"{_NUM}\s*次(?:优化|求解|评估)",
            rf"(?:预算|budget)\s*(?:为|=|is)?\s*{_NUM}",
        ],
    )
    if thickness is not None:
        partial["constraints"]["minimum_local_thickness_ratio"] = thickness
    if lift is not None:
        partial["constraints"]["minimum_lift_ratio"] = lift
    if drag_percent is not None:
        partial["objective"]["minimum_cd_reduction_ratio"] = drag_percent / 100.0
    if budget is not None:
        if not float(budget).is_integer():
            raise ValueError("solver evaluation budget must be an integer")
        partial["objective"]["max_solver_evaluations"] = int(budget)

    ssh = re.search(r"(?:ssh|远程)(?:\s*(?:配置|profile|主机|host)?\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?", text, re.IGNORECASE)
    if ssh:
        partial["execution"]["kind"] = "ssh"
        if ssh.group(1) and ssh.group(1).lower() not in {"remote", "ssh", "远程"}:
            partial["execution"]["profile_id"] = ssh.group(1)
    elif re.search(r"(?:本地|local)", text, re.IGNORECASE):
        partial["execution"]["kind"] = "local"

    if saved_defaults:
        for dotted, value in saved_defaults.items():
            if _get(partial, dotted) is None:
                _set(partial, dotted, value)
                used_defaults.append(dotted)
    return partial, used_defaults


def _get(data: Mapping[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    current = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


REQUIRED_FIELDS = (
    "geometry.airfoil_path",
    "geometry.airfoil_sha256",
    "flow.chord_m",
    "flow.angle_of_attack_deg",
    "flow.altitude_m",
    "flow.speed",
    "constraints.minimum_local_thickness_ratio",
    "constraints.minimum_lift_ratio",
    "objective.minimum_cd_reduction_ratio",
    "objective.max_solver_evaluations",
    "execution.kind",
)


PROPOSED_DEFAULTS: dict[str, Any] = {
    "flow.altitude_m": 0.0,
    "constraints.minimum_local_thickness_ratio": 0.90,
    "constraints.minimum_lift_ratio": 0.998,
    "objective.minimum_cd_reduction_ratio": 0.005,
    "objective.max_solver_evaluations": 12,
    "execution.kind": "local",
}


QUESTIONS: dict[str, tuple[str, str]] = {
    "geometry.airfoil_path": ("请提供翼型 DAT 文件路径。", "Provide the airfoil DAT file path."),
    "geometry.airfoil_sha256": ("DAT 文件无法读取或不存在，请提供有效文件。", "The DAT file is missing or unreadable."),
    "flow.chord_m": ("物理弦长是多少米？", "What is the physical chord in metres?"),
    "flow.angle_of_attack_deg": ("攻角是多少度？", "What is the angle of attack in degrees?"),
    "flow.altitude_m": ("海拔是多少米？推荐 0 m。", "What is the altitude? Recommended: 0 m."),
    "flow.speed": ("请给出流速、雷诺数或马赫数之一。", "Provide one of velocity, Reynolds number, or Mach number."),
    "constraints.minimum_local_thickness_ratio": ("局部厚度至少保留基线的多少百分比？推荐 90%。", "Minimum local thickness ratio? Recommended: 90%."),
    "constraints.minimum_lift_ratio": ("Cl 至少保留基线的多少百分比？推荐 99.8%。", "Minimum lift ratio? Recommended: 99.8%."),
    "objective.minimum_cd_reduction_ratio": ("最低降阻目标是多少百分比？推荐 0.5%。", "Minimum drag reduction target? Recommended: 0.5%."),
    "objective.max_solver_evaluations": ("最多允许多少次求解评估？推荐 12 次。", "Maximum solver evaluations? Recommended: 12."),
    "execution.kind": ("在本地还是已配置的 SSH worker 上运行？", "Run locally or on a configured SSH worker?"),
    "execution.profile_id": ("请提供管理员配置的 SSH profile ID（不是地址或命令）。", "Provide the administrator-owned SSH profile ID."),
}


def missing_fields(partial: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for field in REQUIRED_FIELDS:
        if field == "flow.speed":
            if not any(_get(partial, f"flow.{name}") is not None for name in ("velocity_m_s", "reynolds_number", "mach")):
                result.append(field)
        elif _get(partial, field) is None:
            result.append(field)
    if _get(partial, "execution.kind") == "ssh" and _get(partial, "execution.profile_id") is None:
        result.append("execution.profile_id")
    return result


def questions_for(partial: Mapping[str, Any]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for field in missing_fields(partial):
        zh, en = QUESTIONS[field]
        item: dict[str, Any] = {"field": field, "question_zh": zh, "question_en": en}
        if field in PROPOSED_DEFAULTS:
            item["proposed_default"] = PROPOSED_DEFAULTS[field]
            item["ask_save_as_personal_default"] = True
        questions.append(item)
    return questions


def safe_merge_answers(partial: Mapping[str, Any], answers: Mapping[str, Any], *, use_proposed: bool = False) -> dict[str, Any]:
    result = {key: _deep_copy(value) for key, value in partial.items()}
    allowed = set(REQUIRED_FIELDS) | {
        "execution.profile_id",
        "flow.velocity_m_s",
        "flow.reynolds_number",
        "flow.mach",
        "flow.temperature_k",
    }
    allowed.remove("flow.speed")
    if use_proposed:
        for field in missing_fields(result):
            if field in PROPOSED_DEFAULTS:
                _set(result, field, PROPOSED_DEFAULTS[field])
    flattened = _flatten(answers)
    unknown = sorted(set(flattened) - allowed)
    if unknown:
        raise ValueError(f"answer contains forbidden/unknown fields: {', '.join(unknown)}")
    speed_answers = {key for key in flattened if key in {"flow.velocity_m_s", "flow.reynolds_number", "flow.mach"}}
    if speed_answers:
        for key in ("velocity_m_s", "reynolds_number", "mach"):
            result.setdefault("flow", {}).pop(key, None)
    for field, value in flattened.items():
        _set(result, field, value)
    path_text = _get(result, "geometry.airfoil_path")
    if path_text:
        path = Path(str(path_text)).expanduser().resolve(strict=False)
        if path.is_file():
            _set(result, "geometry.airfoil_path", str(path))
            _set(result, "geometry.airfoil_sha256", sha256_file(path))
    return result


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key or key.startswith("_"):
            raise ValueError("answer field names must be non-private strings")
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            result.update(_flatten(value, dotted))
        else:
            result[dotted] = value
    return result


def _deep_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value
