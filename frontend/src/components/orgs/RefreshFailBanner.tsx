/** 已有数据后刷新失败的统一横幅。QueryState 的失败分支要求 `error && !hasData`
 *  （见 ../QueryState.tsx）：hasData 为真时它无条件渲染 children、完全不看
 *  error，所以“首屏没数据”交给 QueryState 处理，“已有数据后刷新失败”必须在
 *  QueryState 外单独给出信号 + 重试，不能被悄悄吞掉。MembersTab 的账号/回收站
 *  两个列表共用这一个组件（2026-09-21 f676b198 只改了 TeamsTab/RolesTab/
 *  InvitationsPanel，这两处当时漏改）。 */
export default function RefreshFailBanner({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <div className="empty query-error account-admin-refresh-fail" role="alert">
      <strong>刷新失败</strong>
      <p>{error}</p>
      <button type="button" className="btn small" onClick={onRetry}>重试</button>
    </div>
  );
}
