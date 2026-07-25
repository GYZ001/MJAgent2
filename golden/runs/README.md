# Renderability 金样运行留档

## 基线
- `golden/renderability/doupocangqiong_ep1_baseline.json`：旧合同《斗破》第 1 集约 24 镜 / 144s

## 打分
```bash
.\.venv\Scripts\python.exe scripts/score_renderability.py --episode-id <id> --out golden/runs/YYYY-MM-DD_ep1.json
```

对照指标见 PRD §4.2；`shot_count_le_70pct_baseline` 为新合同镜数目标。
