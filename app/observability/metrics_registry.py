"""EP-06 指标：进程内存里的 Prometheus 原语，零依赖（不引入 ``prometheus_client``）。

设计约束（见 PRD/enterprise/EP-06_部署运维与安全基线.md §5）：
- **在内存聚合，不得每次请求写库**——写锁热点是本仓库出过整站冻结事故的地方
  （见 ``app/observability/lock_pressure.py`` 模块文档）。本文件全程只碰进程内
  的 dict + 一把 ``threading.Lock``，不 import ``app.db``。
- 本文件是 L1（app/LAYERS.toml）：不依赖任何其它 ``app.*`` 模块，纯标准库，
  这样 L2 的 ``app.observability.lock_pressure`` 才能在不引入新上行边的前提下
  调用它来记录写锁等待。

Counter/Histogram/Gauge 三种原语都按 ``(metric_name, 排序后的 label 元组)``
做 key；渲染成文本时同一 metric_name 的所有 label 组合聚在一起，前面加一行
``# TYPE``。所有写操作共用一把锁——调用频率是"每次 HTTP 请求/每次供应商调用"
量级，不是热循环，锁竞争可忽略；相比每个指标一把锁，单锁换来渲染时"读一份
一致快照"不用夹七把锁，取舍是故意的。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

LabelSet = tuple[tuple[str, str], ...]

# app.db.new_id() 生成 "<prefix>_<12 位十六进制>"（见该函数），这是全仓 id 的
# 唯一形状；纯数字段与 16+ 位十六进制串（部分历史 id/hash）一并当动态段处理。
# 启发式，不是穷举——某个自定义路径段恰好长得像 id 会被误判，代价是 label
# 基数控制错一次，不是错误行为，可接受。
_ID_SEGMENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*_[0-9a-fA-F]{8,}$|^[0-9]+$|^[0-9a-fA-F]{16,}$")


def normalize_path_group(path: str) -> str:
    """把 HTTP 路径里的动态 id 段折成 ``:id``，控制 ``path_group`` label 基数。

    不折叠会让 ``manju_http_requests_total`` 的 label 组合数跟着"历史上出现过
    多少个不同 project_id/episode_id"无限增长——Prometheus 的 label 基数本身
    就是常见的生产事故源，这里在写入前就做限制，而不是依赖采集端后处理。
    """
    segments = path.split("/")
    normalized = [":id" if seg and _ID_SEGMENT_RE.match(seg) else seg for seg in segments]
    return "/".join(normalized)

DEFAULT_SECONDS_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300,
)

_lock = threading.Lock()
_counters: dict[str, dict[LabelSet, float]] = {}
_gauges: dict[str, dict[LabelSet, float]] = {}
_histograms: dict[str, dict[LabelSet, "_Histogram"]] = {}
_help_text: dict[str, str] = {}
_metric_kind: dict[str, str] = {}


@dataclass
class _Histogram:
    buckets: tuple[float, ...]
    bucket_counts: list[int] = field(default_factory=list)
    count: int = 0
    total: float = 0.0

    def __post_init__(self) -> None:
        if not self.bucket_counts:
            self.bucket_counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for i, ceiling in enumerate(self.buckets):
            if value <= ceiling:
                self.bucket_counts[i] += 1


def _labels_key(labels: dict[str, str] | None) -> LabelSet:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _register(name: str, kind: str, help_text: str) -> None:
    """幂等登记 metric 的 TYPE/HELP 文案，供渲染时输出一次。"""
    _metric_kind.setdefault(name, kind)
    _help_text.setdefault(name, help_text)


def inc_counter(
    name: str, labels: dict[str, str] | None = None, *, amount: float = 1.0,
    help_text: str = "",
) -> None:
    key = _labels_key(labels)
    with _lock:
        _register(name, "counter", help_text)
        bucket = _counters.setdefault(name, {})
        bucket[key] = bucket.get(key, 0.0) + amount


def set_gauge(
    name: str, value: float, labels: dict[str, str] | None = None, *, help_text: str = "",
) -> None:
    key = _labels_key(labels)
    with _lock:
        _register(name, "gauge", help_text)
        _gauges.setdefault(name, {})[key] = value


def clear_gauge(name: str, labels: dict[str, str] | None = None) -> None:
    """撤掉一行 gauge（例如模型健康状态从 down 翻回 up 时，删掉 down=1 那行）。"""
    key = _labels_key(labels)
    with _lock:
        _gauges.get(name, {}).pop(key, None)


def replace_gauge_family(name: str, rows: list[tuple[dict[str, str], float]], *, help_text: str = "") -> None:
    """整族 gauge 一次性替换（先清空同名 metric 的全部 label 组合，再写入新的）。

    供 scrape 时态的采集器用（``metrics_collectors.py``）：workflow_type 这类
    label 取值集合是"当下查询到什么就有什么"，不是固定枚举——上一次 scrape 还
    在跑的 workflow_type，这一次可能已经清空。如果只用 ``set_gauge`` 逐行覆盖，
    消失的 workflow_type 会把上一次的旧值永远留在暴露文本里，这里整族替换避免
    这个陈旧数据问题。
    """
    with _lock:
        _register(name, "gauge", help_text)
        _gauges[name] = {_labels_key(labels): value for labels, value in rows}


def observe_histogram(
    name: str, value: float, labels: dict[str, str] | None = None, *,
    buckets: tuple[float, ...] = DEFAULT_SECONDS_BUCKETS, help_text: str = "",
) -> None:
    key = _labels_key(labels)
    with _lock:
        _register(name, "histogram", help_text)
        per_labels = _histograms.setdefault(name, {})
        hist = per_labels.get(key)
        if hist is None:
            hist = _Histogram(buckets=buckets)
            per_labels[key] = hist
        hist.observe(value)


def timer() -> "_Timer":
    return _Timer()


class _Timer:
    """``with metrics_registry.timer() as t: ...`` 用法糖，取秒级耗时。"""

    def __enter__(self) -> "_Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_s = time.monotonic() - self._start

    def peek(self) -> float:
        return time.monotonic() - self._start


def _fmt_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(key: LabelSet) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{_fmt_label_value(v)}"' for k, v in key)
    return "{" + inner + "}"


def _fmt_value(value: float) -> str:
    if value == float("inf"):
        return "+Inf"
    if value != value:  # NaN
        return "NaN"
    return repr(float(value))


_emitted_headers: set[str] = set()


def _emit_header(lines: list[str], name: str) -> None:
    if name in _emitted_headers:
        return
    _emitted_headers.add(name)
    help_text = _help_text.get(name, "")
    kind = _metric_kind.get(name, "gauge")
    if help_text:
        lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")


def render_prometheus_text() -> str:
    """渲染全部已登记指标为 Prometheus 文本暴露格式（``text/plain; version=0.0.4``）。

    渲染是只读快照：持锁期间只做字典浅拷贝，尽快释放，避免把锁一直攥到字符串
    拼接完（拼接可能被大量 label 组合拖慢，不该占着影响其它线程记指标）。
    """
    with _lock:
        names = list(_metric_kind.keys())  # 插入顺序=登记顺序，预登记的族排最前
        counters_snapshot = {n: dict(s) for n, s in _counters.items()}
        gauges_snapshot = {n: dict(s) for n, s in _gauges.items()}
        # 直方图对象是就地累加（bucket_counts 是可变 list），深拷贝一份 bucket_counts
        # 才是真正的快照；只拷 dict 外壳的话渲染期间若又有新 observe() 进来，会把
        # 还没渲染完的那一行数值改花。
        histograms_snapshot = {
            n: {k: _Histogram(buckets=h.buckets, bucket_counts=list(h.bucket_counts),
                               count=h.count, total=h.total)
                for k, h in series.items()}
            for n, series in _histograms.items()
        }
    _emitted_headers.clear()
    lines: list[str] = []
    _render_from_snapshot(lines, names, counters_snapshot, gauges_snapshot, histograms_snapshot)
    return "\n".join(lines) + "\n" if lines else ""


def _render_from_snapshot(
    lines: list[str],
    names: list[str],
    counters_snapshot: dict[str, dict[LabelSet, float]],
    gauges_snapshot: dict[str, dict[LabelSet, float]],
    histograms_snapshot: dict[str, dict[LabelSet, "_Histogram"]],
) -> None:
    # 遍历"全部已登记过的 metric 名字"而不是只遍历有数据的三个 dict：
    # pre_register() 在 import 时登记的族即使还没有任何观测，也要能输出
    # "# TYPE" 头（PRD 要求 /metrics 里始终能看到这几组指标名，不是等第一次
    # 真实调用发生才出现）。
    for name in names:
        _emit_header(lines, name)
        for key, value in counters_snapshot.get(name, {}).items():
            lines.append(f"{name}{_fmt_labels(key)} {_fmt_value(value)}")
        for key, value in gauges_snapshot.get(name, {}).items():
            lines.append(f"{name}{_fmt_labels(key)} {_fmt_value(value)}")
        for key, hist in histograms_snapshot.get(name, {}).items():
            _render_one_histogram(lines, name, key, hist)


def _render_one_histogram(lines: list[str], name: str, key: LabelSet, hist: "_Histogram") -> None:
    # hist.bucket_counts[i] 已经是"值 <= buckets[i] 的观测次数"（_Histogram.observe
    # 对每次 observe() 把所有满足 value <= ceiling 的桶都 +1），本身就是 Prometheus
    # 要求的累积语义，这里只管原样输出，不能再累加一次，否则每往上一档桶就被
    # 二次放大（曾经的 bug：见 tests/test_metrics_endpoint.py 的桶值回归用例）。
    base = dict(key)
    for ceiling, bucket_count in zip(hist.buckets, hist.bucket_counts):
        le_key = _labels_key({**base, "le": _fmt_value(ceiling)})
        lines.append(f"{name}_bucket{_fmt_labels(le_key)} {bucket_count}")
    inf_key = _labels_key({**base, "le": "+Inf"})
    lines.append(f"{name}_bucket{_fmt_labels(inf_key)} {hist.count}")
    lines.append(f"{name}_sum{_fmt_labels(key)} {_fmt_value(hist.total)}")
    lines.append(f"{name}_count{_fmt_labels(key)} {hist.count}")


# ---------------------------------------------------------------------------
# 具名指标——PRD/enterprise/EP-06 §5 列出的最少集合。调用方（HTTP 中间件、
# lock_pressure、metrics_collectors）只认这些函数名，不直接拼 metric 字符串，
# 避免同一个指标在不同调用点被拼出两个不小心打错的名字互不相认。
# ---------------------------------------------------------------------------
HTTP_REQUESTS_TOTAL = "manju_http_requests_total"
HTTP_REQUEST_DURATION_SECONDS = "manju_http_request_duration_seconds"
JOBS_ACTIVE = "manju_jobs_active"
JOBS_QUEUED = "manju_jobs_queued"
MODEL_HEALTH = "manju_model_health"
MODEL_FAILURES_TOTAL = "manju_model_failures_total"
QUOTA_USAGE_RATIO = "manju_quota_usage_ratio"
DB_WRITE_LOCK_WAIT_SECONDS = "manju_db_write_lock_wait_seconds"
PROVIDER_CALL_LATENCY_SECONDS = "manju_provider_call_latency_seconds"

# PRD §5 的"最少集合"：预登记 HELP/TYPE，即使进程刚启动、一次观测都还没发生，
# /metrics 也能看到这 9 个族的元数据行——运维/Grafana 按名字配置面板不必等到
# 第一次真实事件触发才发现指标"不存在"。_pre_register_defaults 在文件末尾
# 调用一次（模块 import 时），reset_all()（仅供测试）之后也会重新调用一次，
# 否则测试对 reset_all 的调用会把这批元数据一并清空。
_DEFAULT_FAMILIES: tuple[tuple[str, str, str], ...] = (
    (HTTP_REQUESTS_TOTAL, "counter", "HTTP 请求总数"),
    (HTTP_REQUEST_DURATION_SECONDS, "histogram", "HTTP 请求耗时（秒）"),
    (JOBS_ACTIVE, "gauge", "正在运行的工作流数（按 workflow_type 分组）"),
    (JOBS_QUEUED, "gauge", "排队等待执行的工作流数（按 workflow_type 分组）"),
    (MODEL_HEALTH, "gauge", "模型健康状态（1=当前状态）"),
    (MODEL_FAILURES_TOTAL, "counter", "供应商调用失败次数"),
    (QUOTA_USAGE_RATIO, "gauge", "配额用量占上限的比例（0-1，>1 表示已超限）"),
    (DB_WRITE_LOCK_WAIT_SECONDS, "histogram", "SQLite 写锁等待耗时（秒），只统计等满 busy_timeout 仍失败的次数"),
    (PROVIDER_CALL_LATENCY_SECONDS, "histogram", "供应商调用耗时（秒，按 purpose 分组）"),
)

_last_model_health_state: dict[str, str] = {}


def _pre_register_defaults() -> None:
    with _lock:
        for name, kind, help_text in _DEFAULT_FAMILIES:
            _register(name, kind, help_text)


def record_http_request(method: str, path_group: str, status: int, duration_s: float) -> None:
    labels = {"method": method, "path_group": path_group, "status": str(status)}
    inc_counter(HTTP_REQUESTS_TOTAL, labels, help_text="HTTP 请求总数")
    observe_histogram(
        HTTP_REQUEST_DURATION_SECONDS, duration_s, {"method": method, "path_group": path_group},
        help_text="HTTP 请求耗时（秒）",
    )


def record_db_write_lock_wait(seconds: float) -> None:
    """记一次写锁等待。见 ``app.observability.lock_pressure.note_lock_contention``
    调用点的说明：这里记的是"等满 busy_timeout 仍未抢到锁"的那部分等待时间，
    不是所有写事务的排队时间——后者需要在 app.db 里逐笔埋点，而 app.db 是本次
    改动的禁区（CLAUDE.md 派单要求 diff 为零），如实只报可测的这一部分。"""
    observe_histogram(DB_WRITE_LOCK_WAIT_SECONDS, seconds, help_text="SQLite 写锁等待耗时（秒），只统计等满 busy_timeout 仍失败的次数")


def record_provider_call_latency(purpose: str, duration_s: float) -> None:
    """按 purpose 记一次供应商调用耗时。只在 ``app.models_registry.routing.
    call_with_failover`` 这一个调用点观测（PRD 要求的 label 只有 purpose，没有
    model_id）——那是唯一同时拿得到 purpose 字符串与真实耗时的地方；
    ``app.models_registry.health.record_outcome`` 的"拉"路径（定期反查
    provider_calls 批量重放）没有 purpose 上下文，不重复计这个指标，避免同一次
    调用被两条路径各算一次耗时。"""
    observe_histogram(
        PROVIDER_CALL_LATENCY_SECONDS, duration_s, {"purpose": purpose},
        help_text="供应商调用耗时（秒，按 purpose 分组）",
    )


def record_model_outcome(model_id: str, state: str, reason: str | None) -> None:
    """模型健康状态 + 失败计数的唯一写入点，配 ``app.models_registry.health.
    record_outcome``（覆盖"推"/"拉"两条路径，是全仓最接近"每次真实供应商调用"
    的汇合点）。``reason`` 为 None 表示成功，不计入 ``MODEL_FAILURES_TOTAL``；
    只在这一处计数，避免与 ``record_provider_call_latency`` 的调用点重叠导致
    同一次失败被算两次。"""
    previous = _last_model_health_state.get(model_id)
    if previous and previous != state:
        clear_gauge(MODEL_HEALTH, {"model_id": model_id, "state": previous})
    set_gauge(MODEL_HEALTH, 1, {"model_id": model_id, "state": state}, help_text="模型健康状态（1=当前状态）")
    _last_model_health_state[model_id] = state
    if reason:
        inc_counter(
            MODEL_FAILURES_TOTAL, {"model_id": model_id, "reason": reason},
            help_text="供应商调用失败次数",
        )


def set_jobs_active(module: str, count: int) -> None:
    set_gauge(JOBS_ACTIVE, count, {"module": module}, help_text="正在运行的工作流数（按 workflow_type 分组）")


def set_jobs_queued(module: str, count: int) -> None:
    set_gauge(JOBS_QUEUED, count, {"module": module}, help_text="排队等待执行的工作流数（按 workflow_type 分组）")


def set_quota_usage_ratio(scope_type: str, scope_id: str, resource: str, ratio: float) -> None:
    set_gauge(
        QUOTA_USAGE_RATIO, ratio, {"scope_type": scope_type, "scope_id": scope_id, "resource": resource},
        help_text="配额用量占上限的比例（0-1，>1 表示已超限）",
    )


def reset_all() -> None:
    """仅供测试：清空全部已登记指标，避免用例之间互相污染。清空后立即重新预
    登记默认族（见 ``_DEFAULT_FAMILIES``），否则调了 reset_all() 的测试会看不到
    PRD 要求"即使没有观测也要有 HELP/TYPE 头"的那批默认指标。"""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _histograms.clear()
        _help_text.clear()
        _metric_kind.clear()
        _last_model_health_state.clear()
    _emitted_headers.clear()
    _pre_register_defaults()


_pre_register_defaults()
