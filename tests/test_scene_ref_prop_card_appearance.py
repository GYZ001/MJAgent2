"""主场景定场图道具卡外观接线单测（2026-10-06，《顾念长安》真实故障：出租屋
主场景定场图把已有道具卡「鞋柜」画成带抽屉的高木柜，与道具卡登记的「原木色
低矮开放搁板矮柜」互相矛盾，视频模型在两者之间摇摆——根因是 ``app.scenes.
scene_ref_prompt`` 生成提示词时压根没有参照 ``bible.props``）。

本次修复复用 ``app.video_modes.scene_state_views.prop_appearance_notes_for_
description`` 同一套「文本里逐字出现的道具卡」匹配判据——现已搬到
``app.props.text_match``，两处共用单一判据（见该模块 docstring）。
``app.scenes.scene_ref_prompt`` 新增必传关键字参数 ``prop_notes``：命中道具卡
时追加一句「外观按道具卡画」的正面陈述，放在场景描述之后；未命中传空串，提示
词与引入本参数之前逐字一致。

覆盖点：
① ``scene_ref_prompt`` 命中卡名/别名时追加外观陈述；重叠匹配取最长；未命中
  提示词不受扰动；``prop_notes`` 是必传关键字参数，无默认值；
② 四个真实调用点（``app.scenes`` 第 627/933/1625 行附近、
  ``app.domain.projects.detail._effective_scene_prompt``）都把本项目
  ``bible.props`` 接进来参与匹配，不静默传空串；
③ 已有场景图不会因为本次改动被判过期——``scene_ref_exists``/
  ``app.multiview.scene_primary_is_usable`` 只看主图文件是否存在，不读取、不
  比较 ``scene_ref_prompt()`` 的输出文本。

不测试真实供应商往返；``generate_scene_refs`` 端到端用例沿用
tests/test_scene_pack_regeneration_atomic_swap.py 同一套『跳过供应商、只验证
产物与接线』惯例。
"""
from __future__ import annotations

import asyncio
import inspect
import threading

from app import config, db, multiview, scenes
from app.domain.projects import detail as detail_module
from app.props.text_match import prop_notes_for_text
from app.schemas import Bible, Prop, Scene, World

_SCENE_NAME = "温念的出租屋"
_SCENE_CANONICAL = "出租屋玄关角落摆着一个鞋柜，墙面斑驳，暖黄吊灯，陈设简陋"


def _prop(name: str, appearance: str, *, aliases: list[str] | None = None) -> Prop:
    return Prop(name=name, appearance_canonical=appearance, aliases=aliases or [])


# ---------- ① scene_ref_prompt 本身 ----------

def test_prompt_includes_card_appearance_when_name_matched() -> None:
    props = [_prop("鞋柜", "原木色低矮开放搁板矮柜，无抽屉，三层搁板")]
    notes = prop_notes_for_text(_SCENE_CANONICAL, props)

    prompt = scenes.scene_ref_prompt(
        "写实", _SCENE_CANONICAL, scene_name=_SCENE_NAME, aspect_ratio="9:16", prop_notes=notes,
    )

    assert "「鞋柜」" in prompt
    assert "外观（颜色、材质、款式）按道具卡画" in prompt
    assert "原木色低矮开放搁板矮柜，无抽屉，三层搁板" in prompt


def test_prompt_includes_card_matched_by_alias() -> None:
    """卡的正名「出租屋矮柜」没在描述里逐字出现（描述里写的是别名「鞋柜」），
    必须靠别名命中。"""
    props = [_prop("出租屋矮柜", "低矮开放搁板架，原木色", aliases=["鞋柜"])]

    notes = prop_notes_for_text(_SCENE_CANONICAL, props)

    assert "「出租屋矮柜」" in notes
    assert "低矮开放搁板架" in notes
    # 去掉别名后正名完全不在描述里出现，必须变成空字符串，证明上面的命中确实
    # 来自别名而非偶然撞上正名的子串。
    assert prop_notes_for_text(_SCENE_CANONICAL, [_prop("出租屋矮柜", "低矮开放搁板架，原木色")]) == ""


def test_prompt_longest_match_does_not_double_count_shorter_alias_card() -> None:
    """描述里写「顾屿外套」时不能把恰好是子串的「外套」卡也套上——重叠匹配取
    最长，整段只讲一次「顾屿外套」的外观。"""
    description = "顾屿外套搭在椅背上，墙角堆着纸箱。"
    props = [
        _prop("外套", "黑色羊毛外套，圆领"),
        _prop("顾屿外套", "深灰色风衣外套，领口绣着暗纹"),
    ]

    notes = prop_notes_for_text(description, props)

    assert "「顾屿外套」" in notes
    assert "深灰色风衣外套，领口绣着暗纹" in notes
    assert "黑色羊毛外套" not in notes
    assert notes.count("外观（颜色、材质、款式）按道具卡画") == 1


def test_prompt_unchanged_when_no_card_matched() -> None:
    """文本里没提到任何已建卡的道具：``prop_notes`` 算出来是空串，提示词与
    引入本参数之前逐字一致——用 ``normalize_scene_prompt`` 的空分段过滤规则
    作为独立观察点，不依赖重新实现一份旧版 ``scene_ref_prompt`` 来对比。"""
    props = [_prop("凝灵丹", "半透明淡金色药丸，表面有细小光斑")]
    notes = prop_notes_for_text(_SCENE_CANONICAL, props)
    assert notes == ""

    with_literal_empty = scenes.scene_ref_prompt(
        "写实", _SCENE_CANONICAL, scene_name=_SCENE_NAME, aspect_ratio="9:16", prop_notes="",
    )
    with_computed_empty = scenes.scene_ref_prompt(
        "写实", _SCENE_CANONICAL, scene_name=_SCENE_NAME, aspect_ratio="9:16", prop_notes=notes,
    )
    assert with_literal_empty == with_computed_empty
    assert "按道具卡画" not in with_literal_empty


def test_normalize_scene_prompt_unaffected_by_empty_segment() -> None:
    """独立观察点：``normalize_scene_prompt`` 在任意位置插入空字符串分段对
    输出没有任何影响（空分段被过滤）——这是『未命中道具卡时提示词逐字不变』
    的结构性保证，不依赖 ``scene_ref_prompt`` 内部怎么拼。"""
    without = scenes.normalize_scene_prompt("甲段落", "乙段落", "丙段落")
    with_empty_inserted = scenes.normalize_scene_prompt("甲段落", "", "乙段落", "丙段落")
    assert without == with_empty_inserted


def test_prop_notes_is_required_keyword_without_default() -> None:
    """``prop_notes`` 必传、无默认值——漏传必须在调用那一刻就是 TypeError，
    不能静默退化成『没有道具陈述』（CLAUDE.md「可选参数是缺陷的温床」）。"""
    try:
        scenes.scene_ref_prompt("写实", _SCENE_CANONICAL, scene_name=_SCENE_NAME, aspect_ratio="9:16")
    except TypeError:
        pass
    else:
        raise AssertionError("scene_ref_prompt 缺省 prop_notes 时应抛 TypeError")


# ---------- ② 四个调用点都把 bible.props 接进来 ----------

def test_detail_effective_scene_prompt_uses_matched_card_appearance() -> None:
    """调用点 4：``app.domain.projects.detail._effective_scene_prompt``（GET
    /projects/{id} 的 ``scene_prompt_effective`` 展示值）接的是校验过的
    ``Prop`` 对象列表，不是静默传空串。"""
    props = [_prop("鞋柜", "原木色低矮开放搁板矮柜，无抽屉")]
    scene = {"name": _SCENE_NAME, "scene_canonical": _SCENE_CANONICAL}

    result = detail_module._effective_scene_prompt("写实", scene, props, "9:16")

    assert "原木色低矮开放搁板矮柜" in result
    # override 优先于重算，且 override 存在时完全不触发匹配/外观陈述。
    overridden = detail_module._effective_scene_prompt(
        "写实", {**scene, "scene_prompt_override": "人工编辑的提示词"}, props, "9:16",
    )
    assert overridden == "人工编辑的提示词"


def test_scene_ref_prompt_internal_call_sites_pass_computed_prop_notes() -> None:
    """调用点 1-3（源码断言，见模块 docstring 列出的三个函数）：``app.scenes``
    里对 ``scene_ref_prompt`` 的每一处调用都带非空字面量的 ``prop_notes``/
    ``notes`` 关键字，不是硬编码空串——拿不到真实 props 就必须显式传参，不得
    静默退化。"""
    sources = {
        "_generate_one_scene_reference": inspect.getsource(scenes._generate_one_scene_reference),
        "_generate_and_register_scene": inspect.getsource(scenes._generate_and_register_scene),
        "_refresh_scene_on_state_change": inspect.getsource(scenes._refresh_scene_on_state_change),
    }
    for name, src in sources.items():
        assert "scene_ref_prompt(" in src, f"{name} 应该仍在调用 scene_ref_prompt"
    # _generate_one_scene_reference / _generate_and_register_scene 各自算好
    # prop_notes_for_text(...) 再传给 scene_ref_prompt；没有一处硬编码空串。
    assert 'prop_notes=""' not in sources["_generate_one_scene_reference"]
    assert "prop_notes_for_text(" in sources["_generate_one_scene_reference"]
    assert 'prop_notes=""' not in sources["_generate_and_register_scene"]
    # _refresh_scene_on_state_change 接的是调用方已经算好的 notes 字符串（见
    # 其 docstring），函数体内用的就是这个参数，不是空字面量。
    assert "prop_notes=notes" in sources["_refresh_scene_on_state_change"]

    generate_one_src = sources["_generate_one_scene_reference"]
    ensure_src = inspect.getsource(scenes.ensure_scenes_for_storyboard)
    generate_refs_src = inspect.getsource(scenes.generate_scene_refs)
    # 两处调用 _refresh_scene_on_state_change 的地方都显式算了 notes，不是
    # 传空串；generate_scene_refs 把 bible.props 显式传给了
    # _generate_one_scene_reference。
    assert "notes=prop_notes_for_text(" in generate_one_src
    assert "notes=prop_notes_for_text(" in ensure_src
    assert "bible.props" in generate_refs_src


def test_generate_scene_refs_passes_real_props_to_scene_ref_prompt(tmp_path, monkeypatch) -> None:
    """调用点 1 端到端：``generate_scene_refs`` → ``_generate_one_scene_reference``
    → ``scene_ref_prompt``，真实命中卡时 ``prop_notes`` 非空且带着卡外观文字；
    monkeypatch 住 ``scene_ref_prompt`` 本身只为了在不真的出图的情况下捕获它
    被调用时的关键字参数，不替换其判据逻辑。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-ref-props.db")
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    conn = db.get_conn()

    bible = Bible(
        world=World(visual_style_canonical="cinematic animation"),
        characters=[],
        scenes=[Scene(name=_SCENE_NAME, scene_canonical=_SCENE_CANONICAL)],
        props=[Prop(name="鞋柜", appearance_canonical="原木色低矮开放搁板矮柜，无抽屉")],
    )
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at, aspect_ratio) "
        "VALUES('proj_sr', 'SceneRefProps', 'bible_ready', ?, 1, 1, '9:16')",
        (bible.model_dump_json(),),
    )
    conn.commit()

    captured: list[dict] = []
    real_scene_ref_prompt = scenes.scene_ref_prompt

    def spy_scene_ref_prompt(*args, **kwargs):
        captured.append(kwargs)
        return real_scene_ref_prompt(*args, **kwargs)

    async def fake_generate_image(*_args, **_kwargs):
        return {"b64_json": "YnJhbmQtbmV3"}

    def fake_record_reference_asset(**kwargs):
        return {"id": "art_1", "status": "approved", "file_path": kwargs["file_path"]}

    monkeypatch.setattr(scenes, "scene_ref_prompt", spy_scene_ref_prompt)
    monkeypatch.setattr(scenes, "record_reference_asset", fake_record_reference_asset)
    monkeypatch.setattr(scenes.hiagent, "generate_image", fake_generate_image)
    monkeypatch.setattr(multiview, "scene_multiview_enabled", lambda: False)

    asyncio.run(scenes.generate_scene_refs("proj_sr", only_scene=[_SCENE_NAME]))

    assert captured, "scene_ref_prompt 应该被真实调用过一次"
    prop_notes = captured[0]["prop_notes"]
    assert "「鞋柜」" in prop_notes
    assert "原木色低矮开放搁板矮柜，无抽屉" in prop_notes

    row = conn.execute(
        "SELECT image_path FROM scene_references WHERE project_id='proj_sr' AND scene_name=?",
        (_SCENE_NAME,),
    ).fetchone()
    assert row is not None and row["image_path"], "场景图应该已经落盘登记"


# ---------- ③ 已有场景图不会因本次改动被判过期 ----------

def test_existing_scene_image_stays_usable_after_prompt_computation_changes(tmp_path, monkeypatch) -> None:
    """已有场景图的『是否可用』判据只有 ``scene_ref_exists``/
    ``app.multiview.scene_primary_is_usable`` 两处，二者都只看 ``image_path``
    文件是否真的存在，完全不读取、不比较 ``scene_ref_prompt()`` 的输出文本
    （见两者各自 docstring：『用户拍板 2026-09-01，有图就是可用』）。本用例
    种一行『按改动前的旧提示词生成』的存量场景图（``prompt`` 列存的是不含任何
    道具陈述的旧文本），验证它在改动后仍然被判定为存在/可用——不会因为
    ``scene_canonical`` 现在会算出一个不同的 ``prop_notes`` 而被判过期。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-stale.db")
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,bible_version,created_at,aspect_ratio) "
        "VALUES('proj_old','Old','bible_ready','{}',1,1,'9:16')",
    )
    conn.commit()
    old_image = tmp_path / "old-main.jpg"
    old_image.write_bytes(b"old-main-image")
    # 旧提示词：引入 prop_notes 之前的样子，不含任何道具卡外观陈述。
    old_prompt = scenes.normalize_scene_prompt(
        f"场景定场图（纯环境、画面中不出现任何人物）：{_SCENE_CANONICAL}",
    )
    scene_id = scenes.register_initial_scene_ref(
        conn, "proj_old", _SCENE_NAME, str(old_image), _SCENE_CANONICAL, old_prompt, {}, 1,
    )

    # 改动后对同一段 scene_canonical 重新算出的 prop_notes 非空、与存量
    # prompt 完全不同——如果过期判定挂在"当前提示词 != 存量 prompt"上，这里
    # 就会被判过期；实际判据只认文件是否存在，所以必须仍然可用。
    fresh_notes = prop_notes_for_text(
        _SCENE_CANONICAL, [Prop(name="鞋柜", appearance_canonical="原木色低矮开放搁板矮柜")],
    )
    assert fresh_notes and fresh_notes not in old_prompt

    assert scenes.scene_ref_exists(conn, "proj_old", _SCENE_NAME) is True

    row = conn.execute("SELECT * FROM scene_references WHERE id=?", (scene_id,)).fetchone()
    assert multiview.scene_primary_is_usable(row, []) is True


def test_scene_ref_prompt_has_no_other_consumers_that_could_gate_staleness() -> None:
    """全仓回归守卫：``scene_ref_prompt`` 只有本文件已知的四个调用点（加自身
    定义），没有第五处悄悄把它接进某个过期/校验闸门——否则本次改动可能在未知
    位置让存量场景图或视频被判过期。"""
    import pathlib
    import re

    known_files = {
        "app/scenes.py",
        "app/domain/projects/detail.py",
    }
    hits: set[str] = set()
    for path in pathlib.Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # 只认「调用/定义」形态（紧跟左括号），排除 docstring/注释里的纯文字提及
        # （如 app.props.text_match、app.schemas.world 的背景说明段落）。
        if re.search(r"\bscene_ref_prompt\(", text):
            hits.add(str(path.relative_to(".")).replace("\\", "/"))
    assert hits == known_files, f"scene_ref_prompt 出现在未预期的文件：{hits - known_files}"
