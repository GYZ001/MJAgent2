# 成片台字幕嵌入：台词账本 × 本地语音对齐 → 时间戳精确的烧录字幕

> 优先级 P0 ｜ 依赖：分镜台 2.x 台词合同（`storyboard_pack_segment.dialogue[]`）、成片台合成链路（`app/media_exec/concat.py`、`app/final_edit.py`）
> 一句话：字幕**文本**逐字取自台词账本（不会写错字、不会错说话人），字幕**时间**取自对生成视频音轨的本地语音识别对齐（不依赖任何外部服务），合成成片时用 libass 烧进画面，并同时产出 `.srt/.ass` 边车文件。

---

## 0. 结论先行（2026-09-14 实测）

设计前先做了证伪实验，不是纸上推演。用 B 现网《我欲封天》第 10 集两镜真实生成视频（Seedance 2.0 产出，含语音音轨）跑本地 ASR，再与台词账本逐字对齐：

| 项目 | 镜 7 | 镜 10 |
|---|---|---|
| 台词账本字数（去标点） | 52 | 43 |
| ASR 命中字数 / 命中率 | 51 / 98% | 42 / 98% |
| 唯一错字 | 名→鸣（同音） | 师→熙（近音） |
| 对齐出的发声窗口 | 0.42s → 11.52s | 0.24s → 7.14s |
| 逐字时间戳粒度 | 60 ms | 60 ms |
| 识别耗时（A 机 2 核，2 线程） | 1.13s / 15.1s 音频（RTF 0.075） | 1.17s / 15.1s（RTF 0.078） |
| 模型加载 | 1.29s（每进程一次） | — |

B 机（8 核，4 线程）复测：见 §12 表（同一脚本、同一音频）。

烧录链路也在 B 上真实跑通：静态 ffmpeg 7.0.2 带 `libass`，用 `ass=文件:fontsdir=/usr/share/fonts/truetype/wqy` 把「没错，一旦王师兄进入内宗」烧进镜 10 前 3 秒，抽帧肉眼核对：中文字形正确、居中、位于底部安全区之上（fontconfig 报「无配置文件」告警但不影响渲染）。

**两个实验各自证明一半**：ASR 证明「时间戳能从音轨里精确取到」，libass 证明「能把中文烧进 1080×1920 竖屏」。剩下的是工程接线。

---

## 1. 现状事实

- **视频模型只出画面与声音，不出字幕**（2026-09-14 用户裁定，`app/production/video_text_policy.py` 的 `_TEXT_POLICY_NONE`：「台词只出声不出字幕」；`app/media_exec/subtitle_gate.py` 抽帧问 VLM，发现叠加字幕即判 BLOCKER 定向重抽）。所以成片里**现在没有任何字幕**，字幕必须由本功能后期加。
- **每一段 15 秒视频都有台词合同**：`shots.shot_contract_json` → `storyboard_pack_segment.dialogue[]`，每条含 `utterance_id`、`line`（原话逐字）、`speaker_identity_id`、`delivery_kind`（`spoken_dialogue | offscreen_dialogue | inner_monologue | narration`）、`source_quote_id`。提示词里用 `{{speech:U01}}` 占位符要求模型在指定时机念出这句话（`app/production/storyboard_speech_render.py`）。旧版镜头只有 `shots.dialogues[]`（`speaker/line/delivery`）。
- **生成视频带真实语音音轨**：B 上实测 `episodes/10/shots/10/v1.mp4` 为 aac 32k/44.1k 立体声，mean_volume −20 dB，与台词逐字对得上（§0）。采样率不统一（32000 / 44100 混用），成片链路已在逐镜归一（`audio_normalize_filter`）。
- **成片合成两条路**（`app/media_exec/concat.py::concatenate_episode`）：
  - `draft_concat`（主流，B 上第 10 集报告 `decision_reason=simple_timeline_fast_concat`）：逐镜归一音频，视频不变速且分辨率一致时 `-c:v copy`，再 concat demuxer `-c copy`——**全程零视频重编码**。
  - `final_edit`（有确定性插字或非硬切转场时启用）：`_prepare_clip` 逐镜 INTERMEDIATE 编码 → `_compose` xfade + loudnorm + DELIVERY 编码。
  - 两条路的产物都是 `projects/{pid}/episodes/{n}/final/episode.mp4` + `episode.edit-report.json`；`episode_mix_status()` 把报告原样投影给成片台。
- **连播成片**（`app/domain/series_ops/merge.py`）把各集 `episode.mp4` 拼成 `series/epA-epB/film.mp4`，各集流参数一致时 concat demuxer 流拷贝（秒级完成）。
- **交付包**（`app/delivery_package_build.py`）复制 `media/episode.mp4`、`reports/final-edit.json`、逐镜 mp4；**导出**（`app/domain/series_ops/exports.py`）硬链接 `film.mp4`。
- **已有的中文字体与文字渲染**：`app/final_edit.py::_font_path()` 按候选列表找 CJK 字体（`MANJU_CJK_FONT_PATH` 可覆盖），B 上实际存在 `/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc`；确定性插字卡用 Pillow 画 PNG 再 overlay。静态 ffmpeg **没有 `drawtext`**、有 `ass`/`subtitles` 滤镜。
- **B 机环境**：8 核 / 30 GB / 无 GPU / Python 3.11.15 / 磁盘余 146 GB / **没有 `bzip2` 可执行文件**（模型包是 `.tar.bz2`，只能用 Python `tarfile` 解）。A 机 2 核 / 3 GB / Python 3.12。两机都没有 numpy/onnxruntime/torch。
- `app/schemas/shot_state.py::AudioTimelineItem` 有「计划中的」发声时间轴（start_s/end_s/text），但那是分镜阶段的预期，视频模型并不按它执行，不能拿来当字幕时间。

## 2. 目标与非目标

**P0（本期必做）**
1. 成片合成时把本集全部**已采纳镜头**的台词按真实发声时间烧进 `episode.mp4`；同时写出 `final/episode.srt` 与 `final/episode.ass`。
2. 字幕文本 = 台词账本原话（逐字），不用 ASR 识别文本（它会写错同音字）。
3. 时间戳 = 本地 ASR（sherpa-onnx + SenseVoice int8，CPU）逐字时间戳与台词逐字对齐的结果（**同音等价**：用 pypinyin 无声调拼音做序列匹配，见 §5）；每句台词有 `aligned | missing` 两种状态，**没在音轨里找到的台词不烧**（画面里没人说，烧上去就是界面撒谎），列进报告与成片台。
4. 对齐结果按「镜头版本 id + 文件 sha256」缓存，重合成不重跑 ASR。
5. 连播成片（`film.mp4`）自然携带各集已烧字幕，流拷贝路径不受影响；并生成整片 `film.srt`。
6. 交付包含 `.srt`；成片台显示「N/M 句已对齐、K 句未出声（镜 x、y）」与 `.srt` 下载。
7. 一个总开关 + 四个样式设置（监制房），默认样式按 1080×1920 竖屏短剧惯例。
8. 引擎/模型/字体缺失时，**成片失败并给出安装命令**，不静默出无字幕成片；开关关闭时合成行为与今天逐字节一致。

**P1**
- 成片台「手动标时间」：对 `missing` 的句子填起止秒数后重合成。
- 导出目录带 `film.srt` 硬链接；交付包带 `.ass`。
- 说话人前缀（`名字：`）与画外音/独白的差异样式。

**P2**
- mp4 内嵌软字幕轨（`mov_text`），播放器可开关。
- 只对有台词的镜头做逐镜烧录、无台词镜头保持 `-c:v copy`（省编码时间）。
- 把「台词未出声」反馈给评审墙 Supervisor 作定向重抽依据。

**非目标**
- 不做 TTS 配音（音轨来自视频模型）。
- 不做 ASR 全文转写字幕（文本以账本为准）。
- 不按「均分时间」估算字幕（那是兜底编造，见 §3）。
- 不接任何云端识别服务。

## 3. 方案选型（三选一，已定）

| 方案 | 文本来源 | 时间来源 | 结论 |
|---|---|---|---|
| A 视频模型直接生成字幕 | 模型 | 模型 | **否**。已被裁定禁止，字幕闸门正在拦它；模型烧的字体/位置/错字不可控。 |
| B 台词账本 + 按字数均分镜内时间 | 账本 | 估算 | **否**。实测镜 7 台词 0.42s 开口、11.52s 说完，镜 10 却 7.14s 就说完——同为 15 秒段，发声占比 76% 与 47%，均分必然错位数秒。用户要的是「完美契合」，估算做不到，而且是 CLAUDE.md 禁止的兜底填充。 |
| **C 台词账本文本 + 本地 ASR 逐字对齐时间** | 账本 | 音轨 | **选它**。文本零错字，时间 60 ms 粒度；实测两镜命中 98%。台词没被念出来时能如实报「未出声」——这本身就是一条有价值的质检信号（CLAUDE.md「单次长调用会漏掉整个类别，缺失必须有可见信号」）。 |

用户提的两个思路「内置音频转文字」与「直接把小说台词复过去」在 C 里各占一半：ASR 只负责**定位**，账本负责**内容**。纯 ASR 字幕会把「名动八方」写成「鸣动八方」、把「许师姐」写成「许熙姐」（实测），账本文本正好把这类错误归零。

**引擎选型**：sherpa-onnx 1.13.8（pip 轮子 4.4 MB + core 10.6 MB，自带 onnxruntime，**不依赖 numpy/torch**；cp311/cp312 manylinux 轮子均已核实可下载）+ SenseVoice-small int8 模型（压缩包 163 MB，解压 230 MB；中文原生、CTC 逐 token 时间戳、CPU 上 RTF < 0.1）。备选 Paraformer-zh 同框架可换，接口不变。Whisper 系（faster-whisper 需 ctranslate2；whisper.cpp 需另编译）中文短句更慢且更爱幻听，不选。

## 4. 端到端流程

```
concatenate_episode(episode_id)
  ├─ 现有：选片 piece_specs[(shot_no, path, rate)]、探测、跳过缺镜
  ├─ 新增 ① subtitles.episode.prepare(conn, episode_id, piece_specs, probe_by_shot)
  │     ├─ 读每镜台词合同（shot_contract_json → dialogue[]；旧镜头退回 shots.dialogues）
  │     ├─ 缓存命中？(adopted_version_id, file_sha256) → 取 result_json
  │     ├─ 未命中：ffmpeg 抽 16k 单声道 wav → 一个子进程跑全部未命中镜头的 ASR → tokens+timestamps
  │     ├─ align：逐字对齐 → 每句 {status, match_ratio, char_times[]}
  │     ├─ 写缓存 subtitle_alignments
  │     └─ 返回 per-shot 本地 cue（尚未换算到整集时间轴）
  ├─ draft_concat 路径：
  │     逐镜归一（不变）→ timeline 换算（offset_i=Σ前面归一片段实测时长）→ 写 episode.ass
  │     → concat demuxer：有字幕时走重编码分支 + `-vf ass=…:fontsdir=…`（DELIVERY_VIDEO_ARGS）
  ├─ final_edit 路径：
  │     _prepare_clip 不变 → _compose 里 offset_i=Σ时长−Σxfade → 在最终滤镜链末尾加 `ass=`
  ├─ 新增 ② 校验：ffprobe 时长照旧；抽 1 帧确认文件可解码（已有）
  └─ 发布：episode.mp4 + episode.ass + episode.srt + edit-report.subtitles 原子落盘
```

**ASR 只在「已采纳镜头的原始文件」上跑**，与倍速无关；倍速在换算阶段处理（`t / rate`）。

## 5. 对齐算法（`app/subtitles/align.py`，纯函数，L1）

输入：`lines = [(utterance_id, text)]`（本镜台词按合同顺序）、`tokens = [(token, start_s)]`（ASR 输出）。

1. **展开**：token → 逐字，每字继承 token 起始时间；丢弃标点与 `<|zh|>` 这类特殊 token。台词同样去标点/空白/引号，得到 `known_chars` 及每字所属 line 下标。
2. **单调匹配**：`difflib.SequenceMatcher(None, known_chars, asr_chars, autojunk=False)`，`get_matching_blocks()` 天然单调（后一句不会对到前一句前面）。整镜一次匹配，不逐句搜索——逐句搜索会让重复短语（「师兄」出现两次）串位。
3. **每句判定**（2026-09-14 按 B 上 3 集 77 句真实台词的独立扫描修订）：匹配在**无声调拼音序列**上做（`pypinyin.lazy_pinyin` 整句转换，利用词典读音），同音字视为命中；`match_ratio = 命中字数 / 句字数`，另记 `exact_chars`（字面相同数）供报告。
   - `len ≥ 4`：`ratio ≥ 0.60` → `aligned`；
   - `len ≤ 3`（「火蛇术」「是啊」）：要有一个长度 ≥ 2 的连续命中块（`len ≤ 2` 须全命中），位置由单调性保证落在前后句之间；
   - 其余 → `missing`。
   依据：77 句纯字面匹配的分布是 ≥0.9 有 62 句、0.7–0.9 有 12 句、0.5–0.7 有 2 句、<0.3 有 1 句；掉到 0.9 以下的几乎全是同音错字（灵石→零食、止血丹→止血蛋、妖化术→妖花树、养丹坊→养丹方），两句 0.5–0.7 的（「火蛇术！」2/3、「我出三块灵石！」4/6）都确实念了。同音等价后复扫同一批 77 句：≥0.9 有 72 句、0.7–0.9 有 3 句（都是模型小幅改词但确实念了）、0.5–0.7 只剩「火蛇术！」（2/3，连续块长 2，按短句规则 aligned）、<0.3 只有「你……你……」（结巴句，ASR 没听出来，判 missing 是对的）——念了的最低 0.67、没念的 0.0，0.60 落在空当里。阈值是内部常量，报告里带每句 ratio/exact，便于后续校准，不做成设置项。
4. **逐字时间**：命中字取 ASR 时间；未命中字在相邻命中字之间线性插值；句尾时间 = 末字起始 + `tail`，`tail` = 本句相邻字间距中位数（实测 0.12–0.24s），上限 0.40s。
5. **多余语音**：ASR 里连续 ≥ 6 个未匹配到任何台词的字 → `extra_speech[{text, start_s, end_s}]`，进报告不烧字幕（模型自己加戏或旁白幻听，都该让人看见）。
6. **无音轨**：`has_audio=False` 的镜头全部 `missing`，reason=`no_audio`。


## 6. 字幕切分与时间规则（`app/subtitles/cues.py`，L1）

- 一句台词按句读（，。！？；：、…）切成小句，每小句起止来自逐字时间。
- 相邻小句合并直到 ≤ `max_chars`（默认 14）且间隔 < 0.6s；单小句 15–28 字在最近标点处折成两行（`\N`），> 28 字按时间拆成多条 cue。
- cue 起 = 首字时间 − 0.10s 提前量；止 = 末字时间 + tail + 0.30s；最短 0.7s（能延则延）；相邻 cue 间隔 ≥ 0.04s，不重叠；全部裁到本镜有效时长内（`duration / rate`）。
- 每条 cue 记 `{shot_no, utterance_id, text, start_s, end_s}`（镜内本地时间）。

## 7. 时间轴换算（`app/subtitles/timeline.py`，L1）

- `draft_concat`：`offset_i = Σ_{j<i} prepared_duration_j`，`prepared_duration` 取归一后片段的 ffprobe 实测（与现有「视频流实测时长是权威」口径一致，不用名义 `duration_s`）。
- `final_edit`：`offset_i = Σ_{j<i} prepared_duration_j − Σ_{j≤i} xfade_before_j`（**含第 i 段自己的入场转场**：xfade 让第 i 镜提前 `xfade_before_i` 秒进入），与 `_compose` 的 `cumulative`/`offset` 演进逐步对照过（U1 用 3 段手算验证，见 `tests/test_subtitles_timeline.py` 顶部推导）。
- 部分合成（跳过缺镜）只换算入选镜头；转场跨缺镜时按硬切（与现有规则一致）。
- 换算后再裁一次：`end ≤ offset_i + effective_duration_i`。

## 8. 渲染（`app/subtitles/ass.py`，L1）

ASS 头部固定 `PlayResX=1080, PlayResY=1920, WrapStyle=2, ScaledBorderAndShadow=yes`；样式：

| 参数 | 默认 | 设置键 | 说明 |
|---|---|---|---|
| 字体 | `_font_path()` 解析出的文件 → Pillow `getname()[0]` 取家族名（wqy-zenhei → `WenQuanYi Zen Hei`） | `MANJU_CJK_FONT_PATH`（已有） | `fontsdir=` 指向该文件所在目录，不依赖 fontconfig（B 上已验证） |
| 字号 | 64 | `subtitle_font_size` 40–120 | 1080 宽下 14 字 ≈ 900 px |
| 底边距 | 400 px | `subtitle_margin_bottom` 0–800 | 避开短视频平台底部 UI（约 20% 高） |
| 每行字数 | 14 | `subtitle_max_chars_per_line` 8–24 | 影响 §6 合并/折行 |
| 说话人前缀 | 关 | `subtitle_show_speaker` | P1 |
| 颜色/描边 | 白字、黑描边 3、阴影 1、居中（Alignment 2） | 固定 | 不做成设置，避免调出不可读字幕 |

ASS 文本转义：`\` `{` `}` 与换行；时间格式 `H:MM:SS.cc`。SRT 由同一份 cue 列表生成（`HH:MM:SS,mmm`）。

烧录命令片段：`-vf "ass=<episode.ass>:fontsdir=<dir>"`（final_edit 路径拼进 filter_complex 末尾）。路径含特殊字符按 ffmpeg 滤镜转义规则处理（`:` `\` `'`），并加守卫测试。

## 9. 数据模型与产物

```sql
subtitle_alignments(
  shot_version_id TEXT PRIMARY KEY,   -- shot_versions.id（已采纳版本）
  media_sha256    TEXT NOT NULL,      -- 与 video_delivery_manifest.items[].file_sha256 同源
  engine_id       TEXT NOT NULL,      -- 'sherpa-onnx/1.13.8'
  model_id        TEXT NOT NULL,      -- 'sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
  result_json     TEXT NOT NULL,      -- {tokens:[[tok,t]...], lines:[{utterance_id,status,match_ratio,char_times}], extra_speech:[...]}
  created_at      REAL NOT NULL)
```
命中条件：`shot_version_id` 相同且 `media_sha256`、`model_id` 相同；样式改动只重做 §6–§8，不重跑 ASR。建表走仓库懒建表约定：`ensure_schema()`（独立连接）+ `ensure_tables_on_connection(conn)`（逐条 execute，**禁 executescript**）+ 经 `app.db_schema.ensure_schema_respecting_caller_transaction` 分派，`tests/test_schema_guard.py` 会扫到。

文件产物（与 `episode.mp4` 同目录、同一次发布原子落盘）：`episode.ass`、`episode.srt`；连播：`film.srt`（按 `chapters[].start_s` 偏移拼接各集 srt）。

`episode.edit-report.json` 新增（落地形状，2026-09-15）：
```json
"subtitles": {
  "enabled": true, "engine_id": "sherpa-onnx/1.13.8", "model_id": "sherpa-onnx-sense-voice-…-int8-2024-07-17", "font_family": "WenQuanYi Zen Hei",
  "lines_total": 31, "lines_aligned": 29, "lines_missing": 2, "cues": 87,
  "missing": [{"shot_no": 12, "utterance_id": "U02", "line": "……", "match_ratio": 0.12, "reason": "not_found|no_audio|short_line_partial"}],
  "extra_speech": [{"shot_no": 5, "text": "……", "start_s": 3.2, "end_s": 5.9}],
  "lines": [{"shot_no": 12, "utterance_id": "U01", "status": "aligned", "match_ratio": 0.98, "exact_chars": 41, "matched_chars": 42, "total_chars": 43, "start_s": 0.24, "end_s": 7.26}],
  "cues_timeline": [{"shot_no": 1, "utterance_id": "U01", "text": "……", "start_s": 0.34, "end_s": 2.9}],
  "ass_text": "…", "ass_sha256": "…", "srt_sha256": "…",
  "cache_hits": 25, "asr_shots": 2, "asr_elapsed_s": 6.2
}
```
报告是唯一真源：`episode.srt`/`episode.ass` 边车由报告里的 `cues_timeline`/`ass_text` 幂等物化（发布与恢复流程各调一次），`episode_mix_status()` 只在边车存在且 sha256 与报告一致时给 `subtitle_srt_url`，投影给前端时剔除 `ass_text`/`cues_timeline`。
关闭时写 `{"enabled": false}`，不写其它键。`episode_mix_status()` 已把整份报告投影给前端，无需新接口；交付包新增 `role=subtitle_srt` 文件。

## 10. 接入点清单

| 位置 | 改动 |
|---|---|
| `app/media_exec/concat.py::concatenate_episode` | 选片后调用 `subtitles.episode.prepare()`；把结果传入两条路径；发布时一并写 ass/srt 与报告键 |
| `concat.py::_draft_concat_pieces / _run_concat_demuxer` | 有字幕 → 强制重编码分支并加 `-vf ass=`；无字幕 → 原逻辑不动（守卫测试断言 ffmpeg 参数逐项相同） |
| `app/final_edit.py::_compose` | 接收可选 `ass_path`，在最终视频标签后接 `ass=` 再 map；单镜分支加 `-vf` |
| `app/domain/series_ops/merge.py::build_series_film` | 各集 `episode.srt` 存在时按 chapter 偏移合成 `film.srt`（不影响流拷贝判定） |
| `app/delivery_package_build.py` | `_copy_if_present(episode.srt → media/episode.srt, role=subtitle_srt)` |
| `app/monitoring.py` + `app/config.py` | 5 个设置键（§8）+ `subtitle_burn_in_enabled` 布尔，默认 `false` |
| `scripts/preflight.py` | 新检查：`import sherpa_onnx`、模型目录含 `model.int8.onnx`+`tokens.txt`、CJK 字体可解析、`ffmpeg -filters` 含 `ass`；开关开而任一缺 → fail，开关关 → warn |
| `scripts/fetch_asr_model.py`（新） | 下载/校验 sha256/用 Python `tarfile` 解压到 `data/models/sense-voice-int8/`；支持 `--from-file` 离线包与 `--url` 镜像；幂等 |
| `requirements.txt` | `sherpa-onnx==1.13.8`、`pypinyin==0.55.0`（MIT、零依赖、3.9 MB） |
| 前端 `CinemaPage.tsx` | 状态卡一行摘要 + 预览面板「未出声台词」列表（镜号可点到生成台）+ `.srt` 下载按钮 |

设置读取口径：`get_setting()` 非法值一律 `RuntimeError`（沿用 `subtitle_gate.enabled()` 写法），不静默回退默认。

## 11. 失败策略与用户出路

| 情形 | 行为 |
|---|---|
| 开关开、引擎/模型/字体缺 | 成片**失败**，错误文案给出 `scripts/fetch_asr_model.py` 或 `MANJU_CJK_FONT_PATH` 的具体动作；上一版 `episode.mp4` 原封不动 |
| ASR 子进程超时/崩溃 | 同上失败（超时 = 60s + 5s×镜数）；不降级成无字幕成片 |
| 某句 `missing` | 不烧该句；报告 + 成片台列出镜号与原话；出路 = 去生成台重抽该镜（P0）/ 手动标时间（P1） |
| `extra_speech` | 只报告，不烧 |
| 部分合成 | 只对入选镜头做，摘要注明「阶段成片」 |
| 开关关 | 与现状逐字节一致；报告 `subtitles.enabled=false` |

不提供「忽略缺失继续」之外的任何强制绕过——「不烧未出声的句子」本身就是安全默认，不需要绕过。

## 12. 性能预算

| 环节 | A（2 核） | B（8 核） |
|---|---|---|
| ASR 15s 镜头 | 1.1s（RTF 0.075，2 线程） | 0.39s（RTF 0.026，4 线程） |
| 模型加载 | 1.3s / 进程 | 0.92s / 进程 |
| 27 镜整集首次 | ≈ 32s | ≈ 12s |
| 重合成（缓存命中） | 0 | 0 |

B 机复测（同脚本、4 线程，Python 3.11 + tuna 源装轮子）：模型加载 0.92s；镜 7 识别 0.39s（RTF 0.026）、镜 10 识别 0.38s（RTF 0.025）；对齐结果与 A 机逐字相同（51/52、42/43，错字同为 名→鸣、师→熙）。

**编码代价是真正的成本**：`draft_concat` 今天对分辨率一致的集是零视频重编码；烧字幕后整集要做一次 DELIVERY 编码（x264 medium crf20，A 机实测约 2.5× 实时，B 8 核多线程快数倍）。405 秒的一集在 A 上约 17 分钟，在 B 上预计 3–5 分钟——验收时在 B 实测填数。这与 final_edit 路径今天的开销同量级，且成片本来就是串行任务。P2 的「只重编有台词的镜头」可再省一部分。

内存：SenseVoice int8 子进程峰值约 500 MB；ASR 全机串行（`_ASR_LOCK`，与 `_MERGE_LOCK` 同做法），三集并行也只多一个子进程。

## 13. 架构与工程约束（派单必带）

- 新包 `app/subtitles/`，**不在 `app/` 根目录加散文件**。层号声明进 `app/LAYERS.toml`：`align/cues/ass/timeline/asr_worker` = 1（只依赖 stdlib，`asr_worker` 只在子进程里 `import sherpa_onnx`，**主进程永不 import 它**）；`store` = 2（碰 `app.db`）；`engine/episode` = 4（ffmpeg + 子进程 + store）；调用方 `concat.py` 是 L5、`final_edit.py` 是 L4，方向合法。
- 文件 ≤ 500 行、函数 ≤ 50 代码行，**一条基线都不许新增**；`concat.py`（1416 行，在基线内）与 `final_edit.py`（573）只能净减或持平——需要的新逻辑放进新包，调用方各加几行。
- 建表遵守懒建表三前提（§9）；不得在调用方连接上隐式提交；诊断/缓存写入用独立连接。
- 所有权显式：`prepare(conn, ...)` 的 `conn` 必传，无默认值。
- 子代理禁跑全量，定向命令：`.venv/bin/python -m pytest tests/test_subtitles_*.py tests/test_final_edit.py tests/test_concat_av_normalization.py tests/test_episode_partial_concat.py tests/test_series_film_merge.py tests/test_delivery.py -q`；退出码不走管道；并发/缓存用例连跑 ≥ 10 次。
- 真模型冒烟测试 `tests/test_subtitles_engine_real.py`：`MANJU_ASR_MODEL_DIR` 不存在则 skip；存在时用测试内 ffmpeg 生成的正弦波音频断言「无语音 → 全部 missing」，再用仓库自带 1 条 3 秒真实语音样本（从 B 镜 10 截取，进 `tests/fixtures/`，< 100 KB）断言 aligned。
- 提交按关注点分组：①纯算法包 ②引擎/缓存/脚本 ③合成接线 ④前端；每次显式列路径。

## 14. 验收标准（我做，不是子代理自报）

1. **对齐率**：B 快照上 ≥ 30 个真实镜头（封天项目，跨 3 集）跑对齐，输出 ratio 分布；人工抽听 10 句确认：被念出的句子 aligned ≥ 95%，`missing` 列表里没有一句其实被念了（有则记录并调阈值）。
2. **时间误差**：人工标注 10 句开口时刻，`|cue.start − 开口| ` 中位数 ≤ 150 ms、最大 ≤ 300 ms。
3. **渲染**：3 集各抽 3 帧，字幕在安全区、不裁边、无乱码；再用现有 `subtitle_gate.sample_frames + VLM` 反向确认它能看见字幕（证明烧上去了）。
4. **缓存**：同集第二次合成 `cache_hits == 已采纳镜数`、`asr_shots == 0`。
5. **失败路径**：把模型目录改名后合成 → 失败文案含脚本名，旧成片 sha256 不变。
6. **关闭等价**：开关关时 `_run_concat_demuxer` 收到的参数与改动前逐项相等（守卫测试 + 演练脚本对比）。
7. **连播**：两集已烧字幕的成片合并，`merge_mode == stream_copy`，`film.srt` 时间偏移与 `chapters` 一致。
8. **门禁**：`arch_graph --check-layers` 0 上行边、`check_file_conventions` 无新增基线、`test_schema_guard`、干净副本全量、B 生产快照演练（独立手写对齐脚本 `/tmp/asr_probe/probe.py` 作为观察点，与新包结果逐句比对）。

## 15. 分期与派单切分

| 单元 | 内容 | 依赖 |
|---|---|---|
| U1 纯算法 | `align.py` `cues.py` `ass.py` `timeline.py` + 表驱动测试（含同音替换、缺句、多余语音、短句、倍速、xfade 偏移、转义） | 无 |
| U2 引擎与缓存 | `asr_worker.py`（子进程 CLI）、`engine.py`（wav 抽取、子进程、超时、锁）、`store.py`（懒建表）、`scripts/fetch_asr_model.py`、preflight 检查、`requirements.txt`、真模型冒烟 | U1 接口 |
| U3 合成接线 | `episode.py` 编排、`concat.py` 两条路径、`final_edit._compose`、报告键、`merge.py` film.srt、交付包、设置键 | U1、U2 |
| U4 前端 | 状态摘要、未出声列表、srt 下载；`CinemaPage.test.ts` 文案用例 | U3 报告结构 |
| 上线 | B 上 `fetch_asr_model.py` 装模型（data/ 不随部署同步，只做一次）→ preflight 绿 → 开开关 → 冒烟 1 镜对齐 + 1 集合成 → 抽帧核对 | 全部 |

U1 与 U2 可并行；U3 等两者；U4 只依赖 §9 的 JSON 形状，可与 U3 并行。

## 16. 风险与未定事项

- **模型改写台词**：模型偶尔不逐字念（同义改写），ratio 落到 0.4–0.7 会被判 missing。对策：报告里带该窗口的 ASR 原文让人一眼看出「其实念了但改了词」；P1 同音容错 + 手动标时间。
- **背景音乐/音效干扰**：SenseVoice 对噪声鲁棒（实测配乐下 98%），但纯音乐镜头可能幻听出几个字 → 只会进 `extra_speech`，不会烧。
- **平台安全区差异**：抖音/快手/视频号底部 UI 高度不同，`subtitle_margin_bottom` 可调；默认 400 px 取的是最保守值。
- **模型包获取**：GitHub Release 直链在国内可能慢；脚本支持 `--url` 镜像与 `--from-file`（商业化单租户离线交付时随安装包附带 163 MB 模型）。
- **字体授权**：wqy-zenhei 为 GPL+字体例外，商用无碍；如客户要求更好观感，装 Noto Sans CJK 后改 `MANJU_CJK_FONT_PATH` 即可，无代码改动。
- **B 无 bzip2**：脚本必须用 Python `tarfile`，已写进 §10。
