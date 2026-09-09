"""连播第 3/9/10 集：不连续原文引用、缺失单元范围和错位证据段号。"""
from copy import deepcopy
import json
from pathlib import Path

from app.domain.video_ops.source_coverage import storyboard_source_coverage_gap
from app.production.prep_pack.provenance_repair import verify_manifest_provenance_with_repair
from app.production.storyboard_beat_sheet import _AiSegmentPlan
from app.production.storyboard_beat_sheet import _AiBeatSheetDraft, _validate_beat_sheet_draft
from app.production.storyboard_capacity_normalize import normalize_and_assert_capacity
from app.production.storyboard_dialogue_ledger import DialogueQuote
from app.production.storyboard_beat_sheet_repair import fill_single_owner_ranges
from app.production.storyboard_segment_ranges import segment_unit_range_errors
from app.production.storyboard_source_spans import segment_source_bindings, source_binding_is_current
from app.source_chapters import _episode_source_blocks
from app.source_excerpt import SourceSegment, index_source_segments
from test_storyboard_source_coverage_gate import _add_shot, _conn


def _source_fixture():
    content = '甲走进屋里。\n\n乙叫住了他。\n\n丙挥手道别。'
    conn = _conn(content)
    source = dict(conn.execute('SELECT id,idx,title,content FROM chapters').fetchone())
    text, _ = _episode_source_blocks([source])
    segments = index_source_segments(text)
    spans = segment_source_bindings({'source_segment_indexes': [1, 3]}, segments=segments,
                                    full_source_text=text, authorized_sources=[source])
    assert len(spans) == 2
    assert spans[0]['end_offset'] < spans[1]['start_offset']
    assert all(source_binding_is_current(span, source) for span in spans)
    return conn, source, text, segments, spans


def _bind(conn, shot_id, number, span, segment=None):
    _add_shot(conn, shot_id, number, (span['start_offset'], span['end_offset']))
    conn.execute('UPDATE storyboard_source_bindings SET source_version_hash=?, excerpt_hash=? WHERE shot_id=?',
                 (span['source_version_hash'], span['excerpt_hash'], shot_id))
    if segment is not None:
        conn.execute('UPDATE shots SET shot_contract_json=? WHERE id=?',
                     (json.dumps({'storyboard_pack_segment': segment}, ensure_ascii=False), shot_id))
    conn.commit()


def test_legacy_noncontiguous_excerpt_recovers_actual_ranges_without_filling_middle():
    conn, source, text, segments, spans = _source_fixture()
    _bind(conn, 's1', 1, spans[0])
    second = segment_source_bindings({'source_segment_indexes': [2]}, segments=segments,
                                    full_source_text=text, authorized_sources=[source])[0]
    _bind(conn, 's2', 2, second)
    assert '丙挥手道别' in storyboard_source_coverage_gap(conn, 'e')
    conn.execute('UPDATE shots SET shot_contract_json=? WHERE id=?',
                 (json.dumps({'storyboard_pack_segment': {'source_segment_indexes': [1, 3], 'prompt_text': '甲进屋，丙挥手。'}}), 's1'))
    assert storyboard_source_coverage_gap(conn, 'e') is None
    conn.execute('DELETE FROM shots WHERE id=?', ('s2',))
    gap = storyboard_source_coverage_gap(conn, 'e')
    assert gap and '乙叫住了他' in gap
    assert '丙挥手道别' not in gap


def test_persisted_spans_require_current_source_and_excerpt_hashes():
    conn, _source, _text, _segments, spans = _source_fixture()
    segment = {'source_segment_indexes': [1, 3], 'prompt_text': '甲进屋，丙挥手。', 'source_bindings': spans}
    _bind(conn, 's1', 1, spans[0], segment)
    gap = storyboard_source_coverage_gap(conn, 'e')
    assert '乙叫住了他' in gap and '丙挥手道别' not in gap
    broken = deepcopy(segment)
    broken['source_bindings'][1]['excerpt_hash'] = 'forged'
    conn.execute('UPDATE shots SET shot_contract_json=? WHERE id=?',
                 (json.dumps({'storyboard_pack_segment': broken}), 's1'))
    assert '丙挥手道别' in storyboard_source_coverage_gap(conn, 'e')


def test_changed_chapter_does_not_rederive_old_identity_source_ranges():
    conn, _source, _text, _segments, spans = _source_fixture()
    _bind(conn, 's1', 1, spans[0], {'source_segment_indexes': [1, 3], 'prompt_text': '甲进屋，丙挥手。'})
    conn.execute('UPDATE chapters SET content=replace(content,?,?)', ('丙挥手道别', '丁突然闯入'))
    assert '丁突然闯入' in storyboard_source_coverage_gap(conn, 'e')


def test_selected_units_do_not_claim_unselected_parts_of_paragraph():
    source = {'id': 1, 'idx': 1, 'title': '开场', 'content': '甲开门。乙关窗。丙离开。'}
    text, _ = _episode_source_blocks([source])
    segments = index_source_segments(text)
    # 标题单独占一个行单元；定位正文中间一句，避免依赖标题的句单元编号。
    from app.production.storyboard_segment_ranges import split_source_units
    units = split_source_units(segments[0].text)
    number = next(i for i, (a, b) in enumerate(units, 1) if segments[0].text[a:b] == '乙关窗。')
    span = segment_source_bindings({'source_segment_indexes': [1], 'source_unit_ranges': [
        {'source_segment_index': 1, 'from_unit': number, 'to_unit': number},
    ]}, segments=segments, full_source_text=text, authorized_sources=[source])[0]
    assert source['content'][span['start_offset']:span['end_offset']] == '乙关窗。'


def test_single_owner_missing_ranges_recover_all_sources_in_one_pass():
    sources = [SourceSegment(str(i), '第一句。第二句。', 0, 8) for i in range(58)]
    plan = _AiSegmentPlan(segment_no=3, synopsis='完整场戏', source_segment_indexes=list(range(1, 59)))
    notes = fill_single_owner_ranges([plan], {i: 2 for i in range(1, 59)}, set())
    assert len(notes) == 58
    assert segment_unit_range_errors([plan], sources, set()) == []
    assert fill_single_owner_ranges([plan], {i: 2 for i in range(1, 59)}, set()) == []


def test_shared_source_without_ranges_is_not_arbitrarily_assigned():
    plans = [_AiSegmentPlan(segment_no=i, synopsis='相邻镜头', source_segment_indexes=[1]) for i in (1, 2)]
    assert fill_single_owner_ranges(plans, {1: 3}, set()) == []
    assert all(not plan.source_unit_ranges for plan in plans)


def test_real_episode_nine_repairs_missing_ranges_and_splits_in_source_order():
    fixture = json.loads((Path(__file__).parent / 'fixtures/series_beat_failure_20260909.json').read_text())
    sources = index_source_segments(fixture['source_text'])
    paratext = set(fixture['paratext_indexes'])
    quotes = [DialogueQuote.model_validate(q) for q in fixture['dialogue_targets']]
    draft = _AiBeatSheetDraft.model_validate(fixture['draft'])
    assert segment_unit_range_errors(draft.segments, sources, paratext)
    assert _validate_beat_sheet_draft(draft, source_segments=sources, dialogue_quotes=quotes, paratext_indexes=paratext) == []
    assert normalize_and_assert_capacity(draft, quotes, source_segments=sources, paratext_indexes=paratext)
    assert segment_unit_range_errors(draft.segments, sources, paratext) == []
    indexes = [i for plan in draft.segments for i in plan.source_segment_indexes]
    assert indexes == sorted(indexes)
    assert set(indexes) == set(range(1, len(sources) + 1)) - paratext
    assert {q.quote_id for q in draft.kept_lines} | {q.quote_id for q in draft.dropped_lines} == {q.quote_id for q in quotes}


def test_misnumbered_character_anchor_is_relocated_only_on_unique_verbatim_evidence():
    phrase = '走在前方的白衣男子温和的开口，他正是王腾飞。'
    sources = [SourceSegment(str(i), '其它剧情。', 0, 5) for i in range(19)]
    sources[18] = SourceSegment('19', phrase, 0, len(phrase))
    original = {'characters': [{'display_name': '王腾飞', 'provenance': {
        'method': 'resolution', 'anchor_phrase': phrase, 'anchor_segments': [12],
    }}]}
    manifest = deepcopy(original)
    assert verify_manifest_provenance_with_repair(sources, manifest) == []
    proof = manifest['characters'][0]['provenance']
    assert proof['anchor_segments'] == [19] and proof['anchor_phrase'] == phrase
    sources[17] = sources[18]
    ambiguous = deepcopy(original)
    assert verify_manifest_provenance_with_repair(sources, ambiguous)
    assert ambiguous == original
    invented = deepcopy(original)
    invented['characters'][0]['provenance']['anchor_phrase'] = '原文没有出现过的证据。'
    assert verify_manifest_provenance_with_repair(sources, invented)
