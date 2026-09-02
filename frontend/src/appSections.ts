// 侧栏导航结构 + 懒加载入口的纯数据定义，从 App.tsx 拆出（2026-09-02，App.tsx 逼近
// FILE_CONVENTIONS.toml 基线，装不下先想怎么拆）。这里只放数据：懒加载函数是单纯的
// `() => import(...)`，SECTIONS/SYSTEM_SECTIONS 是不含 JSX 的数组字面量，不 import
// 'react'。View 类型定义也搬到这里——SECTIONS/PAGE_LOADERS 都要用它，留在 App.tsx
// 会形成 appSections.ts 反向 import App.tsx 的循环；App.tsx 改为
// `export type { View } from "./appSections"`，对外导入路径
// （`import type { View } from '../App'`）保持不变，不是破坏性改名。
//
// `lazy(loadXxxPage)` 的组件常量仍留在 App.tsx：`lazy` 是 React API，这个文件不含 React。

export const loadBiblePage = () => import("./pages/BiblePage");
export const loadScenesPage = () => import("./pages/ScenesPage");
export const loadEpisodesPage = () => import("./pages/EpisodesPage");
export const loadScriptPage = () => import("./pages/ScriptPage");
export const loadBoardPage = () => import("./pages/BoardPage");
export const loadWallPage = () => import("./pages/WallPage");
export const loadCinemaPage = () => import("./pages/CinemaPage");
export const loadSeriesPage = () => import("./pages/SeriesPage");
export const loadMonitorPage = () => import("./pages/MonitorPage");
export const loadReaderPage = () => import("./pages/ReaderPage");
export const loadAccountAdminPage = () => import("./pages/AccountAdminPage");

export type View =
  | "studio"
  | "bible"
  | "scenes"
  | "episodes"
  | "script"
  | "board"
  | "wall"
  | "cinema"
  | "series"
  | "observability"
  | "system"
  | "reader";

export const PAGE_LOADERS: Partial<Record<View, () => Promise<unknown>>> = {
  bible: loadBiblePage,
  scenes: loadScenesPage,
  episodes: loadEpisodesPage,
  script: loadScriptPage,
  board: loadBoardPage,
  wall: loadWallPage,
  cinema: loadCinemaPage,
  series: loadSeriesPage,
  observability: loadMonitorPage,
  system: loadMonitorPage,
  reader: loadReaderPage,
};

export const SECTIONS: {
  key: View;
  label: string;
  icon: string;
  group: string;
  needProject?: boolean;
  needEpisode?: boolean;
  matchViews?: View[];
}[] = [
  {
    key: "bible",
    label: "世界书",
    icon: "书",
    group: "世界书",
    needProject: true,
    matchViews: ["bible", "scenes", "episodes"],
  },
  {
    key: "script",
    label: "映射台",
    icon: "映",
    group: "内容制作",
    needEpisode: true,
  },
  {
    key: "board",
    label: "分镜台",
    icon: "镜",
    group: "内容制作",
    needEpisode: true,
  },
  {
    key: "wall",
    label: "生成台",
    icon: "生",
    group: "内容制作",
    needEpisode: true,
  },
  {
    key: "cinema",
    label: "成片台",
    icon: "片",
    group: "质量交付",
    needEpisode: true,
  },
  { key: "series", label: "连播台", icon: "连", group: "质量交付", needProject: true },
  { key: "observability", label: "观测台", icon: "观", group: "项目观测", needProject: true },
];

export const SYSTEM_SECTIONS: Array<{ key: "overview" | "models" | "accounts" | "settings"; label: string; icon: string }> = [
  { key: "overview", label: "总览", icon: "总" },
  { key: "models", label: "模型中心", icon: "模" },
  { key: "accounts", label: "账号管理", icon: "户" },
  { key: "settings", label: "系统设置", icon: "设" },
];
