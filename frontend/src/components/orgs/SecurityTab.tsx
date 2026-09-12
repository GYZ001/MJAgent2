import InvitationsPanel from "./InvitationsPanel";
import SecurityPolicySection from "../system/SecurityPolicySection";
import UserSessionsPanel from "../system/UserSessionsPanel";

/** 账号管理——「安全」标签页（EP-03 第二阶段）：邀请链接 + 密码/会话策略 +
 *  活跃会话查看/强制下线。三块各自独立请求数据、独立维护忙碌态/错误态，
 *  与 AccountAdminPage 其余标签页同一思路。 */
export default function SecurityTab() {
  return (
    <>
      <InvitationsPanel />
      <SecurityPolicySection />
      <UserSessionsPanel />
    </>
  );
}
