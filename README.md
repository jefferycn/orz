# orz 2.7

Jellyfin 剧集自动改名：监控 NAS 上的 TV series / Anime 目录，新建剧集文件夹时查 TMDB 写 `tvshow.nfo`、建季文件夹；视频和字幕移入季文件夹后自动按规则改名。Python 3.12 重写（原版是 PHP），只用标准库。

## 平时不用敲命令

容器跑起来就自动监控，不需要手动执行任何命令：

```shell
cp .env.example .env   # 填好 TMDB_API_KEY
docker compose up -d --build
```

它在后台用 inotify 监控 `/media/TV show` 和 `/media/Anime`（事件驱动，不轮询）：文件或文件夹一有改动才处理，没改动时不读目录。inotify 是直接调 libc（标准库 ctypes），不用装 `inotify-tools`，也不用新依赖。

1. 新建文件夹 `绝望主妇 (2004)`、`罗梅莉亚战记 (2026)` → 查 TMDB，写 `tvshow.nfo`、建季文件夹（标准写法是补零的 `Season 01`；已有的 `Season 1` 会在下次扫描时改成 `Season 01`，如果 `Season 01` 已存在就把文件并进去，绝不覆盖重名文件）、并把集列表缓存到 `.orz/episodes.json`。
2. 视频 / 字幕文件移入季文件夹 → 等文件大小稳定几秒（复制未完成不改名），按 `剧名 - S01E01 - 集名.mkv` 改名，字幕保留 `.zh.srt` / `.en.ass` / `.sc_v2.ass` / `.tc_v2.ass` 等语言后缀（sc 简体、tc 繁体，`_v2` 是版本号），跟同名视频一起改。

### 剧名用中文还是英文？

文件夹名是 TMDB 的搜索词，不是改名用的剧名。改名用的剧名来自 TMDB 返回的 `name`，语言由 `TMDB_LANGUAGE`（默认 `zh-CN`）决定：

- 文件夹写 `青春猪头少年不会梦到兔女郎学姐 (2018)`：TMDB 有这个中文译名，搜得到，`tvshow.nfo` 里的 title 就是中文，改名后也是中文。
- 文件夹写英文 `Giji Harem (2024)`：只要 TMDB 有 zh-CN 译名（疑似后宫），`tvshow.nfo` 和改名结果同样是中文。文件夹用英文更稳：中文译名如果跟 TMDB 的 zh-CN 译名不一致（比如你写“伪恋”，TMDB 写“疑似后宫”），搜不到就建不出 NFO。
- TMDB 没有某部剧的 zh-CN 译名时，`name` 会回落到原名 / 英文，改名结果就是原名 / 英文，不是文件夹名。
3. 改名**脱机完成**：只读本地 `tvshow.nfo` + `.orz/episodes.json`，每次改名**不调 TMDB**。TMDB 只在新建剧集（`create`）、刷新（`refresh`）时调一次，没网也能改名。旧版 PHP 留下的剧只有 nfo、没有集缓存：第一次有文件放进它的季文件夹时，程序会去 TMDB 取一次缓存、同一轮就把文件改了，不用手动 `refresh`；取不到（文件夹名跟 TMDB 对不上）就记 warning、文件不动，10 分钟内不再重试。

没有轮询，没有补扫：启动时只加 watch，不扫描。文件放进季文件夹会触发 inotify 事件，5 秒稳定后改名；容器停机期间移入的文件不会被处理——直到有新事件碰到它那个文件夹，或者你手动跑一次 `orz scan` 补上（`scan` 是唯一的扫描，只在你敲命令时跑）。每个季文件夹的快照（修改时间 + 文件列表）存在本剧 `.orz/snapshots.json` 里：处理完一个文件夹就记一笔，orz 自己改名产生的事件再来时，快照没变就直接跳过，不会重复处理。inotify 队列溢出时也只记一条 warning（可能丢了几个事件，跑一次 `orz scan` 补上），不自动扫。

已经是干净的 `剧名 - SxxEyy - 集名` 形式的文件一律不动：`Dragon Ball - S02E15 - ...` 放在 `Season 2` 里、orz 自己改过的 `绝望主妇 - S04E01 - ...`，都算已经整理好。只带集数标记但不是这个形式的，仍要改名：`Attack on Titan S01E01.mkv`、`A Sister's All You Need. E01 ...`、以及发布组文件名 `Desperate.Housewives.S04E01.1080p.WEB-DL...ass`（点号分隔、带编码和发布组标签的是下载名，不是资料库名），都会改成中文格式。文件名里没有明确集数标记的文件也是要改的（比如 `[Group] Show - 05.mkv`）。季标记和文件夹季号不一致的文件仍会处理（比如 `Season 2 - 05` 放在 `Season 01` 里）。集号只属于视频：字幕不占集号，后到的字幕即使视频已经改好名了也会跟着改；同集的两个视频才算冲突。

`tvshow.nfo` 里的 TMDB 编号，orz 认三种写法：`<tmdbid>`（orz 和旧版 PHP 写的）、`<tmdb>` 和 `<uniqueid type="tmdb">`（Jellyfin/Kodi 写的）。如果 nfo 里完全没有 TMDB 编号（比如只有 TVDB 的 `<id>`），文件就算存在也读不出来：日志会报一条 warning（每小时每部剧只报一次，不会每个季文件夹、每轮都刷），说清原因并让你对这部剧跑一次 `orz refresh` 重建 nfo——不会擅自覆盖你原来的 nfo。

## 命令行只是测试和排错用

```shell
docker exec orz python3 orz.py create "/media/anime/罗梅莉亚战记 (2026)"
docker exec orz python3 orz.py refresh "/media/anime/罗梅莉亚战记 (2026)"  # 出了新季 / 集名变了 / 缓存丢了
docker exec orz python3 orz.py folder "/media/anime/罗梅莉亚战记 (2026)/Season 01"  # 手动改一季（脱机）
docker exec orz python3 orz.py dry-run "/media/anime/罗梅莉亚战记 (2026)/Season 01"  # 只看会怎么改，不动文件
docker exec orz python3 orz.py scan     # 手动跑一轮：补上容器停机期间移入的文件
```

`folder` / `dry-run` 不需要 `TMDB_API_KEY`；`create` / `refresh` / `scan` / `watch` 需要。

## 日志和 pending

每个文件的处理结果都写日志（`docker logs orz`）：

- `Renamed A -> B`：改了什么。
- `Pending <文件>: <原因>`：为什么没改。常见原因：
  - `file still copying/moving (size not stable yet)` —— 还在复制，下轮再试；
  - `episode 13 has no TMDB match` —— 集数超出已知范围；
  - `episode 2 already exists (collision, not overwriting)` —— 目标集已存在，绝不覆盖；
  - `looks like a special/trailer/extra, not an episode — left alone` —— 特典 / 预告 / 花絮不是正片，不补位、不改名；
  - `no number found` —— 文件名里找不到可信集数，且无法补位。
- `No episode cache ...` —— 旧版 PHP 留下的剧只有 nfo、没有集缓存，`watch` / `scan` 会在第一次有文件进来时自动去 TMDB 取一次（懒刷新）；`folder` / `dry-run` 是脱机的，没有 TMDB 客户端，才会提示手动跑一次 `refresh`。

pending 列表也存在 `.orz/state.json` 里（见下）。

## 三个老痛点怎么修的

**1. 单文件集数难分离，不再 fallback 到 01。** 先去掉发布组方括号、1080p/720p/2160p/4K、x264/x265/HEVC、年份、季标记，再按优先级找集数：`SxxEyy` > `Eyy` > `第x集` > 单个裸数字。剧名前缀用文件夹名先剥掉，所以 `24`、`86` 这种带数字的剧名不会被误认。找不到可信数字时**不猜、不写 01**，记为 pending。例外：集数本身写在方括号里（`[DBD-Raws][Medalist][01]…`、`…[BDRip][01](A85E5FC2).cht.ass`）时，`[01]` 在剥方括号前先提取，`(A85E5FC2)` 这种 8 位 CRC 校验码直接去掉。

**多文件一起放进来时，用整批共有的文字再剥一层。** 同一轮扫描里 ≥2 个未改名文件时，先算 token 级公共前后缀（按空格 / `-` / `_` / `.` / 方括号分 token，不按字符——`01` / `02` 公共字符是 `0`，按字符剥会切坏集数），剥掉发布组、剧名、编码、年份等共有部分，剩下的每文件基本只剩集数。季如果在公共前缀里（`Season 2 - 05`），剥之前先把季读出来。显式 `SxxEyy` / `Eyy` / `第x集` 优先于 batch 猜测；单文件时没有“公共”可言，回落到逐文件解析（比如 `Show Vol.3 - 02` 单独进来，`Vol.3` 的 `3` 没法知道是共有的，仍会 gap-fill 到 1）。

**2. TMDB 只有 1 季，文件却写 S2/S02 从头编号。** 按绝对集数映射：TMDB S1 有 12 集，文件 `S2E01` → `S01E13`、`S2E02` → `S01E14`，不建 Season 02。绝对集数超出 TMDB 已列出的范围时，仍落在最后一季、集名留空（`剧名 - S01E13.mkv`）。注意：偏移量取自 TMDB 的集数——如果 TMDB S1 列了 24 集而发布组在 12 集处分季，`S2E01` 会算成 `S01E25`；这种情况在剧文件夹放一个 `orz.json` 钉住偏移（只读，程序永不改写它）：

```json
{"season_map": {"2": 1}, "episode_offset": {"2": 12}}
```

还有一种常见情况：TMDB 明明有 S2，但 Season 01 里已经按绝对编号放了 S2 的前几集（青之箱：`S01E26-29` 其实是 S2E01-04）。这时程序认出 Season 01 已有超出 S1 集数的集，自动按绝对编号继续：`S2E05` → `S01E30`，集名仍取 S2E05 的标题（`戻ってきたよ`）。`Season 2 - 05` 这种写法也认得出：`Season 2` 是季、`05` 是集，不会把 2 和 05 当成两个候选集数。

TMDB 有时会把两季并在一季里列（青之箱：TMDB S1 列了 37 集，其实是 25 集 S1 + 12 集 S2；龙珠：153 集全在 TMDB S1 里）。这时偏移量不能直接用 TMDB 的季长度：程序把每集的播出日期也存进 `.orz/episodes.json`，同一季里播出日期断档超过 150 天就切一刀（两季之间通常隔半年以上，两 part 之间只隔两三个月），切出来的段才是真正的季——青之箱切成 25 + 12，`S2E05` 就是 25 + 5 = `S01E30`，不会再算成 42。

**3. 追剧时往已有 5 集的季文件夹放单集。** 先读已改好名的文件，算出已有集数和缺口：没数字的文件先补**最小缺口**（有 1、2、3、5 → 补 4），没缺口则取 max+1（有 1–5 → 6）。映射结果存在 `.orz/state.json`（文件指纹 → 集数），重扫不再重新猜。文件指纹（文件头 4 MiB 的哈希）是懒算的：只有查旧映射、或补位猜集数时才读文件，文件名能解析出集数的文件完全不读盘——全库启动扫描不再因为指纹读出几个 G 的 page cache。**补位只给正片文件**：名字带 `Sp` / `Special` / `Extras` / `PV` / `预告` / `特典` / `花絮` / `前瞻` / `Trailer` 的（特典、预告片、花絮）永远不补位、不改名，记为 pending——`Sp01` 不是第 13 集，`30分钟前瞻PV` 也不是第 5 话。

## 文件布局

```
/media/anime/罗梅莉亚战记 (2026)/
├── tvshow.nfo                 # TMDB 信息
├── orz.json                    # 可选，用户手写覆盖（程序只读）
├── .orz/
│   ├── episodes.json          # 集列表缓存（create/refresh 时写，含播出日期）
│   ├── snapshots.json         # 各季文件夹快照（启动补扫只处理改动过的目录）
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
| `NAS_TV_DIR` | `/volume1/TV show` | NAS 本地电视剧目录（compose 挂载的冒号左边）；`.env` 里没有时用默认值 |
| `NAS_ANIME_DIR` | `/volume1/Anime` | NAS 本地动画目录，同上 |
| `ORZ_WATCH_DIRS` | `/media/anime,/media/tv-series` | 监控目录，逗号分隔；带空格的路径不用转义，整个值加引号，如 `"/media/TV show,/media/Anime"`。程序内部用 `os.path` 处理路径、不拼 shell 命令，所以空格不会断开路径 |
| `ORZ_STABLE_SECONDS` | `5` | 文件大小不变几秒后才改名 |

## 出了问题怎么恢复

如果某次运行改错了文件（比如 v2.6 第一次启动的 86 个文件），`tools/restore_from_log.py` 能按日志里的 `Renamed` 记录把文件移回原名。先停掉容器（否则它会立刻再改一遍），再：

```shell
docker compose run --rm --build orz python3 tools/restore_from_log.py --dry-run
docker compose run --rm --build orz python3 tools/restore_from_log.py
```

（docker compose 是 v1（`docker-compose`，连字符）时，`run` 没有 `--build` 参数，先单独跑一次 `docker-compose build`，再 `docker-compose run --rm orz python3 tools/restore_from_log.py`；`up -d --build` 在 v1 里是有的。）

只恢复表里那 86 条记录：旧路径已存在、找不到、找到多个时都跳过，绝不覆盖；还会删掉受影响剧里过期的 `.orz/state.json`，以及空的重名季文件夹（比如 refresh 建出来的空 `Season 01`，而剧本身用 `Season 1`）。

不是剧的文件夹不会被当成剧：Jellyfin 的图片（`landscape.jpg` / `backdrop.jpg` / `logo.png` / `*-thumb.jpg`）、字幕、nfo 文件，以及群晖的 `@eaDir`（里面甚至有以文件名命名的子文件夹，比如 `@eaDir/landscape.jpg/`），都不会触发 TMDB 查询、也不会记 warning；`@eaDir` 根本不监听。季文件夹除了 `Season 01` / `Specials`，`S02` / `S03` 这种短写法也认。`Season 1` 合并进 `Season 01` 时，两边都有的 `season.nfo` 如果是同一季的记录（字节相同，或 `<seasonnumber>` 相同），旧那份会删掉，只留标准文件夹里的那份；季号对不上或解析不了的，旧文件夹原样保留。

## 本地开发

```shell
python3 -m unittest discover -s tests -v   # 测试，不联网
python3 orz.py watch                        # 本地跑守护进程
```

## 从 PHP 旧版迁移

旧版只写了 `tvshow.nfo`、没有 `.orz/episodes.json`：第一次有文件放进它的季文件夹时，程序自动去 TMDB 取一次缓存、同一轮就把文件改了，不用手动 `refresh`，也不用批量刷。空的季文件夹不会触发取缓存，所以启动补扫不会把所有旧剧都刷一遍。另修了一个旧 bug：`getExactMatch` 原来拿 `results[0]` 的 `first_air_date` 去校验所有结果的年份，现在每个结果用自己的首播年份校验。
