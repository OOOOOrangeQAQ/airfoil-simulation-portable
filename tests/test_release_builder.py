from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    path = ROOT / "tools" / "build_portable_release.py"
    spec = importlib.util.spec_from_file_location("build_portable_release", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ReleaseBuilderTests(unittest.TestCase):
    def test_allowlist_contains_tests_tools_runtime_and_wheelhouse(self) -> None:
        builder = _load_builder()
        self.assertTrue(builder._allowed(Path("tests/test_experience_portability.py")))
        self.assertTrue(builder._allowed(Path("tools/refresh_portable_manifest.py")))
        self.assertTrue(builder._allowed(Path("runtime/python312-win-x64/python.exe")))
        self.assertTrue(builder._allowed(Path("wheelhouse/packages/example.whl")))
        self.assertTrue(builder._allowed(Path("FINAL_DELIVERY_INDEX_ZH.md")))
        self.assertTrue(builder._allowed(Path("RELEASE_CANDIDATE_STATUS_ZH.md")))
        self.assertFalse(builder._allowed(Path("experience/quarantine/untrusted.json")))
        self.assertFalse(builder._allowed(Path("runs/secret.json")))

    def test_sbom_uses_shipped_lock_not_host_environment(self) -> None:
        builder = _load_builder()
        packages = builder._python_packages()
        self.assertIn({"name": "ansys-fluent-core", "version": "0.40.2"}, packages)
        self.assertEqual(packages, sorted(packages, key=lambda item: item["name"].lower()))

    def test_project_version_is_read_from_pyproject(self) -> None:
        builder = _load_builder()
        self.assertEqual(builder._project_version(), "2.0.2")


if __name__ == "__main__":
    unittest.main()
