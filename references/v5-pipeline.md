# V5 调色链路（已验证、被采纳的版本）

这是经过多轮打磨、用户实际认可的那条链路。**不要随便改动顺序和参数**，改之前先看"为什么"。

```
HDR(PQ) → 色调映射(Rec.709) → look LUT(v3) → 清晰度 → Pro-Mist 辉光 → 暗角
        → x265 10bit → 强制 CFR 60
```

---

## 逐段说明

### 1 · 色调映射（HDR → Rec.709）

```
zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:rin=full:t=linear:npl=100,
format=gbrpf32le,
zscale=p=bt709,tonemap=hable:desat=0,
zscale=t=bt709:m=bt709:r=tv,
format=yuv420p10le
```

- `tin=smpte2084` **必须手动写**：小米等手机 HDR 素材的 `color_transfer` 是 `unknown`，
  不写就按 SDR 解码，画面发灰。
- `tonemap=hable:desat=0`：高光滚降自然，`desat=0` 保住彩色物体的饱和度（默认会褪色）。
- HLG 素材把 `tin` 换成 `arib-std-b67`。

**这一步的输出是整条链路的地基。** 换 tonemap 算法（reinhard/mobius）会让后续 LUT 全部偏色。

### 2 · look LUT（调色的主体）

`assets/luts/selected-color-look-v3.cube`（65³）——这是 v3 版本，由三段叠成：

1. **校正基底**：中性 WB + 轻度对比
2. **创作层**：暖调 `colorbalance` + `eq contrast 1.09 sat 1.10` + `vibrance 0.16`
3. **二级（色域级）+ 胶片层**：
   - 食物 +2% 饱和 / +3.8% 亮度（阈值 S>0.80，避开皮肤受光面）
   - 背景青蓝 −40% 饱和 / −15% 亮度（画面里唯一冷色，绝不误伤皮肤）
   - 绿色 +11% 饱和（新鲜感）
   - 红色铁架 +5.5% 饱和 / −6.5% 亮度（压住红溢）
   - 高光肩部滚降 + 高光降饱和 26%
   - 非暖色域整体降饱和 10%（暖色域豁免 → 整体克制、主体浓）
   - 肤色锁定 33.5°（把 25–42° 拉回肤色线）
   - 分离色调：高光暖 / 阴影冷

完整参数在 `references/recipes/warm-appetite.json`，可用 `scripts/build_look_lut.py` 重新烘焙
（已验证与交付版差 **0.16/255**）。

**兼容性**：`warm-appetite-33.cube` 是 33³ 版本，给只吃 33 格的软件用。

### 3 · 清晰度

```
unsharp=7:7:0.30:7:7:0
```

- 色度增强必须为 0，否则边缘泛色
- 0.40 会放大暗部噪点（实测铁板斑点明显），0.30 是平衡点
- 判断标准是**暗部脏不脏**，不是"够不够锐"

### 4 · Pro-Mist 辉光

```
format=gbrpf32le,
split[m][t];
[t]scale=iw/4:ih/4,gblur=sigma=10,scale=iw*4:ih*4,colorbalance=rm=0.10:gm=0.03:bm=-0.08[g];
[m][g]blend=all_mode=softlight:all_opacity=0.37,
format=yuv420p10le
```

**三个必须遵守的点**：

1. **必须 `format=gbrpf32le`**。在 YUV 上 blend 会把色度平面一起混合，画面变成迷幻色块；
   `c1_mode=none` 也救不了；`yuv444p10le` 颜色对但发灰。
2. **不能用 `gbrp16le` 代替代省内存**：实测画面暗 33.8/255，码率从 44 涨到 149 Mbps。
   要省内存只能限线程：`-filter_threads 2 -threads 6`（输出逐字节相同）。
3. 在 1/4 分辨率上模糊再放大——模糊是低频操作，视觉无差，时间省一半。

参数：`sigma` 8–14，`all_opacity` 0.30–0.45，暖色偏移 ±0.10。

### 5 · 暗角

```
vignette=angle=PI/12
```

PI/6 太重（13.7/255），PI/9 中等（6.5/255），**PI/12 是 3.7/255**，几乎只见氛围不见明暗。

### 6 · 编码

```
-r 60 -fps_mode cfr
-c:v libx265 -preset veryfast -crf 19 -pix_fmt yuv420p10le
-color_primaries bt709 -color_trc bt709 -colorspace bt709
-c:a copy -movflags +faststart
```

**`-r 60 -fps_mode cfr` 不能省。** 手机素材是 VFR（实测 avg 60.06），不强制恒帧会得到
`avg_frame_rate=59.7` 的文件，导入剪映显示 30 帧。

`-c:a copy` 保证音轨不经二次编码，音画绝不同步。

---

## 实测指标（4K60，12 核笔记本）

| 配置 | 速度 | 375 秒素材 |
|---|---|---|
| 仅 tonemap + LUT + 暗角 | ~4.1 fps | 约 91 分钟 |
| + 清晰度 | ~3.3 fps | 约 114 分钟 |
| + 辉光 | ~3.1 fps | 约 121 分钟 |

> 分钟数按 `22500 帧 ÷ fps` 外推（375 秒素材 @60fps），与实测 6.86 秒素材 224 秒（约 33 倍实时）一致。
> 实际会再慢 5–10%，编码收尾和 IO 不在 fps 里。

单条 6.86 秒素材实测 **224 秒**（约 33 倍实时），输出 30.9 MB / 36 Mbps。

---

## 成片校验口径

| 项 | 标准 |
|---|---|
| 时长 | 与源差 < 0.2s |
| 帧率 | `avg_frame_rate == 60/1`（严格） |
| 帧数 | == 时长 × 60（±2） |
| 分辨率 | 竖屏素材为 2160×3840 |
| 音频 | 保留原轨 |

帧数用 `-count_packets` 数，**不要读 `nb_frames`**（容器元数据，VFR 素材上不准）。

---

## 这套链路的边界

- **做不了空间级分离**：主体提亮、背景压暗、造景深需要位置信息，LUT 装不下。
  要做就得进达芬奇用窗口节点，或用 `scripts/make_mask.py` + RGB 域 `maskedmerge` 手工分区。
- **`色域级` 和 `空间级` 是两种量级**。这条链路把色域级做到了极致，但分离感的天花板在空间级。
- 题材偏暖调美食。拍人像、风光时 LUT 的暖色偏移会过重，需要另烘一版更中性的 look。
