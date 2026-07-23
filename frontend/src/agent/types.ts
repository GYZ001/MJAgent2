/** Agent Drawer 类型合同（与后端 app/agent/schemas 对齐）。 */
export type AgentView =
  | 'studio' | 'bible' | 'scenes' | 'episodes' | 'script'
  | 'board' | 'wall' | 'cinema' | 'monitor' | 'reader'

export interface ContextEnvelope {
  route: AgentView
  project_id?: string | null
  episode_id?: string | null
  selected_shot_id?: string | null
  selected_version_id?: string | null
  active_tab?: string | null
  unsaved_draft?: boolean
  visible_issue_ids?: string[]
}

export type UiIntent =
  | { type: 'navigate'; view: AgentView; project_id?: string; episode_id?: string; chapter_idx?: number; auto_follow?: boolean }
  | { type: 'select_shot'; episode_id: string; shot_id: string }
  | { type: 'select_version'; shot_id: string; version_id: string }
  | { type: 'open_evidence'; artifact_id: string }
  | { type: 'open_delivery'; episode_id: string; tab: 'preview' | 'readiness' | 'records' }
  | { type: 'open_download'; package_id: string; artifact: 'report' | 'archive' }
  | { type: 'open_credentials'; model_id?: string }
  | { type: 'preview'; shot_id?: string; version_id?: string; artifact_id?: string }
  | { type: 'request_directory_grant' }

export interface AgentConversation {
  id: string
  title?: string | null
  project_id?: string | null
  status: string
  created_at: number
  updated_at: number
}

export interface AgentMessage {
  id: string
  conversation_id: string
  turn_id?: string | null
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: Record<string, unknown>
  created_at: number
}

export interface AgentTurn {
  id: string
  conversation_id: string
  status: string
  failure_code?: string | null
  failure_message?: string | null
}

export interface ApprovalCardData {
  tool_call_id: string
  command: string
  title: string
  summary: string
  risk: string
  estimated_cost_cny?: number | null
  affected?: Record<string, unknown>
  warnings?: string[]
  approval_token?: string
  expires_at?: number
}

export interface AgentStreamEvent {
  event_id: number
  event_type: string
  payload: Record<string, unknown>
}

export const QUICK_PROMPTS = [
  '这个项目下一步该做什么？',
  '最近失败的 Run 在哪里？帮我定位证据。',
  '检查当前分集能否交付，并列出阻塞项。',
  '估算生成当前集待办镜头的费用（先不要执行）。',
] as const
