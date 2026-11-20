# 踩坑目录：现象 → 原因 → 修法

每条都是实测踩出来的，不是推测。按"现象"查最快。

---

## 1. 画面发灰、暗部发白、颜色寡淡，像没上色的 SDR

**原因**：素材是 HDR，但 `color_transfer` 标签是 `unknown`（手机常见），解码器按 SDR 解释。

**判定**：`ffprobe` 同时出现 `color_primaries=bt2020` + `pix_fmt=yuv420p10le` + `transfer=unknown`。

**修法**：手动指定输入色彩空间，不能指望标签：

```
zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:rin=full:t=linear:npl=100,
format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,
zscale=t=bt709:m=bt709:r=tv,format=yuv420p10le
```

PQ 用 `tin=smpte2084`，HLG 用 `tin=arib-std-b67`。**猜错会明显不对**：按 HLG 解 PQ 素材会偏暗偏灰，按 PQ 解 HLG 会过曝。用同一帧渲两种对比即可确定。

`tonemap=hable:desat=0` 是好默认：高光滚降自然、有色物体不褪色。mobius 更保中调，reinhard 更平。

---

## 2. LUT 烘出来和视频对不上 / 完全错乱

**原因**：把 `zscale`（或任何依赖色彩空间标签的滤镜）放进了 LUT 烘焙链。

**为什么**：烘 LUT 的做法是把一张恒等色块图（PNG，无 BT.2020 标签）推过滤镜链。`zscale` 会把它当 SDR 解释，于是烘出来的映射和真实视频毫无关系。

**修法**：**两级流程**
1. 先把 HDR 烘成 Rec.709 母版文件（真实编码，不是 LUT）
2. 在母版上做纯点运算的风格，再烘 LUT

`build_look_lut.py` 的 `primary_chain` **只能**放 `colorbalance`/`eq`/`curves`/`vibrance`/`colorchannelmixer`。放 `gblur`/`blend`/`unsharp` 同样会静默出错——它们是空间滤镜。

---

## 3. 导入剪映/CapCut 显示 30 帧（文件其实是 60）

**原因**：源素材是 **VFR**（手机录制的常态，`avg_frame_rate` 往往是 60.06 而不是 60），转码时没强制恒帧。ffmpeg 会把撞时间戳的帧合并成 33ms 一格，输出 `avg_frame_rate` 变成 59.6~59.9。剪辑软件读到变帧就归一化，最常见的结果就是按 30fps 处理。

**判定**：
```bash
ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of csv=p=0 out.mp4
# 必须是 60/1；若是 24660/413 这类分数，就是 VFR
ffprobe -v error -select_streams v:0 -show_entries packet=duration_time -of csv=p=0 out.mp4 | sort | uniq -c
# 必须只有一种间隔；出现两种就是 VFR
```

**修法**：编码时**始终**加

```
-r 60 -fps_mode cfr
```

已验证：加之前 411 帧（409×16.7ms + 2×33.3ms），加之后 412 帧全 16.7ms，`avg_frame_rate=60/1`。

**不要试图用流拷贝补救**——试过四种全部无效，只能重编码：

| 方法 | 结果 |
|---|---|
| `-c copy -r 60` | avg 不变 |
| `-c copy -fps_mode cfr -r 60` | avg 不变 |
| `-c copy -video_track_timescale 15360 -r 60` | avg 不变 |
| `-c copy -bsf:v setts=pts=N*256:duration=256` | 包间隔不变 |

**还有一层**：码流内部的 VUI 也要对。用 `-bsf:v trace_headers` 检查：
```bash
ffmpeg -loglevel trace -i out.mp4 -c copy -bsf:v trace_headers -f null - 2>&1 \
  | grep -E "vui_num_units_in_tick|vui_time_scale"
# 期望 num_units_in_tick=1, time_scale=60
```

**如果三层（容器 / 时间戳 / VUI）都是 60，软件仍显示 30，那就是软件的工程帧率**，不是文件问题。剪映新建草稿默认 30fps，必须在动剪辑之前改成 60；否则导出选 60 也只是把 30 帧重复成 60。

---

## 4. blend 之后画面出现洋红/绿色块（"迷幻色"）

**原因**：在 YUV 上做 `blend`。blend 按平面混合，`softlight`/`screen` 被同时施加到 U/V 色度平面上，色度被打乱。

**试过且失败的写法**：
- `blend=all_mode=softlight` → 彩块
- `blend=c0_mode=softlight:c1_mode=none:c2_mode=none` → 仍有彩块
- `format=yuv444p10le` 再 blend → 颜色对，但整体发灰变平

**唯一正确做法**：转到**浮点 RGB** 再混合

```
...,format=gbrpf32le,split[m][t];[t]scale=iw/4:ih/4,gblur=sigma=10,scale=iw*4:ih*4[g];
[m][g]blend=all_mode=softlight:all_opacity=0.37,format=yuv420p10le,...
```

代价约 +22% 时间。辉光在 1/4 分辨率上做再放大——模糊本身是低频，视觉无差。

---

## 5. 想省内存把 `gbrpf32le` 换成 `gbrp16le`，画面整体变暗

**现象**：画面平均亮度从 166 掉到 119（**暗了 33.8/255**），码率从 44 Mbps 涨到 149 Mbps（文件大三倍）。

**原因**：16 位整数 RGB 下 blend 的数值语义和浮点不同，等于换了一套混合数学。

**修法**：**不要替换浮点格式**。要省内存就只限线程：

```
-filter_threads 2 -threads 6
```

已验证：限线程后输出与原配置**逐字节相同**（16632399 bytes，像素差 0.0），只是慢一点。

**教训**：任何"等价替换"都必须用像素对比验证，不能靠推理。`mean|d|` 两行代码就能证伪一个假设。

---

## 6. 渲染越跑越慢，最后像卡死（CPU 占满但产出极少）

**现象**：ffmpeg 占 **836% CPU**，但产出只有 0.4 MB/s（正常 3.5 MB/s），59 分钟只完成 19%。

**原因**：**空闲内存耗尽**。4K 浮点帧每帧约 99 MB，滤镜图同时持有数帧；当可用内存掉到 2.4 GB 时系统开始重度换页，CPU 时间全耗在内存管理上。

**判定**：`Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory` 看 `PagesPerSec`（>2000 异常）和 `AvailableMBytes`。

**修法**：
- 先关掉吃内存的应用（浏览器多标签、微信、桌面版 ChatGPT 都是大头）
- ffmpeg 侧加 `-filter_threads 2 -threads 6`
- 长片段（>2 分钟）可拆段渲染再 `-c copy` 拼接，每段结束释放内存

---

## 7. 杀掉 ffmpeg 后又有新的 ffmpeg 起来

**原因**：`taskkill ffmpeg.exe` 只杀进程，**bash 循环还在跑**，立刻启动下一条。

**后果**：如果同时起了第二个批处理，两个进程会并发写同一批文件，产物互相覆盖。

**修法**：停整个任务，不是停进程。用任务管理工具停掉后台任务，确认 `ffmpeg` 计数为 0 再重启。

**另一个坑**：不要在批处理运行中覆盖它的脚本文件——bash 按字节偏移续读脚本，改内容后正在运行的实例会读到错位的内容。**先停任务，再改脚本**。

---

## 8. `nb_frames` 和实际帧数对不上

**原因**：`nb_frames` 是容器元数据，VFR 素材上经常不准。

**修法**：一律用 `-count_packets` 数真实包数：

```bash
ffprobe -v error -select_streams v:0 -count_packets \
  -show_entries stream=nb_read_packets -of csv=p=0 file.mp4
```

校验公式：`帧数 == 时长 × fps`（±2 帧内）。这是恒帧的直接证据，比看 fps 字段可靠。

---

## 9. LUT 套了两遍，画面过饱和

**原因**：合并版 LUT（一级+二级）被套在了已经调过色的画面上。

**判定**：拿"直算结果"比对，差 12.7/255（容差应 <2）。

**修法**：
- 合并版 → 只能套**原始素材**
- 要在软件里分开调 → 用「纯一级 LUT + 纯二级 LUT」串联
- 用 `build_look_lut.py --emit-stages` 一次导出各级 LUT

---

## 10. 取样颜色取错了区域

**现象**：量出来的"肤色"是蓝色。

**原因**：靠目测坐标框区域，框到了背景。**我连续框错三次**。

**修法**：不要猜坐标。
- 要知道画面**有哪些颜色** → k-means 聚类（k=14，看占比排序）
- 要定位**某个物体在哪** → 画网格线截图再看

聚类能直接告出关键事实，比如"食物色相 31.7°、皮肤 30.5–34.3°，完全重叠"——这种结论靠框区域永远得不到。

---

## 11. 皮肤和主体色相重叠，限定器分不开

**实例**：食物 S≈0.89，皮肤 S≈0.39，看似饱和能分开；但皮肤**受光面高光饱和度能到 0.9**。门槛设 S>0.55 → 整个前臂被框进去（权重 0.63）。提到 S>0.80 → 手臂降到 0.13，但食物也从 0.48 掉到 0.40。

**两个一起掉 = 这个维度已经没区分度了**，不是参数没调好。

**修法（按优先级）**：
1. **放弃用这个维度分离**，主体只做极轻的处理（+2% 饱和 / +3.8% 亮度）
2. **分离感交给背景**：找画面里**唯一的冷色**（青蓝背景），压它（−40% 饱和 / −15% 亮度）——没有物体共享这个色相，绝不会误伤皮肤
3. 真要精确分离 → 只能上空间窗口（power window），那就不能烘 LUT 了

**原则**：分离感靠"压背景"，不靠"推主体"。前者没有溢色和伤皮肤的风险。

---

## 12. 遮罩图看起来一塌糊涂，其实调整很轻

**原因**：遮罩可视化通常是高不透明度混合，会把权重**视觉放大**。

**实例**：遮罩图显示手臂被大片染色，但实际像素影响只有 **1.22/255**（最大 14）。差点因此删掉一个无害的调整。

**修法**：用**开关差分**代替看图——把某一项开/关各算一遍相减：

```
mean |d| < 2/255  → 不可见，可保留
mean |d| 3~8      → 明显但克制
mean |d| > 15     → 强调整，要重新审视
```

---

## 13. 中文标签显示成方块

**原因**：PIL 用 Arial/默认字体渲染中文。

**修法**：Windows 用 `C:/Windows/Fonts/msyhbd.ttc`（粗）或 `msyh.ttc`（常规）。

---

## 14. 竖屏片子导出变成横的

**原因**：手机是 3840×2160 存储 + 旋转矩阵。ffmpeg 会自动转正，输出物理分辨率变成 2160×3840。

**修法**：
- 不要再加 `-vf transpose`
- 导出/渲染尺寸写 **2160×3840**
- 达芬奇里渲染尺寸也要手写成竖屏，否则出来是横的

---

## 15. GPU 用不上，别浪费时间

本机实测结论（可作为判断参考）：
- ffmpeg 构建要求驱动 ≥610.00，实际 576.80 → `hevc_nvenc` 直接报错
- 该构建**没有** `libplacebo`/`tonemap_cuda`/`tonemap_opencl`，HDR 映射只能 CPU
- **即使 NVENC 可用也只省约 25%**：x264 fast 5.40 fps 已是滤镜链上限，说明瓶颈是 4K60 浮点色调映射，不是编码器
- 3 路并行 3.30 fps ≈ 单进程 3.24 fps → CPU 已经跑满，并行无收益

**结论**：4K60 HDR 的瓶颈在色彩空间转换。要真正快，用达芬奇的 GPU 管线，而不是在 ffmpeg 上折腾硬件加速。
