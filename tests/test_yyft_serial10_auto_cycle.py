"""scripts/yyft_serial10.py 的"失败停轮→自动重启+clear+重跑"协议覆盖（2026-08-24）。

背景（用户新协议，2026-08-24）：回归轮中途失败停轮后，不需要人工协调，驱动自己
重启后端 → 健康探测 → clear → 从 EP1 重新开始一轮。这是**内部触发器**——与协调层
在修复落地时主动杀掉驱动进程、重启、clear、重发 run 的**外部触发器**并存，互不
冲突（后者对驱动的唯一要求是：进程内状态不落盘、可以在任意一步被 SIGTERM/SIGKILL
直接杀掉后安全重来，见 scripts/yyft_serial10.py 文件头协议说明）。

本文件覆盖 cmd_run 的自动循环协议本身（不是分诊/重试阶梯——那部分见
tests/test_yyft_serial10_failure_triage.py，且已改用 single_pass=True 与本文件
的循环层解耦）：
  1) 停轮后自动触发 重启→clear→重跑 的调用序，用真实 mock 断言调用顺序；
  2) 后端重启协议：import 自检持续失败时绝不 kill 旧进程；自检重试后成功则
     正常继续走完 kill 旧进程 -> 拉起新进程 -> 健康探测；健康探测/端口释放
     超时也会让整个重启失败；
  3) 死循环保险丝——同一失败签名连续出现 2 次即停轮；不同签名继续循环；
  4) 单次 run 调用的自动轮数上限触发停轮；
  5) 重启/clear 本身失败时的收尾行为（不清库、不重跑）；
  6) 自动循环触发的每一轮固定从 EP1 开始（即使首轮 --from 指定了别的起点）；
  7) --single-pass 退回旧语义，完全不触发本协议。

全部通过 monkeypatch 驱动内部函数完成，不连接真实后端、不发真实 HTTP、不碰
真实数据库/剧本数据（第 33 轮回归正在用真实后端跑，这份测试绝不能碰它）。
"""
from __future__ import annotations

from types import SimpleNamespace

from scripts import yyft_serial10


def _sig(episode="EP1", family="content", exc_type="PrepPackGateError",
         message_digest="digest") -> yyft_serial10.FailureSignature:
    return yyft_serial10.FailureSignature(
        episode=episode, family=family, exc_type=exc_type,
        message_digest=message_digest,
    )


# ---------------------------------------------------------------------------
# 1) 调用序：停轮 -> 重启后端 -> clear -> 从 EP1 重跑
# ---------------------------------------------------------------------------

def test_stop_then_auto_restart_clear_rerun_call_order(monkeypatch, tmp_path) -> None:
    """红灯：失败停轮后必须依次触发 重启后端 -> clear -> 重新发起一轮，不需要
    人工协调；第二轮再次失败（不同签名）也照样触发第二次 重启->clear->重跑；
    第三轮成功后循环结束。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    events: list[str] = []
    pass_calls: list[int] = []

    passes = [
        (4, {}, _sig(message_digest="sig-a")),
        (4, {}, _sig(message_digest="sig-b")),
        (0, {"EP1": "ready"}, None),
    ]

    def fake_pass(start_index):
        pass_calls.append(start_index)
        events.append(f"pass:{start_index}")
        return passes[len(pass_calls) - 1]

    def fake_restart():
        events.append("restart")
        return True

    def fake_clear():
        events.append("clear")
        return True

    monkeypatch.setattr(yyft_serial10, "_execute_serial_pass", fake_pass)
    monkeypatch.setattr(yyft_serial10, "restart_backend", fake_restart)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", fake_clear)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 0
    assert events == [
        "pass:0", "restart", "clear",
        "pass:0", "restart", "clear",
        "pass:0",
    ]


# ---------------------------------------------------------------------------
# 2) 后端重启协议：import 自检 / kill / 健康探测
# ---------------------------------------------------------------------------

def test_import_self_check_failure_never_kills_old_process(monkeypatch) -> None:
    """红灯：import app.main 自检持续失败（工作树疑似正被其他 agent 半编辑）时，
    绝不能 kill 旧进程——宁可用旧代码继续跑也不能把服务打死。"""
    sleeps: list[float] = []
    monkeypatch.setattr(yyft_serial10.time, "sleep", lambda s: sleeps.append(s))
    check_calls = {"n": 0}

    def fake_check():
        check_calls["n"] += 1
        return False, "ImportError: circular import（工作树半编辑中）"

    monkeypatch.setattr(yyft_serial10, "_backend_import_self_check", fake_check)

    def fail_if_called(*_a, **_k):
        raise AssertionError("不应该在 import 自检失败时触碰旧进程/拉起新进程")

    monkeypatch.setattr(yyft_serial10, "_find_backend_pid", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_kill_backend_pid", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_launch_backend", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_health_probe", fail_if_called)

    ok = yyft_serial10.restart_backend()

    assert ok is False
    assert check_calls["n"] == yyft_serial10.BACKEND_IMPORT_RETRY_MAX
    assert sleeps == [yyft_serial10.BACKEND_IMPORT_RETRY_DELAY_S] * (
        yyft_serial10.BACKEND_IMPORT_RETRY_MAX - 1
    )


def test_import_self_check_retries_then_succeeds(monkeypatch) -> None:
    """正面：自检前两次失败、第三次通过——不算工作树半编辑，重启协议正常
    继续走完 kill 旧进程 -> 拉起新进程 -> 健康探测。"""
    sleeps: list[float] = []
    monkeypatch.setattr(yyft_serial10.time, "sleep", lambda s: sleeps.append(s))
    check_calls = {"n": 0}

    def fake_check():
        check_calls["n"] += 1
        return check_calls["n"] >= 3, ""

    monkeypatch.setattr(yyft_serial10, "_backend_import_self_check", fake_check)
    monkeypatch.setattr(yyft_serial10, "_find_backend_pid", lambda: 4242)
    killed = {"pid": None}

    def fake_kill(pid):
        killed["pid"] = pid
        return True

    monkeypatch.setattr(yyft_serial10, "_kill_backend_pid", fake_kill)
    launched = {"n": 0}
    monkeypatch.setattr(
        yyft_serial10, "_launch_backend",
        lambda: launched.__setitem__("n", launched["n"] + 1),
    )
    monkeypatch.setattr(yyft_serial10, "_health_probe", lambda: True)

    ok = yyft_serial10.restart_backend()

    assert ok is True
    assert check_calls["n"] == 3
    assert sleeps == [yyft_serial10.BACKEND_IMPORT_RETRY_DELAY_S] * 2
    assert killed["pid"] == 4242
    assert launched["n"] == 1


def test_health_probe_timeout_fails_restart(monkeypatch) -> None:
    """反面：自检/kill/拉起都顺利，但健康探测一直拿不到 200——整个重启判失败。"""
    monkeypatch.setattr(yyft_serial10, "_backend_import_self_check", lambda: (True, ""))
    monkeypatch.setattr(yyft_serial10, "_find_backend_pid", lambda: None)
    monkeypatch.setattr(yyft_serial10, "_launch_backend", lambda: None)
    monkeypatch.setattr(yyft_serial10, "_health_probe", lambda: False)

    ok = yyft_serial10.restart_backend()

    assert ok is False


def test_kill_release_timeout_fails_restart_without_launch(monkeypatch) -> None:
    """反面：旧进程 kill 后端口一直不释放——不能在这种状态下硬拉起新进程
    （会撞端口），必须直接判重启失败。"""
    monkeypatch.setattr(yyft_serial10, "_backend_import_self_check", lambda: (True, ""))
    monkeypatch.setattr(yyft_serial10, "_find_backend_pid", lambda: 999)
    monkeypatch.setattr(yyft_serial10, "_kill_backend_pid", lambda pid: False)

    def fail_if_called(*_a, **_k):
        raise AssertionError("端口未释放就不该拉起新进程/做健康探测")

    monkeypatch.setattr(yyft_serial10, "_launch_backend", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_health_probe", fail_if_called)

    ok = yyft_serial10.restart_backend()

    assert ok is False


# ---------------------------------------------------------------------------
# 3) 死循环保险丝：同一签名连续 2 次停轮；不同签名继续循环
# ---------------------------------------------------------------------------

def test_same_signature_twice_in_a_row_stops(monkeypatch, tmp_path) -> None:
    """红灯：连续两轮命中完全相同的失败签名——判定为确定性问题，停止自动
    循环，不再尝试第三次重启/清库/重跑。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    same = _sig(message_digest="deterministic-#")
    passes = [(4, {}, same), (4, {}, same)]
    pass_calls: list[int] = []

    def fake_pass(start_index):
        pass_calls.append(start_index)
        return passes[len(pass_calls) - 1]

    restart_calls = {"n": 0}
    clear_calls = {"n": 0}

    def fake_restart():
        restart_calls["n"] += 1
        return True

    def fake_clear():
        clear_calls["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "_execute_serial_pass", fake_pass)
    monkeypatch.setattr(yyft_serial10, "restart_backend", fake_restart)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", fake_clear)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 4
    assert len(pass_calls) == 2
    assert restart_calls["n"] == 1  # 只在第 1 轮之后重启过一次，第 2 轮判重复后不再重启
    assert clear_calls["n"] == 1
    log_text = (tmp_path / "serial10.log").read_text(encoding="utf-8")
    assert "同一失败签名连续出现 2 次" in log_text


def test_different_signatures_keep_cycling_until_success(monkeypatch, tmp_path) -> None:
    """反面：每一轮的失败签名都不同——不能被误判为"确定性复现"，必须继续
    循环直到成功或触达轮数上限。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    passes = [
        (4, {}, _sig(message_digest="sig-a")),
        (4, {}, _sig(message_digest="sig-b")),
        (0, {"EP1": "ready"}, None),
    ]
    pass_calls: list[int] = []

    def fake_pass(start_index):
        pass_calls.append(start_index)
        return passes[len(pass_calls) - 1]

    monkeypatch.setattr(yyft_serial10, "_execute_serial_pass", fake_pass)
    monkeypatch.setattr(yyft_serial10, "restart_backend", lambda: True)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", lambda: True)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 0
    assert len(pass_calls) == 3


# ---------------------------------------------------------------------------
# 4) 单次 run 调用的自动轮数上限
# ---------------------------------------------------------------------------

def test_cycle_cap_stops_after_max_cycles(monkeypatch, tmp_path) -> None:
    """红灯：即使每一轮失败签名都不同（不会被"连续复现"熔断触发），单次 run
    调用也必须在 AUTO_RUN_CYCLE_MAX 轮后停止，不能无限循环下去。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    pass_calls: list[int] = []

    def fake_pass(start_index):
        pass_calls.append(start_index)
        # 每轮签名都带上调用次数，保证永不连续重复，专测轮数上限这一道护栏。
        return 4, {}, _sig(message_digest=f"sig-{len(pass_calls)}")

    restart_calls = {"n": 0}

    def fake_restart():
        restart_calls["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "_execute_serial_pass", fake_pass)
    monkeypatch.setattr(yyft_serial10, "restart_backend", fake_restart)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", lambda: True)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 4
    assert len(pass_calls) == yyft_serial10.AUTO_RUN_CYCLE_MAX
    assert restart_calls["n"] == yyft_serial10.AUTO_RUN_CYCLE_MAX - 1
    log_text = (tmp_path / "serial10.log").read_text(encoding="utf-8")
    assert "自动循环已达单次 run 调用的上限" in log_text


# ---------------------------------------------------------------------------
# 5) 重启/clear 本身失败时的收尾（不清库、不重跑）
# ---------------------------------------------------------------------------

def test_restart_failure_stops_without_clearing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    monkeypatch.setattr(
        yyft_serial10, "_execute_serial_pass",
        lambda start_index: (4, {}, _sig()),
    )
    monkeypatch.setattr(yyft_serial10, "restart_backend", lambda: False)

    def fail_if_called():
        raise AssertionError("重启失败不该继续 clear")

    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", fail_if_called)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 5


def test_clear_failure_stops_without_second_pass(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    pass_calls: list[int] = []

    def fake_pass(start_index):
        pass_calls.append(start_index)
        return 4, {}, _sig()

    monkeypatch.setattr(yyft_serial10, "_execute_serial_pass", fake_pass)
    monkeypatch.setattr(yyft_serial10, "restart_backend", lambda: True)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", lambda: False)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 6
    assert len(pass_calls) == 1


def test_missing_signature_on_failure_is_fail_safe_stop(monkeypatch, tmp_path) -> None:
    """防御性红灯：万一 _execute_serial_pass 失败却没产出签名（不应该发生），
    也不能死循环——立即停止，不去碰重启/清库。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    monkeypatch.setattr(
        yyft_serial10, "_execute_serial_pass",
        lambda start_index: (4, {}, None),
    )

    def fail_if_called():
        raise AssertionError("没有签名就不该继续尝试自动恢复")

    monkeypatch.setattr(yyft_serial10, "restart_backend", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", fail_if_called)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 4


# ---------------------------------------------------------------------------
# 6) 自动循环触发的每一轮固定从 EP1 开始（--from 只影响首轮）
# ---------------------------------------------------------------------------

def test_auto_cycle_always_restarts_from_ep1_after_first_pass(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    monkeypatch.setattr(
        yyft_serial10, "EPISODES",
        [("EP1", "e1"), ("EP2", "e2"), ("EP3", "e3"), ("EP4", "e4")],
    )
    passes = [
        (4, {}, _sig(episode="EP4", message_digest="sig-a")),
        (0, {"EP1": "ready"}, None),
    ]
    start_indexes: list[int] = []

    def fake_pass(start_index):
        start_indexes.append(start_index)
        return passes[len(start_indexes) - 1]

    monkeypatch.setattr(yyft_serial10, "_execute_serial_pass", fake_pass)
    monkeypatch.setattr(yyft_serial10, "restart_backend", lambda: True)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", lambda: True)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from="EP4"))

    assert rc == 0
    # 首轮从 --from=EP4 对应的下标 3 开始；自动循环重来的一轮固定回到 EP1（下标 0）。
    assert start_indexes == [3, 0]


# ---------------------------------------------------------------------------
# 7) --single-pass 完全不触发自动循环协议（旧语义，供人工介入/单测单轮逻辑本身）
# ---------------------------------------------------------------------------

def test_single_pass_flag_bypasses_auto_cycle(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    monkeypatch.setattr(
        yyft_serial10, "_execute_serial_pass",
        lambda start_index: (4, {}, _sig()),
    )

    def fail_if_called(*_a, **_k):
        raise AssertionError("--single-pass 不该触发重启/清库")

    monkeypatch.setattr(yyft_serial10, "restart_backend", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", fail_if_called)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from="", single_pass=True))

    assert rc == 4
