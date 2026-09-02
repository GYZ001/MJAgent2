// 连播台域（对内代号 series-film，见 /tmp/series_contract.md）：把一个项目里
// 连续的若干集串行跑完整链路（映射/分镜/确认/生成/成片），再合成一部连播成片。
// 内部标识（view key `series`、路径 `/projects/{id}/series`、接口 `series-film`、
// 类型名 SeriesRun/Film）与后端契约保持字面一致，不做本地改名；面向用户的文案
// 一律叫"连播台/连播成片"，在页面组件里翻译，不在这里。

import { get, mutate } from "./client";

/** SeriesRun.status；SeriesRun.current_stage 额外多一个 "merge"（合成连播成片
 *  的阶段，不属于任何单集）。 */
export type SeriesRunStatus = "running" | "paused" | "failed" | "succeeded" | "cancelled";

/** 单集五个步骤 + 合成连播成片的 merge 步骤（挂在 run 级，不挂在某一集上）。 */
export type Stage = "screenplay" | "storyboard" | "confirm" | "video" | "final" | "merge";

/** EpisodeEntry.stages 的键：Stage 去掉 "merge"（合成不是单集步骤）。 */
export type EpisodeStage = Exclude<Stage, "merge">;

/** skipped = 开始前已满足完成判据，未重跑（挂产物信号，不是状态字段，见契约文档）。 */
export type StageState = "pending" | "running" | "done" | "skipped" | "failed";

export interface EpisodeEntry {
  episode_id: string;
  episode_no: number;
  stages: Record<EpisodeStage, StageState>;
  error: string | null;
}

export interface SeriesRun {
  run_id: string;
  status: SeriesRunStatus;
  episode_from: number;
  episode_to: number;
  current_episode_no: number | null;
  current_stage: Stage | null;
  started_at: number | null;
  updated_at: number;
  finished_at: number | null;
  /** 停在哪一集哪一步的原文错误（中文），配合 current_episode_no/current_stage 定位。 */
  error: string | null;
  episodes: EpisodeEntry[];
}

export interface FilmChapter {
  episode_no: number;
  start_s: number;
  duration_s: number;
}

/** 最近一次成功合成的连播成片；可能来自比当前 run 更早的一次运行。 */
export interface Film {
  url: string;
  path: string;
  duration_s: number;
  size_bytes: number;
  created_at: number;
  episode_from: number;
  episode_to: number;
  chapters: FilmChapter[];
}

export interface SeriesEpisodeAvailable {
  episode_id: string;
  episode_no: number;
  title: string | null;
}

export interface SeriesFilmSnapshot {
  run: SeriesRun | null;
  film: Film | null;
  episodes_available: SeriesEpisodeAvailable[];
}

export interface StartSeriesFilmBody {
  episode_from: number;
  episode_to: number;
  idempotency_key: string | null;
}

export interface StartSeriesFilmResult {
  run_id: string;
  status: "running";
  episode_from: number;
  episode_to: number;
  episodes: EpisodeEntry[];
}

/** GET /projects/{id}/series-film——SeriesPage 轮询快照：最近一条 run（无论终态
 *  与否）+ 最近一次成功合成的 film + 当前可选的起止集清单。 */
export function getSeriesFilm(projectId: string): Promise<SeriesFilmSnapshot> {
  return get(`/projects/${projectId}/series-film`);
}

/** POST /projects/{id}/series-film——开始一段连续区间的连播制作。已有运行中的
 *  连播成片 → 409 SERIES_FILM_ALREADY_ACTIVE；区间内集号正被别的任务占用 → 409
 *  SERIES_FILM_EPISODE_BUSY；缺集 → 422（错误信息里带中文说明）。 */
export function startSeriesFilm(
  projectId: string,
  body: StartSeriesFilmBody,
): Promise<StartSeriesFilmResult> {
  return mutate("POST", `/projects/${projectId}/series-film`, body);
}

/** POST /projects/{id}/series-film/pause——无运行中的 run → 409。 */
export function pauseSeriesFilm(projectId: string): Promise<{ ok: true; status: "paused" }> {
  return mutate("POST", `/projects/${projectId}/series-film/pause`);
}

/** POST /projects/{id}/series-film/resume——从第一个未完成的集/步骤继续；
 *  run 已 succeeded → 409。 */
export function resumeSeriesFilm(
  projectId: string,
): Promise<{ ok: true; status: "running"; run_id: string }> {
  return mutate("POST", `/projects/${projectId}/series-film/resume`);
}
