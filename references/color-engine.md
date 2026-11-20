# 调色引擎：从编码信号到成片颜色

## 为什么不用 ffmpeg 滤镜链

`colorbalance` + `eq` 每个影调区只有三个粗旋钮，做完是"调过色"，不是"调好了"。
真正拉开质感的操作要有：线性光里做白平衡、电影感色调曲线、逐通道 lift/gamma/gain、
以中灰为轴的对比、带肤色保护的分亮度饱和、分离色调。

引擎按这个顺序算：

```
编码信号 → 解码曲线 → 线性光 → 白平衡 → LGG → 对比 → 色调曲线
        → Rec.709 编码 → 饱和(肤色保护) → 高光滚降 → 分离色调
```

**同一份代码既出预览也烘 LUT**，所以看到的就是交付的，不存在"预览好看成片不一样"。

---

## 三个命令

```bash
# 单个方案出图
python scripts/grade_engine.py preview FRAME.png --curve pq --look appetite_warm --out a.jpg

# 五个方案并排（同一帧，必须先看这个再定）
python scripts/grade_engine.py sheet FRAME.png --curve pq --out looks.jpg

# 烘成 LUT，交付/进达芬奇都用它
python scripts/grade_engine.py bake --curve pq --look appetite_warm --out look.cube --size 65
```

---

## 输入曲线（`--curve`）

这就是"覆盖全类型"的关键。`transfers.py` 里有一张曲线表：

| key | 曲线 | 典型来源 |
|---|---|---|
| `rec709` | Rec.709 | 已经是 SDR 的素材 |
| `srgb` | sRGB | 屏幕录制、网页素材 |
| `g22` | Gamma 2.2 | 普通消费机 |
| `pq` | PQ / HDR10 | 手机 HDR 视频、HDR10 |
| `hlg` | HLG | iPhone HDR、广播 HLG |
| `slog3` | Sony S-Log3 | 索尼微单 |
| `vlog` | Panasonic V-Log | 松下 |
| `logc3` | ARRI LogC3 | 阿莱 |
| `dlog` | DJI D-Log | 大疆 |
| `genlog` | 通用 Log 展开 | 不认识的平灰曲线 |

**怎么选**：`fit_transfer.py` 会把所有曲线跑一遍打分并出对照图，但**分数只能排序，不能判决**
——每格都做了自动曝光，曲线差异会被抹平一部分。

```bash
python scripts/fit_transfer.py FRAME.png --out fit/
```

真正的判断依据是：

1. **高光有没有细节**：解码过猛 → 白的地方糊成一片（看 `hi%` 那列，>3% 就要警惕）
2. **黑位有没有死**：看 `lo%`，>10% 说明压过头了
3. **肤色是否自然**：肉色发灰或发橙都不对
4. 实在判断不了 → 用 `genlog`，它对未知平灰曲线的容错最好

**重要**：`灰片` 不等于 `Log`。HDR（PQ/HLG）素材在普通播放器里同样是灰的。
不要因为"看起来灰"就断定是 Log。

---

## 五个内置 look（`--look`）

| key | 名称 | 适合 |
|---|---|---|
| `appetite_warm` | 食欲暖调 | 美食、街头、暖光夜景（默认） |
| `clean_neutral` | 干净中性 | 纪实、需要保真的场合 |
| `market_vivid` | 夜市浓烈 | 短视频、要一眼抓住人 |
| `cinematic_teal` | 电影青橙 | 叙事感、有人物的镜头 |
| `film_soft` | 胶片柔和 | 抬黑、低饱和、怀旧 |

每个 look 是一组具名参数，改哪个都能追溯：

```python
"appetite_warm": dict(
    exposure=1.00, temp=0.30, tint=0.03,
    contrast=1.10, sat=1.16, skin_protect=0.45,
    lift=(0.004,0.002,0.000), gamma=(1.00,1.00,1.02), gain=(1.04,1.00,0.96),
    warm=(-0.004,-0.001,0.008), cool=(0.010,0.002,-0.012), shoulder=1.00)
```

调参优先级：**曝光 → 白平衡 → 对比 → 饱和**。前两个错了，后面怎么调都不对。

---

## 加新 look

在 `LOOKS` 里加一项即可，参数含义：

| 参数 | 作用 | 常用范围 |
|---|---|---|
| `exposure` | 整体曝光倍率 | 0.9–1.1 |
| `temp` | 冷暖（+暖） | −0.4–0.5 |
| `tint` | 洋红/绿（+洋红） | −0.2–0.2 |
| `contrast` | 以 0.18 为轴的对比 | 0.9–1.3 |
| `sat` | 饱和度 | 0.9–1.35 |
| `skin_protect` | 肤色保护强度 | 0.3–0.6 |
| `lift/gamma/gain` | 逐通道影调/中间调/高光 | 小幅度 |
| `warm/cool` | 分离色调（高光/阴影） | ±0.015 |
| `shoulder` | 白点位置，<1 更早滚降 | 0.85–1.0 |

---

## 已知边界

- **自动曝光**按中位亮度对齐到 0.15。极暗或极亮的场景会偏，用 `--no-auto-exposure` 手动定。
- **LUT 精度**：65³ 对平滑曲线足够；如果成片出现色阶断层，加到 129³。
- **不含空间处理**：清晰度、柔光辉光、暗角需要位置信息，烘不进 LUT，
  见 [spatial-layer.md](spatial-layer.md)。
- **验证方法**：LUT 输出与直算结果比对，`mean|d|` 应 < 2/255。
