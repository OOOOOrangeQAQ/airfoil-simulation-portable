"""Portable, policy-gated airfoil workflow public API."""

from .jobspec import JOB_SPEC_VERSION, JobSpecError, validate_job_spec

__all__ = ["JOB_SPEC_VERSION", "JobSpecError", "validate_job_spec"]
__version__ = "2.0.1"
