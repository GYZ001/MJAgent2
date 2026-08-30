"""``.gitignore`` 不得让任何源码目录对 git 不可见。

事故：仓库根 ``.gitignore`` 里有一条**未锚定**的 ``projects/``——它本意是忽略
运行期产物目录 ``app/config.py::PROJECTS_DIR``（``RUNTIME_ROOT / "projects"``，
在仓库根下），但 gitignore 的目录模式不加前导 ``/`` 会匹配**任意深度**的同名
目录。2026-08-30 把 ``app/domain/projects.py``（1,999 行）拆成真包
``app/domain/projects/`` 时，整个新包因此对 ``git add`` 结构性不可见：本地
import 正常、测试全绿、闸门全过，**但它一个文件都不会进仓库**。别人拉下来就是
缺模块。

这类缺陷的特征与 CLAUDE.md 记的 ``check_contract_surface.py`` 那条同形：
**检查/忽略规则悄悄失效，而所有人以为它在正常工作**。所以判据不能挂在「有没有
人记得检查」上，要挂在可执行的判据上。

判据从数据推导：遍历源码树里**实际存在**的目录，逐个问 git「你会忽略它吗」。
不维护任何白名单——新建的源码目录自动被覆盖。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 源码根。这些目录下的任何子目录都是源码，不该被忽略。
SOURCE_ROOTS = ("app", "scripts", "tests", "frontend/src")

#: 这些是工具产物，出现在源码树里也确实该被忽略——它们不是源码。
#: 这不是「例外白名单」：判据是「它是不是工具生成的缓存/依赖」，
#: 而不是「它叫什么名字所以放行」。
TOOL_ARTIFACT_NAMES = frozenset({
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "node_modules", ".venv", "dist", "build", ".benchmarks",
})


def _source_dirs() -> list[Path]:
    found: list[Path] = []
    for root in SOURCE_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        found.append(base)
        for path in base.rglob("*"):
            if not path.is_dir():
                continue
            if any(part in TOOL_ARTIFACT_NAMES for part in path.relative_to(ROOT).parts):
                continue
            found.append(path)
    return found


def test_no_source_directory_is_hidden_by_gitignore() -> None:
    dirs = _source_dirs()
    assert dirs, "源码树应当存在，判据才有意义（空集合不等于「无需检查」）"

    rels = [str(d.relative_to(ROOT)) + "/" for d in dirs]
    # git check-ignore 只对被忽略的路径输出行；一次批量问完，别逐个起进程。
    proc = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=ROOT, input="\n".join(rels), capture_output=True, text=True,
    )
    ignored = [line for line in proc.stdout.splitlines() if line.strip()]

    assert not ignored, (
        "以下源码目录被 .gitignore 忽略了，它们对 git add 结构上不可见——"
        "本地一切正常但推上去缺文件：\n  " + "\n  ".join(ignored)
        + "\n\n多半是某条目录模式没加前导斜杠（例如 `projects/` 会匹配任意深度的"
          "同名目录，应写成 `/projects/` 只匹配仓库根）。"
    )


def test_runtime_product_directories_are_still_ignored() -> None:
    """反向判据：锚定之后，仓库根的运行期产物目录必须仍然被忽略。

    只修「源码被误伤」而把运行期产物放进仓库，是把一个缺陷换成更糟的一个——
    ``projects/`` 下是用户的项目产物，``data/`` 下是生产数据库。
    """
    for name in ("projects", "data", "logs"):
        target = ROOT / name
        if not target.is_dir():
            pytest.skip(f"本机没有运行期目录 {name}/，无法验证")
        proc = subprocess.run(
            ["git", "check-ignore", "-q", f"{name}/"], cwd=ROOT,
        )
        assert proc.returncode == 0, f"运行期产物目录 {name}/ 必须被 .gitignore 忽略"
