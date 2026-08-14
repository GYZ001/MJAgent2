from __future__ import annotations

import ast
import inspect
from pathlib import Path
import subprocess

from app.harness import model_gateway


def test_format_repair_context_is_optional_keyword_only() -> None:
    parameter = inspect.signature(
        model_gateway.chat_structured
    ).parameters["format_repair_context"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == ""


def test_direct_chat_structured_calls_pass_required_keyword_only_arguments() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    required = {
        name
        for name, parameter in inspect.signature(
            model_gateway.chat_structured
        ).parameters.items()
        if (
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            and parameter.default is inspect.Parameter.empty
        )
    }
    violations: list[str] = []

    for relative_path in tracked:
        path = root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "chat_structured"
                and isinstance(function.value, ast.Name)
                and function.value.id == "model_gateway"
            ):
                continue
            explicit_keywords = {
                keyword.arg
                for keyword in node.keywords
                if keyword.arg is not None
            }
            missing = sorted(required - explicit_keywords)
            if missing:
                violations.append(
                    f"{relative_path}:{node.lineno} missing {', '.join(missing)}"
                )

    assert not violations, "\n".join(violations)
