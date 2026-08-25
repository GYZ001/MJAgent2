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


def _stub_fingerprint(monkeypatch, values) -> list:
    """把 compute_code_fingerprint 换成按调用顺序回放 values 列表的假实现，
    并记录调用次数——用于精确断言"基线记录一次 + 每次自愈重启前重新计算一次"
    这个调用节奏，不依赖真实文件哈希。"""
    calls: list[int] = []

    def fake():
        calls.append(1)
        idx = len(calls) - 1
        return values[idx] if idx < len(values) else values[-1]

    monkeypatch.setattr(yyft_serial10, "compute_code_fingerprint", fake)
    return calls


# ---------------------------------------------------------------------------
# 1) 调用序：停轮 -> 重启后端 -> clear -> 从 EP1 重跑
# ---------------------------------------------------------------------------

def test_stop_then_auto_restart_clear_rerun_call_order(monkeypatch, tmp_path) -> None:
    """红灯：失败停轮后必须依次触发 重启后端 -> clear -> 重新发起一轮，不需要
    人工协调；第二轮再次失败（不同签名）也照样触发第二次 重启->clear->重跑；
    第三轮成功后循环结束。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    _stub_fingerprint(monkeypatch, ["same"])
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
    _stub_fingerprint(monkeypatch, ["same"])
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
    _stub_fingerprint(monkeypatch, ["same"])
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
    _stub_fingerprint(monkeypatch, ["same"])
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
    _stub_fingerprint(monkeypatch, ["same"])
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
    _stub_fingerprint(monkeypatch, ["same"])
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
    _stub_fingerprint(monkeypatch, ["same"])
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
    _stub_fingerprint(monkeypatch, ["same"])
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
        raise AssertionError("--single-pass 不该触发重启/清库/指纹计算")

    monkeypatch.setattr(yyft_serial10, "restart_backend", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "compute_code_fingerprint", fail_if_called)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from="", single_pass=True))

    assert rc == 4


# ---------------------------------------------------------------------------
# 8) 代码指纹护栏（2026-08-24 起，第 36 轮回归事故复盘新增）：自愈重启从磁盘
#    加载代码；重启前指纹如果与本次 run 调用开头记录的基线不一致，说明 app/
#    在回归期间被并行改动过，本轮结果不可信，必须立即停轮，绝不能带着半成品
#    代码继续重启/清库/重跑。指纹算法本身（对 app/**/*.py 内容、git HEAD、
#    logs/data 变化的敏感/不敏感）见 tests/test_yyft_serial10_code_fingerprint.py；
#    本节只测 cmd_run 如何使用这个指纹做循环级别的护栏判断。
# ---------------------------------------------------------------------------

def test_fingerprint_baseline_recorded_once_and_rechecked_before_each_restart(
    monkeypatch, tmp_path,
) -> None:
    """正面：指纹全程不变——基线在 run 调用开头记录一次，之后每次自愈重启前
    重新计算一次并与基线比对；只要一致，自动循环照常继续，行为与指纹护栏
    加入之前完全一致。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    fp_calls = _stub_fingerprint(monkeypatch, ["stable-hash"])
    passes = [
        (4, {}, _sig(message_digest="sig-a")),
        (4, {}, _sig(message_digest="sig-b")),
        (0, {"EP1": "ready"}, None),
    ]
    pass_calls: list[int] = []

    def fake_pass(start_index):
        pass_calls.append(start_index)
        return passes[len(pass_calls) - 1]

    restart_calls = {"n": 0}

    def fake_restart():
        restart_calls["n"] += 1
        return True

    monkeypatch.setattr(yyft_serial10, "_execute_serial_pass", fake_pass)
    monkeypatch.setattr(yyft_serial10, "restart_backend", fake_restart)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", lambda: True)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 0
    assert len(pass_calls) == 3
    assert restart_calls["n"] == 2
    # 1 次基线 + 2 次自愈重启前的复核 = 3 次；不多不少。
    assert len(fp_calls) == 3


def test_fingerprint_drift_before_restart_stops_cycle_with_dedicated_exit_code(
    monkeypatch, tmp_path,
) -> None:
    """红灯核心场景：第 1 轮停轮后，重启前重新计算指纹发现与基线不一致
    （app/ 在本轮回归期间被并行改动）——必须立即停止自动循环，绝不能带着
    可能已经变化的代码继续 重启/清库/重跑；退出码是与 0/2/3/4/5/6 都不冲突
    的专用值，日志写明"回归期间代码发生变更，本轮结果不可信，已停止"。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    _stub_fingerprint(monkeypatch, ["baseline-hash", "drifted-hash"])
    monkeypatch.setattr(
        yyft_serial10, "_execute_serial_pass",
        lambda start_index: (4, {}, _sig()),
    )

    def fail_if_called(*_a, **_k):
        raise AssertionError("指纹漂移后不该继续重启/清库")

    monkeypatch.setattr(yyft_serial10, "restart_backend", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", fail_if_called)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == yyft_serial10.CODE_DRIFT_EXIT_CODE
    assert rc not in {0, 2, 3, 4, 5, 6}
    log_text = (tmp_path / "serial10.log").read_text(encoding="utf-8")
    assert "回归期间代码发生变更，本轮结果不可信，已停止" in log_text
    assert "重新发起" in log_text


def test_fingerprint_drift_detected_only_at_second_restart_boundary(
    monkeypatch, tmp_path,
) -> None:
    """红灯：第 1 轮之后指纹仍与基线一致，第 1 次自愈重启正常发生；第 2 轮
    再次失败，这次重启前指纹已经漂移——必须在第 2 个重启边界停轮，且已经
    发生过的第 1 次重启/清库不受影响（不追溯撤销），第 2 次重启/清库绝不
    能发生。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    _stub_fingerprint(monkeypatch, ["hash-0", "hash-0", "hash-1"])
    passes = [
        (4, {}, _sig(message_digest="sig-a")),
        (4, {}, _sig(message_digest="sig-b")),
    ]
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

    assert rc == yyft_serial10.CODE_DRIFT_EXIT_CODE
    assert len(pass_calls) == 2
    assert restart_calls["n"] == 1
    assert clear_calls["n"] == 1


def test_fingerprint_not_checked_when_first_pass_succeeds(
    monkeypatch, tmp_path,
) -> None:
    """边界：第 1 轮直接全部 ready——从不触发任何自愈重启，指纹只在 run 调用
    开头记录一次基线，不会有任何"重启前复核"调用（没有重启可言）。"""
    monkeypatch.setattr(yyft_serial10, "LOG", tmp_path / "serial10.log")
    fp_calls = _stub_fingerprint(monkeypatch, ["only-baseline"])
    monkeypatch.setattr(
        yyft_serial10, "_execute_serial_pass",
        lambda start_index: (0, {"EP1": "ready"}, None),
    )

    def fail_if_called():
        raise AssertionError("首轮即成功，不该触发重启/清库")

    monkeypatch.setattr(yyft_serial10, "restart_backend", fail_if_called)
    monkeypatch.setattr(yyft_serial10, "_clear_all_episodes", fail_if_called)

    rc = yyft_serial10.cmd_run(SimpleNamespace(start_from=""))

    assert rc == 0
    assert len(fp_calls) == 1
