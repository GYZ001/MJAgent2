from __future__ import annotations

import json
import threading

import pytest

from app import db
from app.rejected_media import _purge_artifact, purge_rejected_media


def test_purge_rejected_media_removes_files_records_and_gallery_refs(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rejected-media.db")
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()

    portrait = tmp_path / "rejected-portrait.jpg"
    profile = tmp_path / "rejected-profile.jpg"
    rejected_keyframe = tmp_path / "rejected-keyframe.jpg"
    valid_keyframe = tmp_path / "valid-keyframe.jpg"
    for path in (portrait, profile, rejected_keyframe, valid_keyframe):
        path.write_bytes(b"\xff\xd8\xff\xd9")

    bible = {
        "characters": [{"name": "Hero", "ref_image_path": str(portrait)}],
        "scenes": [],
    }
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','P',?,1)",
        (json.dumps(bible),),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,created_at) VALUES('e','p',1,1)",
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s','e',1,5)",
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,content_hash,
               parent_artifact_ids_json,model_snapshot_json,file_path,created_at
           ) VALUES('bad','character_portrait','reference_asset','p:Hero:1',1,
                    'approved','T3','hash','[]','{}',?,1)""",
        (str(portrait),),
    )
    conn.execute(
        """INSERT INTO evaluations(
               id,artifact_id,evaluator_type,evaluator_name,evaluator_version,status,
               hard_gate_passed,evaluation_role,runtime_blocking,issues_json,evidence_json,
               dimension_scores_json,created_at
           ) VALUES('ev','bad','model','portrait_qa','1','scored',1,'score_only',0,
                    '[]',?,'{}',1)""",
        (json.dumps({"qa": {"status": "failed", "hard_failures": ["face_mismatch"]}}),),
    )
    conn.execute(
        """INSERT INTO character_portraits(
               id,project_id,character_name,ep_start,ep_end,image_path,artifact_id,
               pack_status,created_at
           ) VALUES('portrait','p','Hero',1,NULL,?,'bad','ready',1)""",
        (str(portrait),),
    )
    conn.execute(
        """INSERT INTO character_portrait_views(
               id,portrait_id,view_role,image_path,status,created_at
           ) VALUES('profile','portrait','profile',?,'ready',1)""",
        (str(profile),),
    )
    image_inputs = {
        "reference_images": [
            {
                "id": "loser",
                "path": str(rejected_keyframe),
                "deleted": True,
                "rejectReason": "best_of_three_not_selected",
            },
            {
                "id": "winner",
                "path": str(valid_keyframe),
                "selectedForSeedance": True,
            },
        ],
    }
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,image_inputs,created_at
           ) VALUES('v','s',1,'prompt','idem',?,1)""",
        (json.dumps(image_inputs),),
    )
    conn.execute(
        """INSERT INTO reference_sets(
               id,shot_id,source_version_id,fingerprint,created_at,updated_at
           ) VALUES('set','s','v','fp',1,1)""",
    )
    conn.execute(
        """INSERT INTO reference_assets(
               id,reference_set_id,asset_type,path,selected,deleted,created_at
           ) VALUES('loser','set','plot_key_frame',?,0,1,1)""",
        (str(rejected_keyframe),),
    )
    conn.execute(
        """INSERT INTO reference_assets(
               id,reference_set_id,asset_type,path,selected,deleted,created_at
           ) VALUES('winner','set','plot_key_frame',?,1,0,1)""",
        (str(valid_keyframe),),
    )
    conn.commit()

    report = purge_rejected_media(conn)

    assert report["artifacts"] == 0
    assert portrait.exists()
    assert profile.exists()
    assert not rejected_keyframe.exists()
    assert valid_keyframe.exists()
    assert conn.execute("SELECT COUNT(*) n FROM artifacts WHERE id='bad'").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) n FROM character_portraits WHERE id='portrait'",
    ).fetchone()["n"] == 1
    refs = json.loads(conn.execute(
        "SELECT image_inputs FROM shot_versions WHERE id='v'",
    ).fetchone()["image_inputs"])["reference_images"]
    assert [ref["id"] for ref in refs] == ["winner"]
    assert conn.execute(
        "SELECT COUNT(*) n FROM reference_assets WHERE id='loser'",
    ).fetchone()["n"] == 0
    updated_bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='p'",
    ).fetchone()["bible_json"])
    assert updated_bible["characters"][0]["ref_image_path"] == str(portrait)


def test_reference_purge_refuses_artifact_in_published_lineage(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "release-lineage.db")
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,created_at) VALUES('p','P',1)",
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,created_at) "
        "VALUES('e','p',1,1)",
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,
               content_json,content_hash,parent_artifact_ids_json,
               model_snapshot_json,created_at
           ) VALUES('reference-parent','character_portrait','reference_asset',
                    'p:Hero:1',1,'rejected','T1','{}','parent-hash','[]','{}',1)""",
    )
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,
               content_json,content_hash,parent_artifact_ids_json,
               model_snapshot_json,created_at
           ) VALUES('published','screenplay_document','episode','e',1,'stale',
                    'T2','{}','published-hash','[\"reference-parent\"]','{}',2)""",
    )
    conn.execute(
        "UPDATE episodes SET published_screenplay_artifact_id='published' "
        "WHERE id='e'",
    )
    conn.commit()

    artifact = conn.execute(
        "SELECT * FROM artifacts WHERE id='reference-parent'",
    ).fetchone()
    with pytest.raises(ValueError, match="禁止物理清理"):
        _purge_artifact(conn, artifact)

    assert conn.execute(
        "SELECT 1 FROM artifacts WHERE id='reference-parent'",
    ).fetchone() is not None
