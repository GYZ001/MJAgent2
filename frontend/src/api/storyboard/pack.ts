/**
 * 分镜台 2.0.0（docs/STORYBOARD_PROMPT_IR_DESIGN.md）冻结契约：一个 15 秒段的完整
 * 记录。落在 Shot.storyboard_pack_segment 上（后端 app/production/storyboard_pack.py
 * persist_storyboard_pack 写入），非 null 是唯一权威标记——这一行的
 * shot_size/camera_move/camera_angle/first_frame_desc/last_frame_desc 等描述单个
 * 连续镜头的字段在这里粒度失效，段内 3-4 个镜头切换全部写在 prompt_text 文本里。
 */
export interface StoryboardPackDialogueLine {
  speaker_identity_id: string;
  line: string;
  source_segment_index: number;
}

/**
 * 段所属节拍的自包含记录（2026-08-26 补齐）：拿到一个 shot 就能渲染它承载哪几个
 * 节拍、分别在讲什么，不必再跨行反查。取代裸 beat_ids 数组作为展示用真源。
 */
export interface StoryboardPackSegmentBeat {
  beat_id: string;
  summary: string;
  segment_indexes: number[];
}

export interface StoryboardPackResourceCharacter {
  identity_id: string;
  portrait_id?: string | null;
  description?: string;
  /**
   * 非持久化展示字段，语义同 PrepPackCharacterAsset 的同名字段（见
   * api/screenplay.ts）：按本集集号实时解析出的「当前实际会用的那张」定妆照，
   * 与生成侧同一份判据（app.portraits.current_portrait_ref）。portrait_id 是
   * 段落落库时固化的快照，只做溯源；current_portrait_id 为 null 表示当前无
   * 可用定妆照，不得回退显示 portrait_id 对应的旧图。
   */
  current_portrait_id?: string | null;
  current_portrait_image_url?: string | null;
}

export interface StoryboardPackResourceScene {
  scene_id: string;
  scene_reference_id?: string | null;
  description?: string;
}

export interface StoryboardPackResourceProp {
  label: string;
  description?: string;
}

export interface StoryboardPackResources {
  characters: StoryboardPackResourceCharacter[];
  scenes: StoryboardPackResourceScene[];
  props: StoryboardPackResourceProp[];
}

export interface StoryboardPackSegment {
  segment_no: number;
  duration_s: number;
  synopsis: string;
  source_segment_indexes: number[];
  /** 模型直接产出的整块可复制提示词；代码不拼装、不挂尾缀，必须整块展示与复制。 */
  prompt_text: string;
  shot_count: number;
  dialogue: StoryboardPackDialogueLine[];
  resources: StoryboardPackResources;
  /** 能力降级清单（如 Seedance 侧屏上文字改「无字」）；不许静默吞掉，必须显示。 */
  degraded_capabilities: string[];
  /** 段所属节拍，自包含（含摘要），展示时的唯一真源——见 StoryboardPackSegmentBeat 注释。 */
  beats: StoryboardPackSegmentBeat[];
  /**
   * @deprecated 前端不再读取。早期持久化路径只落了裸 ID，无摘要；现在 beats 已
   * 自包含摘要，展示一律改读 beats。字段仍随行下发（后端过渡期兼容），不属于
   * 前端消费的形状，留着只是避免破坏未迁移的调用方。
   */
  beat_ids: string[];
  /**
   * 冻结契约自己的模型词表（"seedance_2" | "minimax_h3"），由后端从解析出的
   * prompt profile 派生；与 Episode.target_video_model 的供应商 key
   * （"hiagent" | "minimax_h3"）不是同一套词表，不能互相当同义词直接查表。
   */
  target_model: string;
  storyboard_version: string;
}
