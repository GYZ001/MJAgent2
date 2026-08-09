from __future__ import annotations

from scripts.verify import _runtime_facade_modules, affected_python_tests


def test_runtime_facades_cover_exec_loaded_implementation_slices() -> None:
    assert _runtime_facade_modules([
        "app/media_exec/run_job.py",
        "app/domain/video_ops.py",
        "app/delivery.py",
    ]) == {"app.worker", "app.api"}


def test_media_exec_change_selects_worker_regressions() -> None:
    selected = affected_python_tests(["app/media_exec/run_job.py"])

    assert "tests/test_media_pipeline_v2.py" in selected
    assert "tests/test_media_job_recovery.py" in selected
