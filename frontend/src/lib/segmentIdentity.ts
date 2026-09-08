import type { StoryboardPackResourceCharacter } from '../api'

/** 与后端选图边界一致；旧记录缺省字段保留原有显示方式。 */
export function needsCharacterImage(character: StoryboardPackResourceCharacter): boolean {
  return character.visibility !== 'voice_only' && character.identity_id !== '旁白'
    && character.subject_kind !== 'extra' && character.subject_kind !== 'crowd'
}
