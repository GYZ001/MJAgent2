"""后台阶段结束后，连播台保留实际失败原因，不被通用完成判据吞掉。"""
import pytest

from app.domain.series_ops import stages
from tests.conftest import patch_api_everywhere
from test_series_film_video_wait import _conn


@pytest.mark.asyncio
@pytest.mark.parametrize('stage,error_column', [('screenplay', 'screenplay_error'), ('storyboard', 'script_error')])
@pytest.mark.parametrize('error', ['', '必保引用 Q02 与原文段号不一致（ERR-示例）'])
async def test_background_stage_returns_its_actual_error_after_task_exit(monkeypatch, stage, error_column, error):
    conn = _conn(None)
    monkeypatch.setattr(stages, 'get_conn', lambda: conn)
    monkeypatch.setattr(stages.task_registry, 'active', lambda *_: False)
    started = []

    async def start(episode_id, body):
        started.append((episode_id, body))
        conn.execute(f'UPDATE episodes SET {error_column}=? WHERE id=?', (error, episode_id))

    patch_api_everywhere(monkeypatch, f'start_{stage}', start)
    patch_api_everywhere(monkeypatch, 'storyboard_start_preflight', lambda *_: {'preview_token': 'preview'})
    runner = stages._run_screenplay if stage == 'screenplay' else stages._run_storyboard
    if error:
        with pytest.raises(RuntimeError) as failure:
            await runner('e')
        assert str(failure.value) == error
    else:
        await runner('e')
    assert started == [('e', {'preflight_token': 'preview'} if stage == 'storyboard' else {})]


def test_old_error_from_other_stage_does_not_fail_current_stage(monkeypatch):
    conn = _conn(None)
    conn.execute("UPDATE episodes SET screenplay_error='旧映射错误',script_error='' WHERE id='e'")
    monkeypatch.setattr(stages, 'get_conn', lambda: conn)
    stages._raise_stage_error('e', 'script_error')
