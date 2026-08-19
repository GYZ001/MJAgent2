---
name: "minimax-h3-microdrama-prompt"
description: "Writes native MiniMax H3 audiovisual prompts for animated drama. Invoke when H3 is the video provider or H3 reference/keyframe prompts need revision."
---

# MiniMax H3 Microdrama Prompt

Use this skill for the MiniMax H3 branch of the project's video-prompt compiler.
The runtime implementation is `app/video_prompt_profiles.py`,
`app/video_prompt_ai.py`, and `app/minimax_h3.py`.

## Shared Direction

Build the same authority-bound physical-performance draft used by the Seedance
branch. Preserve character identity, gender, costume, spatial continuity,
dialogue text and timing, and the shot endpoint. Express emotion as visible
performance and keep action causes and consequences explicit.

## Native H3 Modes

- First frame: emit the H3 first-frame alignment instruction, then
  `integrated_multimodal_description`, `overall_soundscape`, and
  `non_diegetic_music`.
- First and last frame: align Picture 1 to `0.00` and Picture 2 to the exact
  duration, then describe the continuous path between them.
- Full reference: emit, in order, `subject_definitions`, `summary`,
  `retention_analysis`, `detailed_description`, `overall_soundscape`, and
  `non_diegetic_music`.

## Writing Rules

1. Write creative fields in English. Preserve dialogue, lyrics, and visible
   text exactly in their source language.
2. Give vocal sources stable `(S1)`, `(S2)` identifiers and wrap spoken text in
   `<d>[Language] ...</d>`.
3. Describe shots in playback order. Use exact seconds for motion and dialogue.
4. Write camera motion as type, meaningful amplitude, and speed within the
   action sentence.
5. Define each `<Picture N>`, `<Video N>`, and `<Audio N>` role once and retain
   that meaning throughout.
6. Separate diegetic ambience and physical sounds from audience-only music.
7. Use concrete animated-medium language when the canonical project style is
   illustrated; do not drift into photoreal rendering.

## Sources

Based on MiniMax's official
[`h3-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing)
skill and
[`Video Generation`](https://platform.minimax.io/docs/guides/video-generation)
documentation, reviewed at repository revision
`d21241f0a4b3acbb34c97dae47fa417b7065e438`. This project skill is an
independent, compact adaptation for the existing shot authority and provider
pipeline; upstream reference files are not vendored.
