"""视频供应商接入层：把"接哪一家"收敛成一个注册表。

创建、轮询、下载、能力快照、提示词方言、等待策略这六件事原先各自写了一处
``if provider == "minimax_h3"``，散落在 hiagent、video_plan、video_prompt_profiles
和 media_exec/run_job 四个模块里。接第三家要在六个位置各改一次，漏改任何一处都
只在真实出片时才暴露。这里把六件事收敛到一个适配器对象上，注册表按 provider 名
解析，新增一家只需要实现一个适配器并注册。

适配器是实例而不是模块：同一套协议要能绑定不同的服务实例（各自的 base_url、
Key、参数默认值），实例化是唯一能同时表达"协议相同、连接不同"的形式。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.video_plan.models import ProviderVideoCapabilitySnapshot
    from app.video_prompt_profiles import VideoPromptProfile


class VideoProviderAdapter(Protocol):
    """一家视频供应商要提供的全部接入能力。

    这些方法就是整条流水线对供应商的完整依赖面：少实现一个，就意味着某条
    路径会退回到"猜"——所以协议里不设可选方法。
    """

    provider: str
    """注册表键，同时是能力快照里的 provider。"""

    gateway: str
    """能力快照的 gateway 标签，用于区分同一 provider 下的不同网关。"""

    serial_generation: bool
    """供应商是否只能串行出片（决定计划阶段是否按串行估算时延）。"""

    wait_meta_keys: tuple[str, ...]
    """本适配器写进 job meta 的键；重新提交时按这个清单清理。"""

    async def create_video_task(
        self,
        prompt_text: str,
        *,
        image_urls: list[tuple[str, str]] | None = None,
        video_urls: list[tuple[str, str]] | None = None,
        return_last_frame: bool = False,
        call_meta: dict[str, Any] | None = None,
    ) -> str:
        """提交生成任务，返回本适配器可识别的 task_id。"""

    async def poll_video_task(
        self,
        task_id: str,
        *,
        call_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """查询一次任务状态。"""

    def owns_task_id(self, task_id: str) -> bool:
        """task_id 是否由本适配器创建（轮询与下载据此路由）。"""

    def owns_output_url(self, url: str) -> bool:
        """产物 URL 是否属于本适配器的服务。"""

    async def download_output(self, url: str, dest_path: str) -> None:
        """下载本适配器的产物。"""

    def capability_snapshot(
        self,
        *,
        provider: str,
        model: str,
    ) -> ProviderVideoCapabilitySnapshot:
        """产出一份能力快照，决定哪些镜头可渲染。"""

    def capability_snapshot_is_current(
        self,
        snapshot: ProviderVideoCapabilitySnapshot,
    ) -> bool:
        """已存快照是否仍与当前运行时一致；不一致则重新探测。"""

    def prompt_profile(self) -> VideoPromptProfile:
        """本供应商的提示词方言。"""

    def apply_wait_policy(
        self,
        task_id: str,
        result: dict[str, Any],
        meta: dict[str, Any],
        policy: dict[str, Any],
        *,
        duration_s: float,
        current: float,
    ) -> dict[str, Any]:
        """按供应商语义细化等待策略，并把可观测字段写进 meta（原地修改）。"""


# 内置 provider 名 → 构造函数。延迟构造，避免 import 期把整条供应商链拉起来。
_BUILTIN_FACTORIES: dict[str, str] = {
    "hiagent": "app.seedance:SeedanceAdapter",
    "minimax_h3": "app.minimax_h3:MiniMaxH3Adapter",
}

# 页面自建实例可选的接入协议。键是存进模型库的 protocol 值，值是内置适配器名。
# 这里就是"能自助添加什么"的唯一事实来源：协议本身仍要有人实现，但同一协议下
# 的新服务实例不再需要改代码。
VIDEO_PROTOCOLS: dict[str, str] = {
    "minimax_h3": "minimax_h3",
    "seedance": "hiagent",
}

_CACHE: dict[str, VideoProviderAdapter] = {}


def _build(spec: str) -> VideoProviderAdapter:
    module_name, _, attr = spec.partition(":")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)()


def registered_providers() -> tuple[str, ...]:
    return tuple(_BUILTIN_FACTORIES)


def _catalog_item(provider: str) -> dict[str, Any] | None:
    """取自建实例在模型库里的条目（含 base_url / api_key / params）。"""
    import json

    from app.db import get_setting

    try:
        custom = json.loads(get_setting("custom_models") or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(custom, list):
        return None
    item = next(
        (
            entry for entry in custom
            if isinstance(entry, dict) and entry.get("provider") == provider
        ),
        None,
    )
    if item is None:
        return None
    try:
        credentials = json.loads(get_setting("model_credentials") or "{}")
    except (TypeError, ValueError):
        credentials = {}
    saved = (
        credentials.get(str(item.get("id") or ""), {})
        if isinstance(credentials, dict) else {}
    )
    merged = dict(item)
    if isinstance(saved, dict):
        for key in ("base_url", "api_key"):
            if saved.get(key):
                merged[key] = saved[key]
    return merged


def _build_custom(provider: str) -> VideoProviderAdapter | None:
    """按模型库里声明的 protocol 构造一个绑定到该实例连接的适配器。"""
    item = _catalog_item(provider)
    if item is None or "video" not in (item.get("kinds") or []):
        return None
    protocol = str(item.get("protocol") or "").strip().lower()
    builtin = VIDEO_PROTOCOLS.get(protocol)
    if builtin == "minimax_h3":
        from app.minimax_h3 import MiniMaxH3Adapter, connection_from_catalog_item

        return MiniMaxH3Adapter(
            provider=provider,
            connection=connection_from_catalog_item(item),
        )
    if builtin == "hiagent":
        from app.seedance import SeedanceAdapter

        return SeedanceAdapter(provider=provider)
    return None


def resolve(provider: str) -> VideoProviderAdapter:
    """按 provider 名取适配器；未知供应商回落到默认网关。

    回落而不是抛错，是因为 ``active_provider`` 本身已经对未知值做过收敛，
    这里再抛一次只会把配置问题伪装成生成失败。
    """
    key = str(provider or "").strip() or "hiagent"
    if key in _CACHE:
        return _CACHE[key]
    if key in _BUILTIN_FACTORIES:
        _CACHE[key] = _build(_BUILTIN_FACTORIES[key])
        return _CACHE[key]
    if key.startswith("custom:"):
        # 自建实例的连接可能随时被页面改掉，不进缓存。
        adapter = _build_custom(key)
        if adapter is not None:
            return adapter
    return resolve("hiagent")


def same_family(a: str, b: str) -> bool:
    """两个 provider key 是否共用同一套底层适配器（因此同一套提示词方言）。

    自建实例复用内置协议实现（见 ``_build_custom``）：只要两者解析到同一个
    适配器类，提示词方言就完全一样，即使 provider key 字符串不同——例如内置
    ``"hiagent"`` 和一个协议同样声明为 ``seedance`` 的自建实例 ``"custom:xxx"``。
    集级视频模型强绑定（分镜台选择 vs 生成台实际生效供应商）必须按这个比，
    不能按 provider key 原始字符串比：本机部署的历史迁移会把内嵌模型自动包装
    成 ``custom:<id>``（见 app/model_migration.py），字符串比较会让每一集都被
    误判成"绑定不一致"。
    """
    return type(resolve(a)) is type(resolve(b))


def all_adapters() -> list[VideoProviderAdapter]:
    """内置适配器；用于 task_id / 产物 URL 的归属路由。

    自建实例共用内置协议，其 ``owns_*`` 判定与内置实例同源，因此这里只枚举
    内置适配器就够，不必为每个实例各走一遍。
    """
    return [resolve(name) for name in _BUILTIN_FACTORIES]


def _routing_candidates() -> list[VideoProviderAdapter]:
    """归属判定的候选顺序：先当前选中的实例，再内置实例。

    同一协议的多个实例共用 task_id 前缀，只有连接不同；先问当前实例才能让自建
    实例的任务用它自己的 Base URL 去轮询和下载。已知边界：任务在途中把视频模型
    切到另一个同协议实例，轮询会打到新实例——这与切换前的行为一致（历史实现同样
    只认全局 Base URL），要彻底解决需要把实例写进 task_id，那会让在途任务失效。
    """
    from app import hiagent

    candidates: list[VideoProviderAdapter] = []
    active = resolve(hiagent.active_provider("video"))
    candidates.append(active)
    for adapter in all_adapters():
        if adapter is not active:
            candidates.append(adapter)
    return candidates


def adapter_for_task_id(task_id: str) -> VideoProviderAdapter | None:
    """按 task_id 归属路由；没有适配器认领时返回 None。"""
    for adapter in _routing_candidates():
        if adapter.owns_task_id(task_id):
            return adapter
    return None


def adapter_for_output_url(url: str) -> VideoProviderAdapter | None:
    """按产物 URL 归属路由；没有适配器认领时返回 None（走通用公网下载）。"""
    for adapter in _routing_candidates():
        if adapter.owns_output_url(url):
            return adapter
    return None


