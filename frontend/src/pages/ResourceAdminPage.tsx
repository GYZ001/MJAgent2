import { useEffect, useState } from "react";
import { ApiError, getCurrentOrg, type CurrentOrgInfo } from "../api";
import AlertBanner from "../components/resources/AlertBanner";
import AllocationsSection from "../components/resources/AllocationsSection";
import TopRankingSection from "../components/resources/TopRankingSection";
import "../styles/ResourceAdminPage.css";

/** 资源治理看板（EP-04 第二阶段）——系统管理员专属入口：预警横幅 + 配额分
 *  配（含逐维度用量条）+ 用量排行。数据源全部是第一阶段已有的
 *  /api/system/usage/* 与 /api/system/quota/allocations（EP-04 §7），本页不
 *  新建统计口径，只做展示与编辑入口。
 *
 *  组织范围：组织管理员自动用自己所在组织；系统管理员没有归属组织时必须手
 *  动填一个 org_id（后端 `_resolve_org_id` 同样要求系统管理员显式传参，界
 *  面如实反映这条约束，不假装能自动猜出"当前组织"）。 */
export default function ResourceAdminPage() {
  const [orgInfo, setOrgInfo] = useState<CurrentOrgInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [manualOrgId, setManualOrgId] = useState("");

  useEffect(() => {
    getCurrentOrg()
      .then(setOrgInfo)
      .catch((err) => setError(err instanceof ApiError ? err.message : "加载组织信息失败"));
  }, []);

  const orgId = orgInfo?.org?.id ?? (manualOrgId.trim() || null);

  return (
    <div className="account-admin">
      <header className="desk-head">
        <h1>资源</h1>
        <p className="sub">用量、趋势与配额分配——只有系统管理员/组织管理员能看到这一页。</p>
        <hr className="rule" />
      </header>
      {error && <p className="field-error" role="alert">{error}</p>}
      {orgInfo && !orgInfo.org && (
        <div className="resource-admin-toolbar">
          <label>
            组织 ID（你是系统管理员，未归属任何组织，需要手动指定要查看的组织）
            <input value={manualOrgId} onChange={(e) => setManualOrgId(e.target.value)} placeholder="org_..." />
          </label>
        </div>
      )}
      <AlertBanner orgId={orgId} />
      {orgId ? (
        <>
          <AllocationsSection orgId={orgId} />
          <TopRankingSection />
        </>
      ) : (
        orgInfo && <p className="hint">请先指定组织 ID。</p>
      )}
    </div>
  );
}
