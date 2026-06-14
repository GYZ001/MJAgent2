from app.validators import normalize_action_desc


def test_normalize_action_desc_strips_template_sequence_marker() -> None:
    assert normalize_action_desc("先，齐肩黑发发扎低马尾的曲惜从咖啡厅隔板后探身") == (
        "齐肩黑发发扎低马尾的曲惜从咖啡厅隔板后探身"
    )
    assert normalize_action_desc("首先：谷言从怔神中回过神") == "谷言从怔神中回过神"
    assert normalize_action_desc("先……曲惜笑着上前半步") == "曲惜笑着上前半步"


def test_normalize_action_desc_keeps_real_words() -> None:
    assert normalize_action_desc("先前曲惜已经把纸杯放回桌面") == "先前曲惜已经把纸杯放回桌面"
    assert normalize_action_desc("先生推门而入，谷言抬头") == "先生推门而入，谷言抬头"
