# Assets

## bgm/

4 段程序化合成的背景音乐 (ffmpeg sine + noise), 首次运行时自动生成,缓存到此目录:

- `lofi_chill_60bpm.mp3` - Lo-Fi 60 BPM 温暖钢琴
- `ambient_pad_70bpm.mp3` - Ambient 70 BPM drone + 慢琶音
- `soft_electronic_90bpm.mp3` - Soft electronic 90 BPM 轻快 synth
- `coffee_shop_jazz.mp3` - Coffee shop jazz 慵懒钢琴

每段 5 分钟 (300s), 32kHz mono mp3, 程序化生成无版权依赖。
脚本按日期 hash 选 BGM,每天自动循环使用。

## 为什么不进 git?

每个 mp3 ~3.6 MB, 4 段共 ~15 MB, 跟 git LFS 没必要。  .gitignore 排除 *.mp3。

如果新部署,首次运行 `daily_news_podcast.py` 时自动生成。
