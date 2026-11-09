# orz 2.0

Jellyfin 剧集自动改名：监控 NAS 上的 TV series / Anime 目录，新建剧集文件夹时查 TMDB 写 `tvshow.nfo`、建季文件夹；视频和字幕移入季文件夹后自动按规则改名。Python 3.12 重写（原版是 PHP），只用标准库。

## 平时不用敲命令

容器跑起来就自动监控，不需要手动执行任何命令：

```shell
docker compose up -d
```

它在后台每 30 秒轮询 `/media/TV show` 和 `/media/Anime`：

1. 新建文件夹 `绝望主妇 (2004)`、`罗梅莉亚战记 (2026)` → 查 TMDB，写 `tvshow.nfo`、建 `Season 01` / `Specials`、并把集列表缓存到 `.orz/episodes.json`。
2. 视频 / 字幕文件移入季文件夹 → 等文件大小稳定几秒（复制未完成不改名），按 `剧名 - S01E01 - 集名.mkv` 改名，字幕保留 `.zh.srt` / `.en.ass` 语言后缀、跟同名视频一起改。
3. 改名**脱机完成**：只读本地 `tvshow.nfo` + `.orz/episodes.json`，每次改名**不调 TMDB**。TMDB 只在新建剧集（`create`）或刷新（`refresh`）时调一次，没网也能改名。

启动时先全量扫一遍补漏：容器停机期间移入的文件，只要大小已稳定，第一次扫描就处理。

## 命令行只是测试和排错用

```shell
docker exec orz python3 orz.py create "/media/anime/罗梅莉亚战记 (2026)"
docker exec orz python3 orz.py refresh "/media/anime/罗梅莉亚战记 (2026)"  # 出了新季 / 集名变了 / 缓存丢了
docker exec orz python3 orz.py folder "/media/anime/罗梅莉亚战记 (2026)/Season 01"  # 手动改一季（脱机）
docker exec orz python3 orz.py dry-run "/media/anime/罗梅莉亚战记 (2026)/Season 01"  # 只看会怎么改，不动文件
docker exec orz python3 orz.py scan     # 手动跑一轮轮询
```

`folder` / `dry-run` 不需要 `TMDB_API_KEY`；`create` / `refresh` / `scan` / `watch` 需要。

## 日志和 pending

每个文件的处理结果都写日志（`docker logs orz`）：

- `Renamed A -> B`：改了什么。
- `Pending <文件>: <原因>`：为什么没改。常见原因：
  - `file still copying/moving (size not stable yet)` —— 还在复制，下轮再试；
  - `episode 13 has no TMDB match` —— 集数超出已知范围；
  - `episode 2 already exists (collision, not overwriting)` —— 目标集已存在，绝不覆盖；
  - `no number found` —— 文件名里找不到可信集数，且无法补位。
- `No episode cache ... run orz refresh ...` —— 旧版 PHP 留下的剧只有 nfo、没有集缓存，跑一次 `refresh` 即可，之后永久脱机。

pending 列表也存在 `.orz/state.json` 里（见下）。

## 三个老痛点怎么修的

**1. 单文件集数难分离，不再 fallback 到 01。** 先去掉发布组方括号、1080p/720p/2160p/4K、x264/x265/HEVC、年份、季标记，再按优先级找集数：`SxxEyy` > `Eyy` > `第x集` > 单个裸数字。剧名前缀用文件夹名先剥掉，所以 `24`、`86` 这种带数字的剧名不会被误认。找不到可信数字时**不猜、不写 01**，记为 pending。

**2. TMDB 只有 1 季，文件却写 S2/S02 从头编号。** 按绝对集数映射：TMDB S1 有 12 集，文件 `S2E01` → `S01E13`、`S2E02` → `S01E14`，不建 Season 02。绝对集数超出 TMDB 已列出的范围时，仍落在最后一季、集名留空（`剧名 - S01E13.mkv`）。注意：偏移量取自 TMDB 的集数——如果 TMDB S1 列了 24 集而发布组在 12 集处分季，`S2E01` 会算成 `S01E25`；这种情况在剧文件夹放一个 `orz.json` 钉住偏移（只读，程序永不改写它）：

```json
{"season_map": {"2": 1}, "episode_offset": {"2": 12}}
```

还有一种常见情况：TMDB 明明有 S2，但 Season 01 里已经按绝对编号放了 S2 的前几集（青之箱：`S01E26-29` 其实是 S2E01-04）。这时程序认出 Season 01 已有超出 S1 集数的集，自动按绝对编号继续：`S2E05` → `S01E30`，集名仍取 S2E05 的标题（`戻ってきたよ`）。`Season 2 - 05` 这种写法也认得出：`Season 2` 是季、`05` 是集，不会把 2 和 05 当成两个候选集数。

**3. 追剧时往已有 5 集的季文件夹放单集。** 先读已改好名的文件，算出已有集数和缺口：没数字的文件先补**最小缺口**（有 1、2、3、5 → 补 4），没缺口则取 max+1（有 1–5 → 6）。映射结果存在 `.orz/state.json`（文件指纹 → 集数），重扫不再重新猜。

## 文件布局

```
/media/anime/罗梅莉亚战记 (2026)/
├── tvshow.nfo                 # TMDB 信息
├── orz.json                    # 可选，用户手写覆盖（程序只读）
├── .orz/
│   ├── episodes.json          # 集列表缓存（create/refresh 时写）
│   └── state.json             # 文件指纹→集数 + pending 列表
├── Season 01/
│   └── 罗梅莉亚战记 - S01E01 - 第1集.mkv
└── Specials/
```

`.orz/` 是隐藏目录，Jellyfin 不会把它当媒体扫进去。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `TMDB_API_KEY` | — | 必填，[TMDB](https://www.themoviedb.org/documentation/api) 申请 |
| `TMDB_LANGUAGE` | `zh-CN` | 集名语言 |
| `ORZ_WATCH_DIRS` | `/media/anime,/media/tv-series` | 监控目录，逗号分隔；带空格的路径不用转义，整个值加引号，如 `"/media/TV show,/media/Anime"`。程序内部用 `os.path` 处理路径、不拼 shell 命令，所以空格不会断开路径 |
| `ORZ_SCAN_INTERVAL` | `30` | 轮询间隔（秒） |
| `ORZ_STABLE_SECONDS` | `5` | 文件大小不变几秒后才改名 |

## 本地开发

```shell
python3 -m unittest discover -s tests -v   # 测试，不联网
python3 orz.py watch                        # 本地跑守护进程
```

## 从 PHP 旧版迁移

旧版只写了 `tvshow.nfo`、没有 `.orz/episodes.json`，改名时会报缺缓存。对每个旧剧跑一次 `refresh`（或等新剧建文件夹时自动 `create`），之后就脱机了。另修了一个旧 bug：`getExactMatch` 原来拿 `results[0]` 的 `first_air_date` 去校验所有结果的年份，现在每个结果用自己的首播年份校验。
