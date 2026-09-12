import { useEffect, useState } from 'react'
import { api, ApiError, type ProjectGrantRow, type RoleRow, type TeamRow } from '../api'

/** EP-01 第二阶段：项目设置里的「协作者」区块——把项目授权给其他账号或团队，
 *  被授权者按所选角色获得对应权限。增删只对项目所有者/组织管理员生效，非
 *  所有者提交会拿到后端 403（见 app/orgs/api.py::_require_project_manage_access），
 *  这里不做额外的前端角色判断隐藏表单——项目页目前拿不到 owner_user_id，
 *  强行按「是否组织管理员」隐藏会连大多数普通项目所有者自己都挡住，属于
 *  界面比后端更严格的反向错误；403 时的错误文案已经把路指清楚。 */
type SubjectType = 'user' | 'team'

export default function ProjectCollaboratorsPanel({ projectId }: { projectId: string }) {
  const [grants, setGrants] = useState<ProjectGrantRow[] | null>(null)
  const [roles, setRoles] = useState<RoleRow[]>([])
  const [teams, setTeams] = useState<TeamRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [subjectType, setSubjectType] = useState<SubjectType>('user')
  const [subjectId, setSubjectId] = useState('')
  const [roleId, setRoleId] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    setError(null)
    api.listProjectGrants(projectId)
      .then(res => setGrants(res.items))
      .catch(err => setError(err instanceof Error ? err.message : String(err)))
    api.listRoles().then(res => setRoles(res.items)).catch(() => setRoles([]))
    api.listTeams().then(res => setTeams(res.items)).catch(() => setTeams([]))
  }, [projectId])

  const roleName = (id: string) => roles.find(r => r.id === id)?.name ?? id

  const addGrant = async () => {
    if (!subjectId || !roleId) { setFormError('请选择授权对象与角色'); return }
    setBusy(true)
    setFormError(null)
    try {
      const result = await api.createProjectGrant(projectId, {
        subject_type: subjectType, subject_id: subjectId, role_id: roleId,
      })
      setGrants(result.items)
      setSubjectId('')
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (grant: ProjectGrantRow) => {
    setBusy(true)
    try {
      await api.deleteProjectGrant(projectId, grant.subject_type, grant.subject_id)
      setGrants((await api.listProjectGrants(projectId)).items)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card project-collaborators">
      <h2>协作者</h2>
      <p className="sub">把这个项目授权给其他账号或团队，被授权者按所选角色获得对应权限。</p>
      {error && <p className="query-error" role="alert">{error}</p>}
      {grants === null && !error && <p className="account-admin-muted">载入中…</p>}
      {grants && grants.length === 0 && <p className="account-admin-muted">还没有协作者，仅你自己可以访问这个项目。</p>}
      {grants && grants.length > 0 && (
        <ul className="project-collaborators-list">
          {grants.map(g => (
            <li key={`${g.subject_type}:${g.subject_id}`}>
              <span className="stamp">{g.subject_type === 'user' ? '账号' : '团队'}</span>
              <b>{g.subject_id}</b>
              <span>{roleName(g.role_id)}</span>
              <button type="button" className="btn small ghost danger" disabled={busy}
                onClick={() => void revoke(g)}>撤销</button>
            </li>
          ))}
        </ul>
      )}
      <div className="project-collaborators-form">
        <select aria-label="授权对象类型" value={subjectType}
          onChange={e => { setSubjectType(e.target.value as SubjectType); setSubjectId('') }}>
          <option value="user">账号</option>
          <option value="team">团队</option>
        </select>
        {subjectType === 'team' ? (
          <select aria-label="选择团队" value={subjectId} onChange={e => setSubjectId(e.target.value)}>
            <option value="">选择团队…</option>
            {teams.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        ) : (
          <input aria-label="账号 ID" placeholder="账号 ID" value={subjectId}
            onChange={e => setSubjectId(e.target.value)} />
        )}
        <select aria-label="选择角色" value={roleId} onChange={e => setRoleId(e.target.value)}>
          <option value="">选择角色…</option>
          {roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
        <button type="button" className="btn primary" disabled={busy} onClick={() => void addGrant()}>添加协作者</button>
      </div>
      {formError && <p className="query-error" role="alert">{formError}</p>}
    </section>
  )
}
