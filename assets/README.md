# Assets

## bgm/

10 段程序化合成的背景音乐 (ffmpeg sine + noise + filter 链), **已纳入 git 备份**。

| 文件 | 风格 | mean 音量 |
|---|---|---|
| `lofi_chill_60bpm.mp3` | Lo-Fi 60 BPM 温暖钢琴 | -14.5 dB |
| `lofi_study_70bpm.mp3` | Lo-Fi 70 BPM 慢节奏钢琴琶音 | -12.4 dB |
| `lofi_sunset_80bpm.mp3` | Lo-Fi 80 BPM 慵懒吉他感 + 雨声 | -17.7 dB |
| `ambient_pad_70bpm.mp3` | Ambient 70 BPM drone + 慢琶音 | -15.7 dB |
| `ambient_drone_50bpm.mp3` | Ambient 50 BPM 极慢 drone | -7.9 dB |
| `ambient_space_60bpm.mp3` | Ambient 60 BPM 太空感 + 钟声 | -10.9 dB |
| `coffee_shop_jazz.mp3` | Jazz 慵懒钢琴 | -16.4 dB |
| `smooth_jazz_90bpm.mp3` | Smooth jazz 复古萨克斯感 | -17.6 dB |
| `soft_electronic_90bpm.mp3` | Electronic 90 BPM 轻快 synth | -9.8 dB |
| `synthwave_100bpm.mp3` | Synthwave 100 BPM 复古电子 | -8.5 dB |

### 技术细节

- 每段 300 秒 (5 分钟), 32kHz mono mp3, 128kbps
- **音量已 amplify 18dB + limiter** (程序化合成本来 ~-30dB 太安静, 混音听不到)
- 生成脚本: `scripts/daily_news_podcast.py` 的 `BGM_PRESETS` + `ensure_bgm()`
- 混音: `mix_with_bgm()` 把 BGM 降到 -3dB 后与人声 amix
- 每日按日期 hash 自动选 BGM, 每天风格一致
- 无版权依赖 (纯程序化生成), 可商用

### 重新生成

```bash
rm -rf assets/bgm && python scripts/daily_news_podcast.py  # 首次运行自动生成
```
