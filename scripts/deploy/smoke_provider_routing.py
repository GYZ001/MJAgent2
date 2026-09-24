"""部署后只读冒烟（在 B 上跑）：四个职责选路非空 + 文本真实调用 + 图像职责真实鉴权。

2026-09-14 事故：image:default 绑定的模型条目密钥失效整整一周（09-07 起所有出图 401），
映射台/分镜台照常通过（出图解耦到后台，失败只进 warning），直到第 11 集新建场景时才发现。
旧冒烟只对文本做真实调用、对图像只查"选路非空"，查不出这一类。这里对图像条目做一次
GET {base_url}/models 带该条目请求头——零成本、不产生任何生成，401 即密钥失效。

用法（B 上绝对路径）：
    /root/MJAgent2/.venv/bin/python /root/MJAgent2/scripts/deploy/smoke_provider_routing.py
退出码：0 全通过；1 任一职责选路为空 / 文本调用异常 / 图像鉴权失败。不改库、不打印密钥。
"""
from __future__ import annotations

import asyncio
import os
import sys

REPO = os.environ.get("MJAGENT2_REPO") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(REPO)
sys.path.insert(0, REPO)

KINDS = ("text", "vlm", "video", "image")
# 声音生成模型层 2026-09-23 才接入，B 上可能还没配一条：不计入下面的失败判据，
# 否则会拦住每晚的自动部署。仍然打印选路结果，方便人工核对配没配上。
OPTIONAL_KINDS = ("voice",)


def _resolve_all() -> dict[str, tuple[str, str]]:
    from app import hiagent

    resolved: dict[str, tuple[str, str]] = {}
    for kind in KINDS + OPTIONAL_KINDS:
        provider = hiagent.active_provider(kind)
        model = hiagent.active_model(kind, provider) if provider else ""
        resolved[kind] = (provider, model)
        print(f"{kind} -> {provider or '(空)'} / {model or '(空)'}")
    return resolved


async def _text_smoke() -> bool:
    from app import hiagent

    try:
        text = await hiagent.chat(
            [{"role": "user", "content": "只回复一个字：好"}], temperature=0, max_tokens=64,
            call_meta={"kind": "smoke", "purpose": "provider_routing_smoke"},
        )
    except Exception as exc:  # noqa: BLE001 冒烟要把异常原文报出来
        print(f"文本真实调用失败：{type(exc).__name__}: {str(exc)[:200]}")
        return False
    print(f"文本真实调用返回：{text[:20]!r}")
    return bool(text.strip())


async def _image_auth_smoke(provider: str, model: str) -> bool:
    import httpx

    from app import config, hiagent

    base_url, headers = hiagent._model_connection(
        provider, model, config.HIAGENT_BASE_URL, config.HIAGENT_API_KEY,
    )
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(base_url.rstrip("/") + "/models", headers=headers)
    if response.status_code in (401, 403):
        print(f"图像职责鉴权失败：{provider} GET {base_url}/models -> {response.status_code} {response.text[:160]}")
        return False
    print(f"图像职责鉴权通过：{provider} GET /models -> {response.status_code}")
    return True


def main() -> int:
    resolved = _resolve_all()
    ok = all(resolved[kind][0] and resolved[kind][1] for kind in KINDS)
    if not ok:
        print("有职责选路为空")
    voice_provider, voice_model = resolved["voice"]
    if not (voice_provider and voice_model):
        print("声音生成模型未配置（可选职责，不计入失败）")
    ok = asyncio.run(_text_smoke()) and ok
    image_provider, image_model = resolved["image"]
    if image_provider and image_model:
        ok = asyncio.run(_image_auth_smoke(image_provider, image_model)) and ok
    print("SMOKE OK" if ok else "SMOKE FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
