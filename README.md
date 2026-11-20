# Video Grade Pipeline

一个面向手机和相机素材的视频调色 Skill。它把源头判读、HDR 色调映射、风格
LUT、镜头匹配、Resolve PowerGrade、成片渲染和交付校验整理成一条可执行的工作流。

项目以本地处理为主，不依赖云端视觉模型。调色逻辑、LUT、PowerGrade 和校验脚本都
保存在仓库中，便于审计、复用和二次修改。

## 能力

- 区分 PQ、HLG、Log 和普通 SDR 素材，避免把灰片一律当作 Log。
- 将 HDR 素材色调映射到 Rec.709，再应用风格 LUT。
- 支持清晰度、Pro-Mist 辉光、暗角和强制 CFR 输出。
- 根据素材测量结果推导曝光、饱和度、对比和高光肩部。
- 提供示波器、肤色检查、镜头匹配和成片交付校验。
- 提供 Resolve 六节点 PowerGrade 与逐镜头 CDL 批量流程。
- 内置可复用 LUT、DRX 模板和调色配方。

## 环境要求

- Python 3.11 或更高版本
- `ffmpeg` 和 `ffprobe` 位于 `PATH`
- Python 包：`numpy`、`Pillow`、`zstandard`
- 可选：DaVinci Resolve 21 或兼容版本
- 可选的 Bash 主链路脚本需要 Git Bash、WSL 或 Linux/macOS 环境

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

## 安装为 Codex Skill

将仓库放到 Codex 的 skills 目录，目录名保持为 `video-grade-pipeline`：

```powershell
git clone https://github.com/christabelhgf5334-code/video-grade-pipeline.git `
  "$env:CODEX_HOME\skills\video-grade-pipeline"
```

如果未设置 `CODEX_HOME`，常用位置是：

```text
D:\CodexHome\skills\video-grade-pipeline
```

## 快速开始

先检查环境：

```powershell
python "<skill-dir>\scripts\doctor.py" --offline
```

判读素材类型：

```powershell
python "<skill-dir>\scripts\detect_source.py" "<素材目录>" --out source_check
```

批量运行 ffmpeg V5 主链路：

```bash
SRC_DIR=/path/in OUT_DIR=/path/out bash "<skill-dir>/scripts/render_v5.sh"
```

先处理最短的一条素材验证参数：

```bash
SRC_DIR=/path/in OUT_DIR=/path/out TEST_ONE=1 bash "<skill-dir>/scripts/render_v5.sh"
```

逐条测量并推导参数：

```powershell
python "<skill-dir>\scripts\pro_grade.py" "<clip-dir>" --out "<out-dir>" `
  --base-recipe "<skill-dir>\references\recipes\universal-v1.json" `
  --curve slog3 --hdr log
```

## Resolve 路线

先启动 DaVinci Resolve，并配置 Resolve Scripting API 环境。然后运行：

```powershell
python "<skill-dir>\scripts\doctor.py"
python "<skill-dir>\scripts\look_lab_resolve.py" prepare "<素材目录或单文件>" `
  --out "<批次目录>"
python "<skill-dir>\scripts\look_lab_resolve.py" render `
  --batch "<批次目录>" `
  --selections "<批次目录>\selections.json"
```

固定六节点：

1. `01 SOURCE`
2. `02 SHOT BALANCE`
3. `03 TONE MAP`
4. `04 SKIN PROTECT`
5. `05 LOOK`
6. `06 OUTPUT TRIM`

截至 2026-11-20，Resolve 环境自检和 `prepare` 已实跑通过；基于人工选择结果的
`render --selections` 最终成片步骤仍应在目标机器上完成一次验收。

## 输出与校验

```powershell
python "<skill-dir>\scripts\verify_delivery.py" `
  --src "<源目录>" --out "<成片目录>" --fps 60
```

交付默认检查：

- 时长与源素材误差小于 0.2 秒
- 严格 CFR 60 fps
- 帧数匹配时长
- 保留原分辨率与音频
- 成片编码和色彩标签符合目标交付要求

## 项目结构

```text
SKILL.md                 Skill 入口与工作流
agents/openai.yaml       Codex UI 元数据
assets/                  LUT、PowerGrade 与注册表
references/              调色方法、参数、QA 与排障文档
scripts/                 Python、Shell 和 Resolve 自动化脚本
```

更完整的流程和参数说明见 [SKILL.md](SKILL.md) 以及
[references/v5-pipeline.md](references/v5-pipeline.md)。

## 状态与限制

- ffmpeg V5 是批量直出的主链路。
- Resolve 是节点级精修和需要空间遮罩时的优先路线。
- HDR 素材应先完成正确的色调映射，再应用面向 Log 标定的 look。
- LUT 不应重复叠加，合并 LUT 与分级 LUT 只能选择一种链路。
- 本项目不替代人工视觉验收；示波器和数值检查只用于降低漏检风险。

## License

MIT License。详见 [LICENSE](LICENSE)。
