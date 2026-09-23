import OperationError from './OperationError'

/** 已有数据后台刷新失败的统一提示：用于 useProject/useEpisode/usePoll 等详情/
 *  列表页——首次加载失败由页面自己的早退 `<QueryState hasData={false}>` 分支
 *  处理，但那个分支只在数据从未取到过时才会执行；数据已经到手后，后台轮询/
 *  手动重试再失败，QueryState 完全不看 error（hasData 为真时无条件渲染
 *  children，见 ./QueryState.tsx），不会有任何提示——用户看到的是不知不觉
 *  停止更新的旧数据（2026-09-23，f676b198/2c96b89c 只覆盖了账号/资源管理页，
 *  本文件把同一处理搬到其余详情页共用）。独立于 QueryState 之外渲染，不打断
 *  已展示的内容（CLAUDE.md「不得让界面撒谎」/「拦住用户时必须给出路」）。 */
export default function StaleRefreshBanner(
  { error, onRetry, objectName }: { error?: string | null; onRetry: () => void; objectName: string },
) {
  if (!error) return null
  return (
    <OperationError
      title={`${objectName}刷新失败`}
      message={error}
      guidance="当前仍展示上次成功加载的内容，不会用空数据或旧状态覆盖。"
      variant="warning"
    >
      <button type="button" className="btn small" onClick={onRetry}>重试刷新</button>
    </OperationError>
  )
}
