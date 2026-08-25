"""scripts/yyft_serial10.py 的代码指纹算法覆盖（2026-08-24 起，自愈重启护栏）。

背景：第 36 轮回归实测事故——自愈重启从磁盘加载代码，期间若 app/ 正被其他
agent 并行修改，重启会把半成品代码捞进服务，之后各轮的绿灯/红灯都不能当
证据用。护栏职责是"检测并诚实停下"，不是锁代码；检测的前提是先有一个可靠、
确定性的代码指纹算法，本文件只测这个算法本身（纯函数，不碰真实后端/HTTP/
数据库，也不依赖真实仓库当前状态——全部用 tmp_path 构造隔离的假 app/ 目录，
即使本文件运行期间真实仓库的 app/ 被其他并行 agent 修改，也不影响这里的
断言）。

cmd_run 自动循环里"每次自愈重启前比对、指纹变化则停轮"的集成行为见
tests/test_yyft_serial10_auto_cycle.py。
"""
from __future__ import annotations

import subprocess

from scripts import yyft_serial10


def _make_fake_repo(tmp_path):
    """构造一个隔离的假仓库：app/ 下两个 .py 文件（含子目录）、logs/、data/、
    一份非 .py 资源文件，外加一个独立的"驱动脚本自身"文件。返回
    (root, driver_path)。"""
    root = tmp_path / "repo"
    (root / "app" / "sub").mkdir(parents=True)
    (root / "app" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "app" / "sub" / "mod2.py").write_text("VALUE2 = 2\n", encoding="utf-8")
    (root / "app" / "template.json").write_text('{"k": 1}\n', encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "run.log").write_text("noise\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "state.db").write_text("noise\n", encoding="utf-8")
    driver_path = root / "driver.py"
    driver_path.write_text("DRIVER_VERSION = 1\n", encoding="utf-8")
    return root, driver_path


def _fp(root, driver_path):
    return yyft_serial10.compute_code_fingerprint(root=root, driver_path=driver_path)


# ---------------------------------------------------------------------------
# 稳定性 / 确定性
# ---------------------------------------------------------------------------

def test_fingerprint_stable_across_repeated_calls(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    assert _fp(root, driver_path) == _fp(root, driver_path)


def test_fingerprint_insensitive_to_mtime_only_touch(tmp_path) -> None:
    """红灯核心断言：只 touch mtime、不改内容——指纹必须不变（不能用 mtime
    做指纹，mtime 会被无意义的 touch 改变，产生假阳性停轮）。"""
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    target = root / "app" / "mod.py"
    content = target.read_bytes()
    os_utime_future = 10_000_000
    import os
    st = target.stat()
    os.utime(target, (st.st_atime + os_utime_future, st.st_mtime + os_utime_future))
    assert target.read_bytes() == content  # 内容确实没变
    after = _fp(root, driver_path)
    assert after == before


# ---------------------------------------------------------------------------
# 覆盖范围：对 app/**/*.py 内容变化敏感
# ---------------------------------------------------------------------------

def test_fingerprint_changes_when_app_py_content_changes(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    (root / "app" / "mod.py").write_text("VALUE = 999\n", encoding="utf-8")
    after = _fp(root, driver_path)
    assert after != before


def test_fingerprint_changes_when_nested_app_py_content_changes(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    (root / "app" / "sub" / "mod2.py").write_text("VALUE2 = 999\n", encoding="utf-8")
    after = _fp(root, driver_path)
    assert after != before


def test_fingerprint_changes_when_app_py_file_added(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    (root / "app" / "new_module.py").write_text("NEW = True\n", encoding="utf-8")
    after = _fp(root, driver_path)
    assert after != before


def test_fingerprint_changes_when_app_py_file_removed(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    (root / "app" / "sub" / "mod2.py").unlink()
    after = _fp(root, driver_path)
    assert after != before


def test_fingerprint_changes_when_driver_script_itself_changes(tmp_path) -> None:
    """驱动脚本自身也纳入指纹（见 compute_code_fingerprint 的模块级注释理由）。"""
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    driver_path.write_text("DRIVER_VERSION = 999\n", encoding="utf-8")
    after = _fp(root, driver_path)
    assert after != before


# ---------------------------------------------------------------------------
# 不覆盖：logs/、data/、非 .py 资源文件的变化不该触发指纹变化
# ---------------------------------------------------------------------------

def test_fingerprint_insensitive_to_logs_changes(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    (root / "logs" / "run.log").write_text("more noise entirely different\n", encoding="utf-8")
    (root / "logs" / "another.log").write_text("brand new log file\n", encoding="utf-8")
    after = _fp(root, driver_path)
    assert after == before


def test_fingerprint_insensitive_to_data_changes(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    (root / "data" / "state.db").write_text("mutated runtime state\n", encoding="utf-8")
    (root / "data" / "new_table.db").write_text("brand new db file\n", encoding="utf-8")
    after = _fp(root, driver_path)
    assert after == before


def test_fingerprint_insensitive_to_non_py_files_under_app(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    before = _fp(root, driver_path)
    (root / "app" / "template.json").write_text('{"k": 999}\n', encoding="utf-8")
    (root / "app" / "notes.txt").write_text("irrelevant\n", encoding="utf-8")
    after = _fp(root, driver_path)
    assert after == before


# ---------------------------------------------------------------------------
# 覆盖范围：git HEAD 变化也应反映到指纹里（即使 app/**/*.py 字节内容不变）
# ---------------------------------------------------------------------------

def _git(root, *args):
    subprocess.run(
        ["git", *args], cwd=root, check=True,
        capture_output=True, text=True,
    )


def test_fingerprint_changes_when_git_head_moves_with_identical_app_content(tmp_path) -> None:
    root, driver_path = _make_fake_repo(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    before = _fp(root, driver_path)

    # 再提交一次 --allow-empty：app/**/*.py 字节内容完全不变，但 HEAD 移动了。
    _git(root, "commit", "-q", "--allow-empty", "-m", "empty follow-up")
    after = _fp(root, driver_path)

    assert after != before


def test_fingerprint_tolerates_missing_git_repo(tmp_path) -> None:
    """假仓库没有 .git（tmp_path 下的普通目录）——git rev-parse 会失败，指纹
    计算不能因此崩溃，必须优雅降级（仍产出确定性字符串）。"""
    root, driver_path = _make_fake_repo(tmp_path)
    fp1 = _fp(root, driver_path)
    fp2 = _fp(root, driver_path)
    assert fp1 == fp2
    assert isinstance(fp1, str) and fp1
