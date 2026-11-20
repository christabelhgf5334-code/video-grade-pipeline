---
name: video-grade-pipeline
description: "用一条已验证的调色链路处理手机/相机视频并交付剪辑软件：HDR(PQ/HLG)色调映射 → 风格 LUT → 清晰度 → Pro-Mist 辉光 → 暗角 → 强制恒帧输出，附带 Resolve 六节点 PowerGrade 路线、逐镜头 CDL、批量渲染与成片校验。当用户要做视频调色、素材发灰、导入剪映显示30帧、多条素材统一风格、需要 LUT 或达芬奇工程时使用。"
---

# 视频调色管线

一条**已经过实战检验**的调色链路，加上批量处理和校验。核心主张：**先判读，再校正，最后风格**；
顺序错了后面全废。

依赖：`ffmpeg`/`ffprobe` 在 PATH；Python 有 `numpy` + `Pillow`。

## 什么时候用

- 手机拍的 HDR 竖屏素材，画面发灰
- 导入剪映/CapCut 显示 30 帧
- 一批同机位素材要统一风格
- 需要交付 `.cube` LUT 或在达芬奇里继续调

## 路线选择：达芬奇优先

**调色以达芬奇为主路线**，ffmpeg 负责批量、快出片和无达芬奇时的降级。

| 需求 | 走哪条 |
|---|---|
| 要最好的效果、节点级精修、场景参考色彩管理 | **达芬奇** |
| 逐帧跟踪、物体级窗口、局部提亮 | **达芬奇**（ffmpeg 只能静态遮罩）|
| 一批素材快速统一风格、出预览 | ffmpeg |
| 没有达芬奇 / 只需要 LUT 交付 | ffmpeg |

### 达芬奇路线的前置条件（缺一不可）

1. **达芬奇必须处于运行状态** —— 脚本是 attach 到已启动的进程，不是无头模式。
   实测：模块能导入，Resolve 未运行时 `scriptApp('Resolve')` 返回 `None`。
2. 环境变量（本机已验证路径）：
   ```
   RESOLVE_SCRIPT_API = C://ProgramData//Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting
   RESOLVE_SCRIPT_LIB = D://Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll
   ```
   并把达芬奇安装目录加入 `PATH` / `os.add_dll_directory()`，否则 `fusionscript.dll`
   会报"找不到指定的模块"（这是缺依赖 DLL，不是路径错）。
3. `pip install zstandard` —— PowerGrade 模块压缩用；**注意装到脚本实际使用的解释器**
   （本机 `pip` 默认装到了另一个 Python，必须用 `python -m pip`）。

### 达芬奇路线

```bash
python "<skill-dir>/scripts/doctor.py"            # 环境自检（不带 --offline 会连 Resolve）
python "<skill-dir>/scripts/look_lab_resolve.py" prepare "<素材目录或单文件>" --out "<批次目录>"
python "<skill-dir>/scripts/look_lab_resolve.py" render --batch "<批次目录>" \
    --selections "<批次目录>/selections.json"
```

`prepare` 会：建 Resolve 工程 → 逐镜头算 CDL → 推四个 look → 套六节点 PowerGrade →
渲 1080p 预览 → 写联系表与 `selection_template.json`。

六节点固定：`01 SOURCE`（HDR 放色调映射 / Log 放源 LUT）、`02 SHOT BALANCE`（逐镜头 CDL）、
`03 TONE MAP`、`04 SKIN PROTECT`、`05 LOOK`、`06 OUTPUT TRIM`。
`assets/resolve/powergrades/` 有 7 个 `.drx` 模板，`looks.json` 有 5 个 look。

> **诚实标注（2026-11-20 更新）**：`doctor.py` 全项通过（Resolve 21.2.0.6）；
> `look_lab_resolve.py prepare` 已**实跑成功**，产出 `status: preview_complete`
> （建工程 → 逐镜头 CDL → 六节点 PowerGrade → 4 个 look 预览 → 联系表）。
> **`render --selections` 的最终成片渲染仍未实跑**——那一步需要先选 look。

## 主链路（V5，ffmpeg 路线）

没有达芬奇、或需要批量出片时走这条。多级调色、专业版推导、示波器、镜头匹配都在这一侧。

```
HDR(PQ) → 色调映射(Rec.709) → look LUT(v3) → 清晰度 → Pro-Mist 辉光 → 暗角 → CFR 60
```

```bash
SRC_DIR=/path/in OUT_DIR=/path/out bash "<skill-dir>/scripts/render_v5.sh"
```

默认使用内置的 `assets/luts/selected-color-look-v3.cube`。每一步的做法、参数范围和
**为什么这么定**，见 [references/v5-pipeline.md](references/v5-pipeline.md)。

**先渲最短的一条验证**（2 分钟出结果，别等 2 小时）：

```bash
SRC_DIR=/path/in OUT_DIR=/path/out TEST_ONE=1 bash "<skill-dir>/scripts/render_v5.sh"
```

可关的开关：`CLARITY=0` 关清晰度、`GLOW=0` 关辉光、`VIGNETTE=` 关暗角。
**除非有明确理由，不要动 `-r 60 -fps_mode cfr`。**

## 第 0 步 · 判读（不可跳）

```text
python "<skill-dir>/scripts/detect_source.py" "<素材目录>" --out source_check
```

小米等手机的 HDR 模式和 Log 模式**元数据完全相同**（bt2020 + 10bit + `transfer=unknown`），
`ffprobe` 分不出来，但处理方式相反：

| 源头 | 做法 | 用错的后果 |
|---|---|---|
| PQ / HLG | 色调映射（本管线默认） | 当 Log 处理 → 高光撞白、过饱和 |
| Log | 源 LUT（**不能**直接用本管线的 LUT） | 当 HDR 处理 → 发灰发暗 |

脚本会把同一帧按 sdr / pq / hlg 三种解释各渲一遍打分并出三联图。
**分数只是排序，必须看图确认。** 详见 [references/source-pipeline.md](references/source-pipeline.md)。

> `灰片` ≠ `Log`。HDR 素材在普通播放器里同样是灰的。

## 专业版：逐条测量后推导（素材已知时优先）

固定 LUT 只能做到"不出错"。实测五段不同题材的**平均饱和从 0.09 到 0.41（4.5 倍差距）**，
同一条 LUT 必然让水果变霓虹、瀑布还是灰的。

```bash
python "<skill-dir>/scripts/pro_grade.py" CLIP_DIR --out OUT_DIR \
    --base-recipe references/recipes/universal-v1.json --curve slog3 --hdr log
```

它对每条素材：测中间调/动态范围/饱和度 → 推导曝光·饱和·对比·高光肩部 → 烘独立 LUT
→ **闭环实测**（套回探针帧验证，偏差 >0.035 自动重烘，最多 3 轮）→ 渲染。

两条铁律（都是被数据逼出来的）：

1. **饱和用相对提升**（原值 × 1.55，钳 0.16–0.46），不用绝对目标。近乎单色的瀑布就该低饱和。
2. **推导完必须闭环实测**。只看公式会偏 25%——口径不一致（推导用 75 百分位、验证用平均）。

参数表输出到 `OUT_DIR/pro_params.json`。限制与处理见
[references/pro-grade.md](references/pro-grade.md)。

## 风格定制

内置 LUT 偏暖调美食。换题材要另烘：

```text
python "<skill-dir>/scripts/preview_looks.py" frame.png --looks looks.json --out choose.jpg
python "<skill-dir>/scripts/build_look_lut.py" references/recipes/warm-appetite.json --out my.cube
```

`references/recipes/warm-appetite.json` 是已验证的完整配方（一级 + 二级色域 + 胶片层），
重新烘焙与交付版差 **0.16/255**。改哪个参数都写在这张表里，可追溯。

**给用户看方案，不要自己替他定。** 同一帧、同一裁切、同尺寸并排——跨帧比较等于在比较镜头。

## 二级（色域 vs 空间，别混为一谈）

- **色域级**（按颜色选中）：能烘进 LUT ✅ 本管线的 LUT 已包含
- **空间级**（按位置选中）：**烘不进 LUT**，需要遮罩

要做空间分离（主体提亮 + 背景压暗）：

```bash
python "<skill-dir>/scripts/make_mask.py" --size 2160x3840 --preset band --param 0.30 \
    --feather 0.055 --out mask.png
```

**必须看一眼画面再定分界线**——实测反例：默认 0.30 把一条素材的上排烤串（从 18% 开始）
误判成背景压暗了。

若要接进 ffmpeg，注意 `maskedmerge` **只在 RGB 域可用**（YUV 上会只门控亮度、整片去色），
且**白遮罩选的是第二路输入**，写法是 `[背景][主体][遮罩]maskedmerge`。

## Resolve 路线

需要逐帧跟踪、物体级遮罩、节点级精修时走这条。六节点固定：

1. `01 SOURCE` — 源头转换（HDR 放色调映射 / Log 放源 LUT）
2. `02 SHOT BALANCE` — 逐镜头 CDL
3. `03 TONE MAP`
4. `04 SKIN PROTECT`
5. `05 LOOK`
6. `06 OUTPUT TRIM`

`assets/resolve/powergrades/` 里有 7 个 `.drx` 模板和 `looks.json`（5 个 look：
`documentary_resolve` / `warm_human` / `cool_editorial` / `market_resolve` / `portrait_soft`）。

**注意**：这批 look 是为 Log 信号标定的。HDR 素材必须先色调映射到 709 再套，
否则饱和度会高出 50%（实测 0.585–0.673 vs 正确的 0.432）且高光撞白。

> 逐节点该放什么、顺序为什么不能换，见
> `references/resolve-node-order.md`（Resolve 多级调色顺序教程）。

## 交付前必查：示波器 + 镜头匹配

**只看画面不够。** 五条成片实测中间调跨度 0.538、色偏跨度 0.379——每条单独都"对"，
连起来是五条不同的片子。

```bash
python "<skill-dir>/scripts/scopes.py" OUT/xxx_graded.mp4 --out qa.jpg
python "<skill-dir>/scripts/match_shots.py" --in GRADED_DIR --out MATCHED_DIR --src SRC_DIR
```

示波器给硬指标：溢出 <1%、死黑 <1%、R−B <±0.10、**肤色色相 30–37°**。
镜头匹配选基准对齐曝光/对比/白平衡，实测把中间调跨度收窄 **75%**（0.538→0.136，含暗部守卫的最终一轮）。

**饱和度默认不匹配**——瀑布本就该比水果淡，那是内容不是缺陷。同场景才加 `--match-saturation`。
详见 [references/qa-and-matching.md](references/qa-and-matching.md)。

## 校验

```text
python "<skill-dir>/scripts/verify_delivery.py" --src "<源目录>" --out "<成片目录>" --fps 60
```

| 项 | 标准 |
|---|---|
| 时长 | 与源差 < 0.2s |
| 帧率 | `avg_frame_rate == 60/1`（严格，不是 59.7） |
| 帧数 | == 时长 × fps（±2），用 `-count_packets` 数 |
| 分辨率 | 竖屏 2160×3840 |
| 音频 | 保留原轨 |

## 交付时要主动说明

1. **帧率**：文件是严格 CFR。若软件显示 30 帧，那是**工程帧率**（剪映新建草稿默认 30），
   改工程设置即可，不必返工文件。
2. **LUT 不要叠用**：合并版套原始素材，分级 LUT 串联，二选一。叠了会过饱和。
3. **画质取向**：暗部比原片沉是刻意的（胶片曲线）。嫌暗就调 `curve`/`shoulder`，不要整体提亮。

## 参考

- [references/v5-pipeline.md](references/v5-pipeline.md) — **主链路逐段说明与参数**（先读）
- [references/levels-pipeline.md](references/levels-pipeline.md) — **三级管线**：分级原理、致命坑、**度量陷阱**、覆盖率闸门
- [references/pro-grade.md](references/pro-grade.md) — **专业版**：推导规则、闭环校准、已知限制
- [references/qa-and-matching.md](references/qa-and-matching.md) — **示波器 QA 与镜头匹配**
- [references/source-pipeline.md](references/source-pipeline.md) — 源头类型判定
- [references/spatial-layer.md](references/spatial-layer.md) — 清晰度/辉光/暗角配方与三个坑
- [references/recipes/warm-appetite.json](references/recipes/warm-appetite.json) — 已验证配方

## 脚本索引

| 脚本 | 用途 |
|---|---|
| `render_v5.sh` | **主链路批量渲染**，强制 CFR，断点续跑，ETA |
| `pro_grade.py` | **专业版**：逐条测量→推导参数→闭环校准→渲染 |
| `make_curve_lut.py` | 烘焙 Log 编/解码 LUT（ffmpeg 没有原生 Log 解码）|
| `auto_grade.py` | 场景分析：中间调/范围/饱和/偏色/肤色/天空/植被 |
| `detect_source.py` | 判定 PQ / HLG / Log / SDR |
| `build_look_lut.py` | 按配方烘 LUT（一级→二级→胶片层），可导出分级 LUT |
| `preview_looks.py` | N 个方案同一帧并排 |
| `make_mask.py` | 空间遮罩（预设/多边形 + 羽化） |
| `analyze_domains.py` | 色域聚类，判断能否用色相分离 |
| `check_skin.py` | 量化皮肤是否被推动（比一级 vs 一级+二级） |
| `scopes.py` | **示波器**：波形/分离/矢量图 + 溢出·色偏·肤色色相 |
| `measure_levels.py` | **分离度校验**：主体 vs 紧邻背景环 + 背景细节 + 暗角污染检测 |
| `match_shots.py` | **镜头匹配**：选基准、算偏移、烘匹配 LUT、复测离散度 |
| `verify_delivery.py` | 成片校验 |
| `fit_transfer.py` / `transfers.py` / `grade_engine.py` | 传输曲线库与可编程引擎（进阶） |
