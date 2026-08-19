---
name: "seedance-2-microdrama-prompt"
description: "Directs model-native Seedance 2.0 prompts for animated drama. Invoke when generating or revising Seedance video prompts, shot motion, continuity, or references."
---

# Seedance 2.0 Microdrama Prompt

Use this skill for the Seedance 2.0 branch of the project's video-prompt compiler.
The runtime implementation is `app/video_prompt_profiles.py` plus
`app/video_prompt_ai.py`; those files are authoritative when this document and
code differ.

## Director Contract

1. Compile one shot at a time from the published shot and continuity contracts.
2. Preserve character identity, gender, costume, scene geometry, screen
   direction, prop ownership, and the exact authoritative dialogue.
3. Describe an observable chain: opening state, trigger, decisive action,
   physical consequence, reaction, follow-through, and local endpoint.
4. Give each reference one declared job. State what it controls and what must
   not transfer.
5. Use one motivated camera move that keeps the decisive action, contact point,
   entrances, and exits readable.
6. Convert emotions into playable behavior: gaze, grip, breath, weight shift,
   interruption, hesitation, or recovery.

## Animated-Drama Grammar

- Lead with the canonical medium and keep the prompt inside that medium.
- For 2D work, use cel-shaded subjects, painted backgrounds, layer movement,
  held frames, smears, impact frames, and follow-through.
- Keep line weight, palette, character silhouette, and layer roles stable.
- Do not mix photographic rendering language into a canonical illustrated
  style.
- Prefer one strong visible action with a changed endpoint over several vague
  actions.

## Output

Write compact natural Chinese in this order:

`媒介与主体 -> 动作时间线 -> 场景与连续性 -> 镜头 -> 表演与对白 -> 声音 -> 关键约束`

Technical ratio and duration arguments remain at the end for the provider
adapter. Do not invent people, dialogue, props, text, outcomes, or future-shot
content outside the authority contract.

## Sources

Adapted for this project from the MIT-licensed
[`Emily2040/seedance-2.0`](https://github.com/Emily2040/seedance-2.0),
revision `44b514992963a2570beee71aaf2a8720785f7ec2`, especially its prompt,
motion, continuity, director-read, and 2D animation guidance. Also cross-checked
against public Seedance 2.0 prompt guidance on multimodal role assignment and
first/last-frame control.
