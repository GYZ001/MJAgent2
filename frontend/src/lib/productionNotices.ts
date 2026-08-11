export type ProductionNoticeSeverity = 'error' | 'warning'

export interface ProductionTaskNotice {
  severity: ProductionNoticeSeverity
  message: string
}

type ScreenplayNoticeInput = {
  screenplay_status?: string | null
  screenplay_error?: string | null
  screenplay_production?: {
    task_active?: boolean
    can_resume_baseline?: boolean
    can_resume_repair?: boolean
    phase_label?: string
    stage_stop_reason?: 'paused' | 'blocked' | 'failed' | ''
  } | null
}

type StoryboardNoticeInput = {
  status?: string | null
  script_error?: string | null
}

function messageText(value: string | null | undefined): string {
  return value?.trim() ?? ''
}

function resumableScreenplayMessage(
  production: NonNullable<ScreenplayNoticeInput['screenplay_production']>,
  message: string,
): string {
  const phase = production.phase_label ?? '剧本流程'
  const prefix = production.stage_stop_reason === 'failed'
    ? `${phase}异常中断`
    : production.stage_stop_reason === 'blocked'
      ? `${phase}门禁未通过`
      : `${phase}已暂停`
  return `${prefix}；${message}`
}

/**
 * Compatibility fields named `*_error` also carry running progress and repair
 * checkpoints.  Only a terminal failure is an error; an inactive repair is a
 * user-actionable warning, and active/stale text is not a notice at all.
 */
export function screenplayTaskNotice(
  episode: ScreenplayNoticeInput,
): ProductionTaskNotice | null {
  const message = messageText(episode.screenplay_error)
  if (!message) return null

  // The live task is authoritative during recovery/start transitions, where a
  // terminal status from the previous activation can briefly coexist.
  if (episode.screenplay_production?.task_active) return null

  if (episode.screenplay_status === 'failed') {
    if (
      episode.screenplay_production?.can_resume_repair
      || episode.screenplay_production?.can_resume_baseline
    ) {
      return {
        severity: 'warning',
        message: resumableScreenplayMessage(
          episode.screenplay_production,
          message,
        ),
      }
    }
    return { severity: 'error', message }
  }

  if (episode.screenplay_status === 'repairing') {
    const production = episode.screenplay_production
    if (production?.can_resume_repair || production?.can_resume_baseline) {
      return {
        severity: 'warning',
        message: resumableScreenplayMessage(production, message),
      }
    }
  }

  return null
}

/** Derive storyboard notice severity from the workflow state, never text alone. */
export function storyboardTaskNotice(
  episode: StoryboardNoticeInput,
  workspaceState?: string | null,
): ProductionTaskNotice | null {
  const message = messageText(episode.script_error)
  if (!message) return null

  // A workspace snapshot is newer and more specific than the compatibility
  // episode status.  Do not let a stale terminal flag override it.
  if (workspaceState) {
    if (workspaceState === 'failed') return { severity: 'error', message }
    if (workspaceState === 'paused') return { severity: 'warning', message }
    return null
  }

  // Older detail responses may not contain a workspace snapshot.  In that
  // contract, `scripted + script_error` means paused/awaiting intervention.
  if (episode.status === 'script_failed') {
    return { severity: 'error', message }
  }
  if (episode.status === 'scripted') {
    return { severity: 'warning', message }
  }

  return null
}
