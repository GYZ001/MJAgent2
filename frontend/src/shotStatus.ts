import type { Shot } from './api'

/** 生成台镜头按钮上的简明执行阶段。 */
export function compactShotStage(shot: Shot): string {
  const pipeline = shot.pipeline
  const stage = pipeline?.pipeline_stage || pipeline?.current_stage || ''
  const pipelineStatus = pipeline?.pipeline_status || ''
  if (pipeline?.reason_code === 'WAITING_STATIC_BOUNDARY_ASSET') {
    return '等待上一镜静态尾帧'
  }
  if (pipeline?.reason_code === 'PREFETCHING_STATIC_TAIL') {
    return '预生成本镜静态尾帧'
  }
  const dependsOnUpstream = Boolean(
    shot.mode_plan?.depends_on_shot_id
    || shot.versions?.some(
      version => Boolean(version.image_inputs?.after_shot_id),
    )
  )
  if (pipelineStatus === 'paused_budget') {
    return dependsOnUpstream
      ? '预算暂停，恢复后等待上一镜素材'
      : '预算暂停'
  }
  if (stage === 'job_queued' && dependsOnUpstream) {
    return '等待上一镜采用素材'
  }
  const progress = pipeline?.stage_progress
  const current = progress?.current
  const total = progress?.total
  const labels: Record<string, string> = {
    preflight_validating: '校验视频输入',
    preflight_retry: '校验失败，自动重试',
    preflight_blocked: '输入校验未通过',
    job_queued: '已入队',
    reference_prompt: '准备参考图词',
    reference_generate: current != null && total ? `候选图 ${current}/${total}` : '生成候选图',
    reference_consistency: '参考图一致性',
    waiting_continuity_anchor: '等待上一镜',
    waiting_dependency: '等待上一镜采用素材',
    continuity_assembling: '装配连续性',
    video_ready: '视频输入就绪',
    waiting_video_slot: pipeline?.queue_position ? `等待名额（前方 ${pipeline.queue_position}）` : '等待视频名额',
    video_submitting: '提交供应商',
    video_generating: '供应商生成中',
    video_downloading: '下载视频',
    video_technical_check: '技术校验',
    auto_retake_queued: '重新生成排队',
    paused_budget: '预算暂停',
    waiting_human: '等待人工',
  }
  return labels[stage] || pipeline?.stage_label || '状态同步中'
}
