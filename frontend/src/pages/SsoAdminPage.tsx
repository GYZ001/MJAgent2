import IdpListSection from "../components/sso/IdpListSection";
import LocalLoginPolicySection from "../components/sso/LocalLoginPolicySection";

/** 身份接入——系统管理员专属入口（EP-02）：IdP 配置 CRUD + 强制 SSO 开关。
 *  两块各自独立请求数据、独立维护忙碌态/错误态，互不干扰（与
 *  AccountAdminPage 的标签页拆分同一思路，只是这里不需要标签切换）。 */
export default function SsoAdminPage() {
  return (
    <div className="account-admin">
      <header className="desk-head">
        <h1>身份接入</h1>
        <p className="sub">企业单点登录（SSO）配置——只有系统管理员能看到这一页。</p>
        <hr className="rule" />
      </header>
      <IdpListSection />
      <LocalLoginPolicySection />
    </div>
  );
}
