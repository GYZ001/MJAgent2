"""hiagent.download SSRF 收口（Todolist T8）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import hiagent


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",
        "http://localhost/secret",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/secret",
    ],
)
def test_download_rejects_private_and_metadata_urls(tmp_path: Path, url: str) -> None:
    dest = tmp_path / "out.bin"
    with pytest.raises(hiagent.ProviderError):
        asyncio.run(hiagent.download(url, str(dest)))
    assert not dest.exists()
