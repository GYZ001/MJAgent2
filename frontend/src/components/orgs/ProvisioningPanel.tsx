import { forwardRef, useImperativeHandle, useState } from "react";
import type { UnresolvedAssetsDetail, UserRow } from "../../api";
import { HandoverDialog } from "../HandoverDialog";
import { ImportDialog } from "../ImportDialog";

export interface ProvisioningPanelHandle {
  /** 打开「资产 / 移交」弹窗；`detail` 有值时直接复用（删除命中 409 时响应体
   *  已经带了资产清单，不用再多打一次 GET），否则弹窗自己发请求查。 */
  openAssets: (user: UserRow, detail?: UnresolvedAssetsDetail) => void;
}

/** 批量导入入口按钮 + 「导入/资产移交」两个弹窗的容器，从 MembersTab 拆出来
 *  控制单文件行数（EP-03 第一阶段新增两块入口，MembersTab 已经贴着前端单
 *  文件 300 行上限，见 CLAUDE.md「装不下时先想怎么拆，不要先想加基线」）。
 *  `openAssets` 经 ref 暴露给 MembersTab：账号卡片的「资产 / 移交」按钮与
 *  删除命中 409 时都要能从外部打开同一个弹窗。 */
export const ProvisioningPanel = forwardRef<
  ProvisioningPanelHandle, { busy: boolean; onChanged: () => void }
>(function ProvisioningPanel({ busy, onChanged }, ref) {
  const [importOpen, setImportOpen] = useState(false);
  const [handoverTarget, setHandoverTarget] =
    useState<{ user: UserRow; detail?: UnresolvedAssetsDetail } | null>(null);

  useImperativeHandle(ref, () => ({
    openAssets: (user, detail) => setHandoverTarget({ user, detail }),
  }));

  return (
    <>
      <button type="button" className="btn" disabled={busy} onClick={() => setImportOpen(true)}>批量导入</button>
      {importOpen && <ImportDialog onClose={() => setImportOpen(false)} onImported={onChanged} />}
      {handoverTarget && (
        <HandoverDialog
          user={handoverTarget.user} initialAssets={handoverTarget.detail?.assets}
          onClose={() => setHandoverTarget(null)} onHandedOver={onChanged}
        />
      )}
    </>
  );
});
