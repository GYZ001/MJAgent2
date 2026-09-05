"""``projects.bible_json`` 的唯一合法写法：读-改-写 + ``bible_version`` 乐观锁。

人物谱 JSON 是十几处流程共享的一份文档：映射台反应式追加人物/场景、后台出图回写 ref_image_path、
人工编辑改提示词、场景圣经落库……它们过去各自「读整份→改一处→整份写回」，且多数不校验版本。
2026-09-02《神墓》实测：场景圣经任务 01:07:21 整份回写，把 01:07:13/01:07:20 映射台刚追加的两个
场景覆盖掉，本集映射随后两轮失败——这是丢更新（lost update），并发越多越常见。

本模块把写法收口成一个函数：``mutate_bible_json(conn, project_id, mutate)`` 在调用方的连接上
读出 ``bible_json``/``bible_version``，把解析后的 dict 交给 ``mutate`` 原地修改，只有 ``mutate``
返回 True 才写回，写回语句带 ``WHERE COALESCE(bible_version,0)=读到的版本`` 并把版本 +1；
版本被别人推进就重读重做，重试耗尽抛 ``BibleJsonConflict``（ValueError 子类，走命令总线的路由
自动转 409），绝不盲写。不在这里 commit——事务归调用方。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

BIBLE_JSON_CAS_ATTEMPTS = 5


class BibleJsonConflict(ValueError):
    """重试耗尽仍抢不到「读到的版本」：人物谱正被并发修改。"""


def mutate_bible_json(
    conn: Any, project_id: str, mutate: Callable[[dict], bool], *, attempts: int = BIBLE_JSON_CAS_ATTEMPTS,
) -> bool:
    """在 ``conn`` 上以版本乐观锁修改 ``bible_json``；返回是否真的写入了。

    ``mutate(data)`` 原地修改解析后的 dict，返回 True 表示有改动需要写回、False 表示无事可做
    （此时不写、不推进版本）。projects 行不存在或 ``bible_json`` 为空时返回 False。
    ``mutate`` 抛出的异常原样上抛（例如路由层的 404/422），不吞、不写。
    """
    for _attempt in range(max(1, attempts)):
        row = conn.execute(
            "SELECT bible_json, bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if not row or not row["bible_json"]:
            return False
        data = json.loads(row["bible_json"])
        if not mutate(data):
            return False
        expected = int(row["bible_version"] or 0)
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?, bible_version=? WHERE id=? AND COALESCE(bible_version,0)=?",
            (json.dumps(data, ensure_ascii=False), expected + 1, project_id, expected),
        )
        if cursor.rowcount == 1:
            # 2026-09-05 B 实测：这条 UPDATE 不提交就返回，调用方（道具注册、场景别名）接着 await
            # 模型/出图几十秒到几分钟，SQLite 单写锁一直被握着，全站每次写都等 30 秒、事件循环冻结。
            # CAS 写本身就是独立原子动作，写成即提交；调用方再 commit 是空操作。
            conn.commit()
            return True
    raise BibleJsonConflict(f"人物谱正被并发修改（project={project_id}），重试 {attempts} 次仍未写入，请刷新后重试")


__all__ = ["BIBLE_JSON_CAS_ATTEMPTS", "BibleJsonConflict", "mutate_bible_json"]
