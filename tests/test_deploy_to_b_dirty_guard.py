"""``scripts/deploy_to_b.sh`` 脏工作区拒绝发布。

这个脚本 rsync 的是**工作区**而不是某个提交，所以工作区脏的时候跑它，会把别人没写完
的代码静默发到生产。2026-09-12 实测踩到边上：仓库里同时有另一拨未提交的在途改动
（组织/角色 + 模型库，``app/orgs`` 还没建完、全仓测试是红的），只差没人手滑跑一下。

测试真的执行脚本，不是 grep 源码找关键字——脚本从 ``BASH_SOURCE`` 推 ``ROOT``，把它
复制进一个临时 git 仓库，``ROOT`` 就是那个临时仓库，于是可以在不碰真仓库、不连 B 的
前提下验证真实行为。断言里带「没有走到 rsync」这一条：只看退出码的话，任何一种早退
（比如连不上 B）都能让测试变绿，而那不是这条闸门。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy_to_b.sh"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MJ_B_SSH": "mj-test-unreachable-host"}
    return subprocess.run(
        ["bash", str(root / "scripts" / SCRIPT.name), *args],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )


def test_clean_tree_passes_the_guard_and_reaches_the_ssh_check(tmp_path: Path) -> None:
    """干净树不该被这条闸门拦住——它应该继续往下走，然后死在连不上 B 上（退出码 2）。

    这条是闸门的「绿」侧：没有它，下面那条红就可能只是因为脚本根本跑不起来。
    """
    result = _run(_repo(tmp_path), "--dry-run")
    assert result.returncode == 2, result.stderr
    assert "连不上" in result.stderr
    assert "工作区不干净" not in result.stderr


def test_dirty_tree_is_refused_before_anything_is_synced(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "half_finished.py").write_text("# 别人没写完的东西\n", encoding="utf-8")

    result = _run(root, "--dry-run")

    assert result.returncode == 4, result.stderr
    assert "工作区不干净" in result.stderr
    assert "half_finished.py" in result.stderr
    # 没有走到 rsync：连通性检查都还没做，更谈不上同步
    assert "rsync" not in result.stdout
    assert "连不上" not in result.stderr


def test_modified_tracked_file_counts_as_dirty(tmp_path: Path) -> None:
    """未跟踪的新文件与改动过的已跟踪文件都要拦——后者才是「改了一半」的常见形态。"""
    root = _repo(tmp_path)
    (root / "scripts" / SCRIPT.name).write_text(
        (root / "scripts" / SCRIPT.name).read_text(encoding="utf-8") + "\n# 改了一行\n",
        encoding="utf-8",
    )
    result = _run(root, "--dry-run")
    assert result.returncode == 4
    assert "工作区不干净" in result.stderr


def test_allow_dirty_is_an_explicit_opt_out(tmp_path: Path) -> None:
    """出路必须存在且显式：确认过这些改动就该上线的人，加一个参数照样能发。"""
    root = _repo(tmp_path)
    (root / "half_finished.py").write_text("x\n", encoding="utf-8")

    result = _run(root, "--dry-run", "--allow-dirty")

    assert result.returncode == 2, result.stderr  # 过了闸门，死在连不上 B
    assert "工作区不干净" not in result.stderr


@pytest.mark.parametrize("bad", ["--allowdirty", "--force"])
def test_unknown_flags_still_rejected(tmp_path: Path, bad: str) -> None:
    """新增参数不能顺手放宽参数解析——拼错的开关必须报错，不能被当成「没加」而静默继续。"""
    result = _run(_repo(tmp_path), bad)
    assert result.returncode == 1
    assert "未知参数" in result.stderr
