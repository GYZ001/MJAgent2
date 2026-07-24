"""一键自动：to_storyboard 不得自动确认；full 才可越过确认门。"""
from __future__ import annotations

import pytest

from app.capabilities import inputs as I


def test_auto_start_input_defaults_to_storyboard_gate():
    args = I.ProductionAutoStartInput(project_id="proj_x")
    assert args.mode == "to_storyboard"


def test_auto_start_input_accepts_full_mode():
    args = I.ProductionAutoStartInput(project_id="proj_x", mode="full")
    assert args.mode == "full"


def test_auto_start_rejects_unknown_mode():
    with pytest.raises(Exception):
        I.ProductionAutoStartInput(project_id="proj_x", mode="whatever")  # type: ignore[arg-type]
