"""回归脚本的会话凭证读取——惰性，绝不在导入期读盘。

`data/regression_session_token.txt` 是 gitignore 的本地产物。多个回归脚本曾在
**模块级**直接 `read_text()` 它：

    SESSION = (ROOT / "data" / "regression_session_token.txt").read_text(...).strip()

而 `tests/` 会导入这些脚本（只为取其中的纯判据函数，跟凭证毫无关系）。于是任何
没有该文件的环境——全新 clone、CI、`git worktree add` 出来的干净树——在 pytest
的**收集阶段**就抛 `FileNotFoundError`，得到：

    Interrupted: 5 errors during collection

**整个套件一个测试都不会跑**，而且 16 秒就"跑完"、输出里一个 `FAILED` 都没有，
看起来像通过。2026-08-30 实测踩中：/tmp/wt-verify 里跑全量，5 errors、0 tests。

这比普通的路径硬编码危险，因为它把「什么都没验证」伪装成一次快速通过。

所以凭证一律在**真正要用的时候**读。缺失时给出明确指引，而不是让调用方拿到一个
含义不明的异常。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 回归专用凭证。绝大多数回归脚本只认这一个。
REGRESSION_ONLY = (ROOT / "data" / "regression_session_token.txt",)

#: 额外回退本机进程级共享秘密（后端 MJ_LEGACY_SHARED_SESSION 默认开启，接受该
#: 秘密为系统管理员身份）。只有原本就这么写的脚本才用这一组，不扩大到其它脚本
#: ——那会是未经要求的行为变更。
WITH_LOCAL_SECRET = (
    ROOT / "data" / "regression_session_token.txt",
    ROOT / "data" / "local_session_secret.txt",
)


def session_token(candidates: tuple[Path, ...] = REGRESSION_ONLY) -> str:
    """读取会话凭证；按 ``candidates`` 顺序取第一个存在且非空的。

    调用点必须在函数体内，不要在模块级赋值给常量——那等于把导入期读盘换个写法
    保留下来（见模块 docstring）。
    """
    for path in candidates:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    listed = "、".join(str(p.relative_to(ROOT)) for p in candidates)
    raise SystemExit(
        f"找不到会话凭证：{listed} 均不存在或为空。\n"
        f"回归脚本需要本机后端的登录凭证，请先生成该文件再运行。"
    )
