/** 登录页与首次改密页共用的左侧品牌栏。
 *  两套皮肤下都是深色（与侧栏同一家族），所以它的取值写死在 CSS 里，
 *  不参与暗色覆盖层。窄屏下 .auth-aside 会收成一条头部，正文与流程步骤隐藏。 */
export default function AuthAside() {
  return (
    <aside className="auth-aside">
      <div className="auth-brand">
        <span className="auth-seal" aria-hidden="true">漫</span>
        <span className="auth-brand-copy">
          <b>漫剧案头</b>
          <span>智能剧本 · 分镜 · 成片</span>
        </span>
      </div>
      <div className="auth-pitch">
        <h2>把一本小说，做成一部漫剧</h2>
        <p>
          从原著拆解到分集剧本、逐镜分镜、参考图与成片，一条流水线在同一张案头上跑完，
          每一步的产物、质检与模型调用都留痕可查。
        </p>
        <ul className="auth-flow">
          <li><span>前期准备</span></li>
          <li><span>剧本台</span></li>
          <li><span>分镜台</span></li>
          <li><span>生成台</span></li>
          <li><span>成片台</span></li>
        </ul>
      </div>
      <footer>漫剧案头 · 2.0</footer>
    </aside>
  );
}
