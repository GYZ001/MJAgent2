"""定妆照（写实画风）生成侧肤色局部色块规则。

2026-10-04 实测根因（《顾念长安》proj_ca86b15ab7d7「温念」）：定妆照提示词的写实
画风分支（``app.refs.character_visual_style_lock``）只写了「照片级摄影写实质感、
自然肤理」，从未提过"脸上不写局部颜色"这件事；视频装配把这张定妆照（或其头颈
裁切）原样送给 Seedance 作人物参考图，模型自行在双颊/眼皮画出边界清晰的粉红
色块，第 1 集第 5/6/7/9/13/15 段新旧版本反复出现，逐帧审片判 blocker/major，
重抽无法消除。供应商对"图生图/非原始来源"的人物图一律按隐私拒收（见
``app.portraits.headshot_crop`` 模块文档），因此修正只能是"把生成侧提示词改对
+ 生成后核验"（见 ``app.portraits.portrait_skin_blush_check``），不能对已有图
做图生图式修改。

与分镜台 ``app.production.storyboard_skin_blush`` 同一根因、同一判据
（``app.visual_styles.is_photographic_style_prompt``），但规则文案不同：定妆照
是中性姿态的静态全身立绘，不存在"情绪引起的脸红""害羞表演"这类动态场景，
不照搬分镜侧关于眼神/嘴唇/手部替代表演的内容（那些服务于叙事镜头，定妆照
没有叙事动作）。

只挂在 ``app.refs.character_visual_style_lock`` 这一个汇聚点：
``app.refs.portrait_prompt``（初始/重新生成定妆照）、
``app.multiview.character_view_prompt``（多视角包的 front_full/back_full）、
``app.portraits.neutral_identity``（中性定妆照，内部复用 ``portrait_prompt``）
三条生成路径都经过它，改一处即可全覆盖。``face_closeup`` 头像照 2026-10-02 起
已改为从 ``front_full`` 纯像素裁切（``app.portraits.headshot_crop``），裁切
继承 front_full 已有像素，不需要单独追加规则。

非写实画风（国漫电影风/古典水墨风等）不追加本规则——那些画风本身是非写实
渲染，局部色块不会被观众看成"上色错误"，与分镜侧
``skin_blush_dialect_addendum`` 的开关判据同一口径。
"""
from __future__ import annotations

#: 规则版本：改动本规则文案或 ``portrait_skin_blush_check`` 的判定提示词/判据
#: 任一处，都要同步递增；``app.portraits.portrait_skin_audit_store`` 按它 +
#: 图片内容哈希做缓存键，旧版本判过的结果不会被新规则误当作"已核验通过"。
PORTRAIT_SKIN_BLUSH_RULE_VERSION = "portrait_skin_blush_v3"  # v3（2026-10-04）：生成侧规则与加强重画措辞改为纯正面陈述，不再点名颜色词（文生图模型不理解否定）；v2：判定提示词排除嘴唇/眉毛/头发/瞳孔固有色与自然光渐变色温

#: 只写「应该是什么样」，不点名要避开的颜色词：文生图模型不理解否定，提示词里出现
#: 「腮红」「红晕」这类词本身就会提高画出它们的概率（与分镜侧不同——分镜正文给的是
#: 会读懂否定的视频模型提示词）。测试锁住规则文案里不出现这些词。
PORTRAIT_SKIN_BLUSH_RULE = (
    "面部与皮肤（写实摄影画风）：整张脸是干净的素颜状态，脸颊、眼皮、鼻头、额头、"
    "下巴是同一种均匀自然的肤色，只有光线造成的柔和明暗；人物的神情与气色只通过"
    "五官形态、眼神和姿态表现。"
)


def portrait_skin_blush_addendum(*, photographic: bool) -> str:
    """非写实画风返回空串——拼进画风锁定文案后逐字不变；写实画风返回带前导
    句号的规则文案，接线方式与
    ``app.production.storyboard_skin_blush.skin_blush_dialect_addendum`` 同源。"""
    if not photographic:
        return ""
    return "。" + PORTRAIT_SKIN_BLUSH_RULE


__all__ = [
    "PORTRAIT_SKIN_BLUSH_RULE",
    "PORTRAIT_SKIN_BLUSH_RULE_VERSION",
    "portrait_skin_blush_addendum",
]
