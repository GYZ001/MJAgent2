"""EP-03 第一阶段 REST 路由：CSV 批量导入 + 离职移交（L5）。

层号如实按依赖落：本文件只被 ``app.main`` 引用，与 ``app.auth.admin_api``/
``app.orgs.api`` 同一种"路由→域"入口角色，同一条理由判 L5（见
``app.domain.account_deletion`` 模块文档那一段解释）。

全部端点要求 ``require_system_admin``（系统管理员），与
``app.auth.admin_api`` 现有的账号运维端点同一鉴权模型——批量导入/离职移交
都是账号运维操作，不是制作领域命令，不经 Command Bus，因此在
``app/capabilities/exemptions.py`` 里登记了豁免（预检/确认提交两条 POST）。

``DELETE /api/system/users/{user_id}`` 本身不在这里——它是
``app.auth.admin_api.delete_user`` 的既有路由，本次只在那个函数体里插入
``app.provisioning.handover.has_unresolved_assets()`` 前置校验，不新建路由
（避免同一个 URL 出现两个入口）。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, HTTPException, Response, UploadFile

from app.auth.deps import require_system_admin
from app.auth.principal import Principal, current_actor_name
from app.orgs import store as orgs_store
from app.provisioning import handover as handover_domain
from app.provisioning import importer
from app.provisioning import invitations as invitations_domain

router = APIRouter(prefix="/api/system", tags=["provisioning"])

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _actor_org_id(actor: Principal) -> str:
    return actor.org_id or orgs_store.ORG_DEFAULT_ID


@router.post("/users/import/preview", dependencies=[Depends(require_system_admin)])
async def import_preview(file: UploadFile = File(...), actor: Principal = Depends(require_system_admin)):
    """三段式导入的第一步：逐行判定，返回 ``batch_id`` + 报告，**不写库**
    （``users``/``teams`` 等业务表零改动，只落一条批次台账）。
    """
    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        limit_mb = _MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(413, f"CSV 文件超过 {limit_mb} MB，请拆分后重新上传")
    if not raw:
        raise HTTPException(422, "CSV 文件为空")
    return importer.preview_batch(
        raw=raw, filename=file.filename or "import.csv",
        org_id=_actor_org_id(actor), created_by=current_actor_name(),
    )


@router.post("/users/import/{batch_id}/apply", dependencies=[Depends(require_system_admin)])
def import_apply(batch_id: str, actor: Principal = Depends(require_system_admin)):
    """第二步：只应用预检通过的行；随机初始口令仅在本次响应里出现一次。"""
    return importer.apply_batch(batch_id=batch_id, created_by=current_actor_name())


@router.get("/users/import/{batch_id}/report", dependencies=[Depends(require_system_admin)])
def import_report(batch_id: str):
    """第三步：CSV 下载。同一批次的初始口令列只在第一次下载时可见，见
    ``app.provisioning.importer.get_report_csv`` 模块文档。
    """
    filename, csv_text = importer.get_report_csv(batch_id)
    return Response(
        content=csv_text.encode("utf-8-sig"),  # 带 BOM：国内 Excel 直接双击打开不乱码
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/users/{user_id}/assets", dependencies=[Depends(require_system_admin)])
def get_user_assets(user_id: str):
    """离职前置检查：名下项目数/在途任务数/占用存储/团队授权，供管理员决定
    怎么移交（EP-03 §5）。
    """
    return handover_domain.list_user_assets(user_id)


@router.post("/users/{user_id}/handover", dependencies=[Depends(require_system_admin)])
def post_user_handover(user_id: str, body: dict = Body(...)):
    """把 ``user_id`` 名下全部活跃项目移交给 ``to_user_id`` 或 ``to_team_id``
    （二选一），连带迁移 project_grants。移交后 ``DELETE /users/{user_id}``
    才可能放行——具体判据见 ``app.provisioning.handover.has_unresolved_assets``。
    """
    to_user_id = body.get("to_user_id") or None
    to_team_id = body.get("to_team_id") or None
    return handover_domain.handover(
        from_user_id=user_id, to_user_id=to_user_id, to_team_id=to_team_id,
        created_by=current_actor_name(),
    )


@router.post("/invitations", dependencies=[Depends(require_system_admin)])
def create_invitation(body: dict = Body(...), actor: Principal = Depends(require_system_admin)):
    """EP-03 §6：签发一次性邀请链接。返回体带明文 token，只这一次——前端必须
    在这次响应里把完整的 ``/invite/{token}`` 链接展示给管理员复制走。"""
    return invitations_domain.create_invitation(
        org_id=_actor_org_id(actor), username=str(body.get("username") or ""),
        display_name=str(body.get("display_name") or ""), email=str(body.get("email") or ""),
        team_id=body.get("team_id") or None, role_id=body.get("role_id") or None,
        created_by=current_actor_name(),
    )


@router.get("/invitations", dependencies=[Depends(require_system_admin)])
def list_invitations(actor: Principal = Depends(require_system_admin)):
    """本组织全部邀请（不含 token 明文/哈希），供管理台展示状态与撤销入口。"""
    return {"items": invitations_domain.list_invitations(_actor_org_id(actor))}


@router.post("/invitations/{invitation_id}/revoke", dependencies=[Depends(require_system_admin)])
def revoke_invitation(invitation_id: str):
    """撤销一枚尚未被接受的邀请；已接受的邀请拒绝撤销（409），已撤销的幂等
    返回当前状态。"""
    return invitations_domain.revoke_invitation(invitation_id, revoked_by=current_actor_name())
