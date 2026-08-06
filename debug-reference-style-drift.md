# Debug Session: reference-style-drift
- **Status**: [OPEN]
- **Issue**: 同一集参考图/关键帧出现真人风格与漫画风格混用，违反统一画风约束。
- **Debug Server**: pending
- **Log File**: .dbg/trae-debug-log-reference-style-drift.ndjson

## Reproduction Steps
1. 打开生成台/素材库，查看同一镜头或同一集不同镜头的参考图。
2. 观察是否同时出现真人写实与漫画风格素材。
3. 对照人物谱中的统一画面风格，确认是否存在偏离。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | 参考图提示词生成时没有稳定注入 `visual_style_canonical`，导致部分任务回退到模型默认写实风格 | High | Low | Pending |
| B | 参考图来源混用了“人物定妆照/场景参考图/剧情关键帧”，其中某一路没有画风硬约束 | High | Medium | Pending |
| C | 旧版本参考图在清理或复用时没有按风格合同过滤，被新版本继续带入 | Medium | Medium | Pending |
| D | 前端把“关键帧/质检依据/实际参考图”混排显示，造成看起来像参考图风格漂移 | Medium | Low | Pending |
| E | 参考图生成模型或供应商参数在不同路径上不一致，某一路走了偏写实配置 | Medium | Medium | Pending |

## Log Evidence
- Pending

## Verification Conclusion
- Pending
