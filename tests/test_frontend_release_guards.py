from pathlib import Path


def test_frontend_contains_no_local_debug_event_post() -> None:
    root = Path(__file__).resolve().parents[1] / "frontend"
    forbidden = "127.0.0.1:7778/event"
    offenders = []
    for directory in (root / "src", root / "dist"):
        for path in directory.rglob("*"):
            if path.is_file() and forbidden in path.read_text(
                encoding="utf-8",
                errors="ignore",
            ):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
