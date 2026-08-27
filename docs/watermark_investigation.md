# 供应商水印调查报告（技术口径）

范围说明：本阶段不考虑商业化与合规问题，只回答"能不能关、怎么关"这一个纯技术问题。合规/法规相关内容不在本报告中，另有独立归档（`logs/ai_labeling_compliance.md`），本报告不引用、不重复其结论。不涉及事后水印擦除/修补/裁切，不涉及整改方案。

---

## 一、参数对照表（第一优先）

结论先说：**本项目实际用到的两条供应商链路（图片=Seedream、视频=Seedance），关闭水印都只需要在请求体顶层加一个 `"watermark": false`，没有嵌套结构，没有 `logo_info` 这类对象。** 这一点已经在第二节用生产凭据实测确认（不是只查文档）。所有图像类用途（定妆照/多视角图/场景图/图生图）走的是**同一个函数、同一个请求体形状**，字段位置完全一致，下表按用途分行是为了对照方便，不代表底层机制不同。

| 供应商 / 端点 | 用途 | 模型 ID | 关闭水印字段 | 类型 | 嵌套位置 | 服务端默认值 | 我们当前是否传了 |
|---|---|---|---|---|---|---|---|
| 火山引擎 HiAgent 网关 → Doubao-Seedream 5.0<br>`POST {base}/images/generations` | 定妆照 | `d7ute7ppcc7n89uuqqp0` | `watermark` | `boolean` | **请求体顶层**，与 `model`/`prompt`/`size` 同级 | `true`（加水印）—— 已实测确认 | **否**（`app/hiagent.py:2917` 的 payload 里没有这个键） |
| 同上 | 多视角图（profile/three_quarter 等 QA 视角） | 同上 | `watermark` | `boolean` | 顶层 | `true` | 否 |
| 同上 | 场景定场图 | 同上 | `watermark` | `boolean` | 顶层 | `true` | 否 |
| 同上（图生图/i2i，携带参考图） | 图生图（参考图编辑，`kind=image_edit`） | 同上 | `watermark` | `boolean` | 顶层，与非标的 `image` 字段（参考图 data URL）平级 | `true` | 否 |
| 火山引擎 HiAgent 网关 → Doubao-Seedance<br>`POST {base}/contents/generations/tasks` | 视频生成 | `d7jf6nd5boeaebtfbdqg` | `watermark` | `boolean` | **请求体顶层**，与 `model`/`content` 同级 | `false`（不加水印）—— 已实测确认 | 否（`app/seedance.py:60` 的 payload 里没有这个键） |
| MiniMax H3（自建 ComfyUI，`trycloudflare.com` 隧道，非官方托管 API） | 视频生成（当前非生效供应商） | `minimax-h3` | 不适用 | — | — | 未知——没有官方 API 文档，请求体字段（`acceleration`/`turbo_profile`/`video_vae`/`scheduler`/`steps`）是自建 ComfyUI 工作流参数，需要直接查那台私有服务自己的接口定义，不是可查证的"供应商官方参数" | 不适用（未测试，非生产路径） |

### 最小请求体片段（可直接照改）

图片生成 / 多视角图 / 场景图（纯文生图，`kind=image_generate`）：
```json
{
  "model": "d7ute7ppcc7n89uuqqp0",
  "prompt": "……",
  "n": 1,
  "size": "1440x2560",
  "watermark": false
}
```

图生图 / 参考图编辑（`kind=image_edit`，多一个 `image` 字段）：
```json
{
  "model": "d7ute7ppcc7n89uuqqp0",
  "prompt": "……",
  "n": 1,
  "size": "1440x2560",
  "image": "data:image/jpeg;base64,……",
  "watermark": false
}
```

视频生成：
```json
{
  "model": "d7jf6nd5boeaebtfbdqg",
  "content": [{"type": "text", "text": "……"}],
  "watermark": false
}
```

---

## 二、实测对照（第二优先）

测试方式：脚本直接用 httpx 发请求，不经过 `app.hiagent`/`app.db` 任何写路径，未写入 `provider_calls` 表、未写入 `projects/` 目录、未改数据库。凭据/端点/模型 ID 从 `data/manju.db`（只读连接）读取，与生产完全一致。产出全部落在 `/tmp/watermark_test/`。脚本：`run_test.py`（图片）、`run_seed_test.py`（图片+seed）、`run_video_test.py` + `run_video_test2.py`（视频）。

### 2.1 图片：不带参数 vs `watermark:false` vs `watermark:true`

同一个 prompt（角色全身像描述）、同一尺寸 `1440x2560`（与生产 `config.REF_IMAGE_SIZE` 一致），发了 3 次请求。**三次请求全部 HTTP 200，响应体 JSON 结构完全相同**（`{"model","created","data":[{"url"}],"usage",...}`），watermark 参数不影响响应 JSON 的字段结构，唯一区别在图片像素本身：

| 请求 | 结果（用 Read 工具查看，并对右下角做了 3× 放大裁剪二次核对） |
|---|---|
| 不带 `watermark` 参数 | 右下角有一个白色圆角矩形徽章，内容为 **"AI生成"四个字**（仅此文字，没有任何厂商品牌名，没有"Seedream"/"豆包"/"即梦"等字样） |
| `"watermark": false` | 右下角**完全干净**，3× 放大裁剪后确认无任何徽章轮廓、无文字残留 |
| `"watermark": true` | 与"不带参数"表现一致：同样的白色圆角矩形徽章，同样的"AI生成"文字，位置、字体、大小肉眼看不出差异 |

放大裁剪核对文件：`/tmp/watermark_test/baseline_no_param_BRcrop.jpg`、`watermark_false_BRcrop.jpg`、`watermark_true_BRcrop.jpg`（右下角 3×）；另外核对了左上角（`*_TLcrop.jpg`），**没有发现第二处水印**。

**回答"不带参数时到底是什么"**：只有一层——固定样式的白底圆角徽章 + "AI生成" 四字，右下角。**没有观察到独立的厂商品牌 logo 层**（不是"即梦"或"Seedream"字样，也不是图形 logo，只有这一条文字标记）。

**回答"关闭后是全部消失还是只消失一层"**：只有这一层，`watermark:false` 后**全部消失**，没有残留的第二层。

### 2.2 `seed` 参数补充测试

尝试给同一个 prompt 加 `"seed": 424242`，两次请求（`watermark:true` / `watermark:false`）均 **HTTP 200，不报错**，说明该字段被接受。但对比两张图的人物构图、服装、发型均不同（不是同一个人物姿态的复现），**说明 `seed` 在这个网关/模型组合上没有起到锁定构图的作用**（要么被静默忽略，要么该模型的确定性复现不生效）——如实记录这一点，不代表 watermark 结论受影响：两次结果里，`watermark:true` 图右下角依然是"AI生成"徽章，`watermark:false` 图依然干净，与 2.1 的结论完全一致，说明水印开关不依赖构图是否相同。相关文件：`seedtest_wm_true.jpg`、`seedtest_wm_false.jpg` 及对应 `*_BRcrop.jpg`。

### 2.3 视频：不带参数 vs `watermark:false` vs `watermark:true`

同一个 prompt（竹林侠客，纯文本生视频，无参考图/无 seed），发了 3 次请求，全部 `status: succeeded`，输出均为 720×1280、24fps、约 5.04 秒。三次生成时间不同（无 seed，构图也不同），但水印位置固定在画面右下角，用 ffmpeg 抽取首帧、中间帧、尾帧逐一核对：

| 请求 | 首帧/中间帧/尾帧结果 |
|---|---|
| 不带 `watermark` 参数 | 三帧右下角均**无水印** |
| `"watermark": false` | 三帧右下角均**无水印** |
| `"watermark": true` | 首帧、尾帧右下角均**清晰出现**与图片端完全同款的白底圆角"AI生成"徽章；中间帧因该帧角色姿态遮挡了该区域未能确认，但首尾两帧已足够定论 |

放大裁剪核对文件（首帧）：`baseline_frame0_BRcrop.jpg`、`wmfalse_frame0_BRcrop.jpg`、`wmtrue_frame0_BRcrop.jpg`。原始视频：`video_baseline_no_param.mp4`、`video_watermark_false.mp4`、`video_watermark_true.mp4`。

**结论**：视频端水印内容与图片端**完全同款**（同样的"AI生成"文字徽章，同样的右下角位置，没有独立的品牌 logo 层）。不带参数时（即当前生产代码的实际行为）本来就不带水印，因为该网关上这个字段的服务端默认值是 `false`；`watermark:true` 能确认性地把它加回来，证明这不是一个失效/无效参数。

### 2.4 报错情况

**三组测试（图片 3 次、视频 3 次、seed 补充 2 次，共 8 次请求）全部 HTTP 200/succeeded，没有出现任何报错**，因此没有原始错误响应需要贴出。`watermark` 字段无论传 `true`/`false`，服务端都正常识别并按值生效，不是一个会被网关静默丢弃或校验拒绝的字段。

---

## 三、代码接入点（第三优先，只指位置，不改代码）

### 3.1 图片（覆盖定妆照/多视角图/场景图/图生图——全部同一个函数）

- **位置**：`app/hiagent.py:2917`
  ```python
  payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": size}
  ```
  这是 `generate_image()` 函数内构造请求体的唯一位置。`app/portraits.py`（定妆照）、`app/refs.py`（全身立绘）、`app/scenes.py::_generate_scene_image`（场景定场图）、`app/multiview.py::_generate_image`（多视角/关键帧）**全部调用这一个函数**，没有第二条图像生成路径——因此**只需要改这一处**就能覆盖所有图像类用途，不用在每个调用方分别加。
- **参数值应该从哪来**：`generate_image()` 当前没有 `watermark` 形参，是否加、加成什么值，取决于要不要做成可调：
  - 若做**硬编码常量**：与 `app/scenes.py:340-346` 里直接用 `config.REF_IMAGE_SIZE` 的写法同构，可以在 `app/config.py` 加一个模块级常量（`REF_IMAGE_SIZE = "1440x2560"` 定义在 `app/config.py:363`，是同类常量的现有先例），`generate_image()` 里直接引用。
  - 若做**运行时可切换的配置项**：`app/config.py:456-457` 已有同类先例——`DEFAULT_SETTINGS` 字典里的 `"watermark_qa_mode": "reject"` 这一条，配合 `get_setting("watermark_qa_mode")` 读取（用法见 `app/multiview.py:90-91` 的 `watermark_qa_mode()` 函数）。同样的模式可以复用：在 `DEFAULT_SETTINGS` 加一条新 key，`generate_image()` 内部 `get_setting(...)` 读取后写入 payload。
  - 两种模式在仓库里都有现成先例，具体选哪种、默认值给什么，属于实现决策，本报告不替你定。

### 3.2 视频

- **位置**：`app/seedance.py:60-61`
  ```python
  payload = {"model": model, "content": content}
  if return_last_frame:
      payload["return_last_frame"] = True
  ```
  这是 `SeedanceAdapter.create_video_task()` 构造请求体的唯一位置，紧跟着 `return_last_frame` 这个可选布尔字段的写法（`if <条件>: payload["<key>"] = <value>`）就是现成的插入模式范例。这一个类是视频生成的唯一供应商适配器，实际发起点在 `app/media_exec/run_job.py:3703`（`await hiagent.create_video_task(...)`，该文件由 `app/worker.py` 用 `exec()` 注入自身命名空间运行），但请求体本身不在 `run_job.py` 里拼，改 `create_video_task()` 一处即可覆盖。
- **参数值来源**：同 3.1，硬编码或 `app/config.py` 配置项两种模式都有先例，未替你决定。

### 3.3 不需要改的地方

`app/image_providers.py` 只负责参考图（`image` 字段）怎么挂到 payload 上（"seedream"/"openai" 方言开关），跟 `watermark` 无关，不需要碰。`app/media_exec/*.py`（`run_job.py` 等）只是调用 `hiagent.create_video_task()`/上游图像生成函数，不直接拼 payload，同样不需要改。

---

## 附：本次调查产生的文件

- 测试脚本：`/tmp/watermark_test/run_test.py`、`run_seed_test.py`（图片）、`run_video_test.py`、`run_video_test2.py`（视频）
- 测试产出与放大裁剪核对图：`/tmp/watermark_test/*.jpg`、`*.mp4`（系统临时目录，未写入仓库或数据库）
- 本报告未修改 `data/manju.db`、`projects/` 或任何 `app/` 代码，未执行任何 git 操作。
