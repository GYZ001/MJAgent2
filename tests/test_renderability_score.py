"""Renderability 金样打分单测。"""
from app.renderability_score import score_renderability_sample


def test_score_against_legacy_24_shot_baseline() -> None:
    screenplay = {
        "plot_spine": {
            "episode_premise": "甲一要在测验中守住最后的尊严",
            "spine_beats": [
                {"beat_id": f"S0{i}", "who": "甲一", "does": "推进测验主线", "turn": "局势变化", "must_keep": True}
                for i in range(1, 7)
            ],
            "must_keep_ending": "测验结果当场收束",
            "drop_list": ["路人多轮起哄", "服饰材质描写"],
        },
        "key_lines": ["三段？", "我不会一直这样。", "二儿相信你。"],
        "full_script_text": "甲一走向石碑并抬手贴上碑面。",
    }
    storyboard = {
        "shots": [
            {
                "shot_no": i,
                "duration_s": 5,
                "action_desc": "甲一推进测验主线并站定",
                "first_frame_desc": "甲一面向石碑",
                "last_frame_desc": "甲一手掌贴碑",
            }
            for i in range(1, 13)
        ]
    }
    baseline = {"label": "legacy", "shot_count": 24, "total_duration_s": 144}
    result = score_renderability_sample(
        screenplay=screenplay, storyboard=storyboard, baseline=baseline
    )
    assert result["metrics"]["shot_count"] == 12
    assert "shot_count_in_soft_budget" not in result["gates"]
    assert result["vs_baseline"]["shot_count_le_70pct_baseline"] is True
    assert result["metrics"]["contract_version"] == "renderability_v1"
