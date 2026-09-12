import { useState } from "react";
import MembersTab from "../components/orgs/MembersTab";
import TeamsTab from "../components/orgs/TeamsTab";
import RolesTab from "../components/orgs/RolesTab";
import "../styles/AccountAdminPage.css";

type AdminTab = "members" | "teams" | "roles";

/** 账号管理——系统管理员专属入口，三个标签页：
 *  - 成员：账号开户/启停/删除/加量包（原 AccountAdminPage 全部内容，见 MembersTab）
 *  - 团队：EP-01 组织协作，建团队 + 团队成员/角色管理（见 TeamsTab）
 *  - 角色：EP-01 自定义角色，内置模板只读 + 自定义角色创建/编辑/删除（见 RolesTab）
 *  三个标签各自独立请求数据、独立维护自己的忙碌态/错误态，互不干扰——本文件
 *  只做标签切换，不持有任何业务状态。 */
export default function AccountAdminPage() {
  const [tab, setTab] = useState<AdminTab>("members");

  return (
    <div className="account-admin">
      <header className="desk-head">
        <h1>账号管理</h1>
        <p className="sub">开户、启停账号、组织协作与自定义角色——只有系统管理员能看到这一页。</p>
        <hr className="rule" />
      </header>

      <div className="account-admin-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === "members"}
          className={`btn small ${tab === "members" ? "primary" : "ghost"}`} onClick={() => setTab("members")}>
          成员
        </button>
        <button type="button" role="tab" aria-selected={tab === "teams"}
          className={`btn small ${tab === "teams" ? "primary" : "ghost"}`} onClick={() => setTab("teams")}>
          团队
        </button>
        <button type="button" role="tab" aria-selected={tab === "roles"}
          className={`btn small ${tab === "roles" ? "primary" : "ghost"}`} onClick={() => setTab("roles")}>
          角色
        </button>
      </div>

      {tab === "members" && <MembersTab />}
      {tab === "teams" && <TeamsTab />}
      {tab === "roles" && <RolesTab />}
    </div>
  );
}
