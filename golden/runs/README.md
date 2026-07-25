# Renderability / Supervisor 金样运行留档

## 基线
- `golden/renderability/doupocangqiong_ep1_baseline.json`：旧合同《斗破》第 1 集约 24 镜 / 144s

## Supervisor Golden（《陨落的天才》第 1 集）
```bash
set MANJU_GOLDEN_LIVE=1
set MANJU_GOLDEN_EPISODE_ID=ep_23517af4b5a8
.\.venv\Scripts\python.exe scripts/run_golden_storyboard.py
```

或：
```bash
set MANJU_GOLDEN_LIVE=1
.\.venv\Scripts\python.exe -m pytest tests/test_golden_storyboard_e2e.py -m golden -s
```

产出：`golden/runs/YYYY-MM-DD_yunluo_ep1_<suffix>.json`（含 Supervisor phase/outcome + renderability 对照）。
要求：`auto_confirm` 成功、不启动付费视频。

## 仅打分
```bash
.\.venv\Scripts\python.exe scripts/score_renderability.py --episode-id <id> --out golden/runs/YYYY-MM-DD_ep1.json
```

对照指标见 PRD §4.2；`shot_count_le_70pct_baseline` 为新合同镜数目标。
