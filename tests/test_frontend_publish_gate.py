"""守住「前端构建不等于发布」这条闸门本身。

后端 ``app/main.py`` 用 ``SpaStaticFiles(directory=frontend/dist)`` 服务前端，
StaticFiles 每次请求都读盘——写进 ``frontend/dist`` 的那一刻就是发布到生产，而
Python 代码要手工重启才生效。两者生效方式不对称，就有了版本偏斜。

2026-08-30 实测事故：并行 agent 把 ``npm run build`` 当自检手段跑，前端 18:06
发布、后端进程还停在 14:50，用户拿到新前端配旧后端，小说导入预检直接挂掉。

修法是让 vite 的 outDir 指向 ``dist-staging``（构建无副作用），发布收敛到
``scripts/publish_frontend.py``（校验 + 偏斜闸门 + 原子替换）。本文件守住两件
容易被悄悄改回去的事。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_publish_module():
    path = ROOT / "scripts" / "publish_frontend.py"
    spec = importlib.util.spec_from_file_location("publish_frontend", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vite_does_not_build_into_the_live_directory() -> None:
    """outDir 一旦改回 dist，构建就重新带上发布副作用，且没有任何人会察觉。"""
    config = (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    match = re.search(r"outDir:\s*'([^']+)'", config)
    assert match is not None, "vite.config.ts 必须显式声明 build.outDir"
    out_dir = match.group(1)
    assert out_dir != "dist", (
        "vite 的 outDir 不得指向 dist：后端直接服务该目录，构建会即时发布到生产。"
        " 发布请走 scripts/publish_frontend.py。"
    )
    assert out_dir == "dist-staging"


def test_backend_detection_is_immune_to_self_matching_shell_commands() -> None:
    """按 argv 逐元素判定，不对拼接后的命令行做子串匹配。

    本仓库被子串自匹配咬过三次：``pkill -f "uvicorn app.main"`` 匹配到自己的命令行
    把 shell 一起杀掉；``pgrep -f "pytest tests/"`` 让人误以为全量测试在跑（其实早
    被自己 kill 了）；写发布脚本时的验证命令又自匹配了一次。

    shell 的 argv 是 ``[bash, -c, <整段脚本>]``——整段脚本是**一个**元素，永远不会
    等于 ``app.main:app``。逐元素比对对这个形态结构上免疫。
    """
    module = _load_publish_module()
    real_backend = [
        ".venv/bin/python",
        "./.venv/bin/uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8230",
    ]
    assert module._is_backend_argv(real_backend) is True

    # 一条提到了这两个词的普通 shell 命令——旧的子串判据会把它认成后端进程。
    self_matching_shell = [
        "/usr/bin/bash",
        "-c",
        'for p in /proc/*; do case "$c" in *uvicorn*app.main:app*) echo hit;; esac; done',
    ]
    assert module._is_backend_argv(self_matching_shell) is False
    # 证明这不是空测试：旧判据确实会误判上面这条。
    assert all(k in " ".join(self_matching_shell) for k in ("uvicorn", "app.main:app"))

    # uvicorn 之外的 python 进程即便带了 app.main:app 也不算。
    assert module._is_backend_argv(["python", "-c", "app.main:app"]) is False


def test_skew_gate_blocks_when_backend_predates_current_backend_code(tmp_path) -> None:
    """后端启动早于最新 app/**/*.py 时必须拦住，且提示里要给出路。"""
    module = _load_publish_module()
    stale_file = tmp_path / "app" / "some_module.py"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("x = 1")

    module._backend_processes = lambda: [(4242, 1000.0)]
    module._newest_backend_code = lambda: (2000.0, stale_file)
    module.ROOT = tmp_path

    ok, message = module._skew_gate()
    assert ok is False
    assert "4242" in message
    # 拦住用户时必须给出路，不能把人晾在原地。
    assert "出路" in message and "kill 4242" in message


def test_skew_gate_passes_when_backend_started_after_the_newest_code(tmp_path) -> None:
    module = _load_publish_module()
    fresh = tmp_path / "app" / "some_module.py"
    fresh.parent.mkdir(parents=True)
    fresh.write_text("x = 1")

    module._backend_processes = lambda: [(4242, 3000.0)]
    module._newest_backend_code = lambda: (2000.0, fresh)
    module.ROOT = tmp_path

    ok, _ = module._skew_gate()
    assert ok is True


def test_skew_gate_uses_the_oldest_process_when_several_are_running(tmp_path) -> None:
    """多个后端在跑时取最早启动的：任一在服务的进程代码是旧的，发布就不安全。"""
    module = _load_publish_module()
    code = tmp_path / "app" / "x.py"
    code.parent.mkdir(parents=True)
    code.write_text("x = 1")
    module._backend_processes = lambda: [(111, 1000.0), (222, 5000.0)]
    module._newest_backend_code = lambda: (2000.0, code)
    module.ROOT = tmp_path

    ok, message = module._skew_gate()
    assert ok is False
    assert "111" in message, "应对最早启动的进程判定，而不是最新的"
    assert "检出 2 个后端进程" in message


@pytest.mark.parametrize(
    "layout, expected_fragment",
    [
        ({}, "不存在"),
        ({"index.html": ""}, "为空"),
        ({"index.html": "<html>"}, "缺少 assets"),
    ],
)
def test_staging_readiness_is_judged_on_artifacts_not_exit_codes(
    tmp_path, layout, expected_fragment
) -> None:
    """产物完整性挂文件本身，不挂构建命令的退出码。"""
    module = _load_publish_module()
    staging = tmp_path / "dist-staging"
    if layout:
        staging.mkdir()
        for name, content in layout.items():
            (staging / name).write_text(content)
    module.STAGING = staging

    ok, message = module._staging_ready()
    assert ok is False
    assert expected_fragment in message


def test_staging_readiness_accepts_a_complete_build(tmp_path) -> None:
    module = _load_publish_module()
    staging = tmp_path / "dist-staging"
    (staging / "assets").mkdir(parents=True)
    (staging / "index.html").write_text("<html>")
    (staging / "assets" / "index-abc123.js").write_text("//")
    module.STAGING = staging

    ok, message = module._staging_ready()
    assert ok is True
    assert "产物完整" in message


def test_atomic_swap_leaves_the_old_build_untouched_when_promotion_fails(tmp_path) -> None:
    """破坏性操作要有原子性：中途失败旧产物必须原封不动。"""
    module = _load_publish_module()
    live = tmp_path / "dist"
    live.mkdir()
    (live / "index.html").write_text("旧版")
    module.LIVE = live
    module.FRONTEND = tmp_path
    module.STAGING = tmp_path / "dist-staging"  # 不存在 -> 第二次 rename 必失败

    with pytest.raises(OSError):
        module._atomic_swap()

    assert live.is_dir(), "回滚后 dist 必须还在原处"
    assert (live / "index.html").read_text() == "旧版"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("dist.superseded-")]
    assert leftovers == [], f"回滚后不应留下留档目录：{leftovers}"
