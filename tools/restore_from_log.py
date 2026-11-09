#!/usr/bin/env python3
"""把 orz v2.6 启动扫描错改的 86 个文件改回原名。

背景：v2.6 部署后第一次启动，全量补扫 + 懒刷新把一批本来已经
按英文名整理好的文件重新改名了（龙珠 Season 2 -> S01E154-168、
Season 3 -> S01E169-193、青之箱 S2E05 -> S01E42、灵笼预告片 ->
S00E05，等等）。这 86 条映射就是那次运行的完整日志
（2026-11-09 05:01-05:16 UTC）里 `Renamed <旧全路径> -> <新文件名>`
的全部记录：87 条里灵笼预告片被改了两次，第二次的旧名就是
第一次的新名，两条已合并成一条（原名 -> 最终名）。原样嵌在
这里，不依赖日志文件本身。

用法（先停掉 orz，否则它会立刻把移回去的文件再改一遍）：
    docker stop orz            # 或在 Container Manager 里停掉
    docker compose run --rm --build orz \
        python3 tools/restore_from_log.py --dry-run
    # 看一遍计划，没问题再去掉 --dry-run 真正执行
    docker compose run --rm --build orz \
        python3 tools/restore_from_log.py

它做的事：
  1. 对每一条映射，在剧文件夹下按新文件名找到文件（必须恰好
     找到一个），移回旧的全路径；旧路径已存在、找不到、找到多
     个时都跳过，绝不覆盖。
  2. 删掉受影响 6 部剧的 `.orz/state.json`（里面记着错的
     指纹->集数映射，是可再生的状态，不是用户数据）。
  3. 对 /media/Anime 与 /media/TV show 下所有剧：如果一个
     `Season 0N` 文件夹是空的、且同剧有 `Season N` 文件夹
     （refresh 建出来的重名季文件夹），删掉空的那个。
执行后青之箱那个文件会回到原名；再启动 v2.7 的 orz，它会按
正确的绝对编号把它改成 S01E30（而不是 S01E42）。
"""
from __future__ import annotations

import argparse
import os
import sys

# (旧全路径, 新文件名) —— 2026-11-09 05:01-05:16 UTC 那次运行的全部记录
# （87 条，灵笼预告片二改已合并为一条，共 86 条）。
RESTORE = [
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E01 [I Only Need a Little Brother Who Can Cook, a Beautiful Naked Girl, and Friends I Can Relate to.].mp4", '如果有妹妹就好了 - S01E01 - 如果有擅长料理的弟弟和全裸的美少女和意气相投的朋友就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E02 [If Only a Miracle Would Happen.].mp4", '如果有妹妹就好了 - S01E02 - 如果能发生奇迹就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E03 [As Long As It's Research.].mp4", '如果有妹妹就好了 - S01E03 - 如果能取材就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E04 [As Long As You're Working.].mp4", '如果有妹妹就好了 - S01E04 - 如果能工作就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E05 [All You Need is to Write Novels.].mp4", '如果有妹妹就好了 - S01E05 - 如果能写小说就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E06 [Having Your Media Developments Go Well is All You Need.].mp4", '如果有妹妹就好了 - S01E06 - 如果多媒体展开能顺利就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E07 [Adventure is All You Need].mp4", '如果有妹妹就好了 - S01E07 - 如果有冒险就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E08 [All You Need is Love and Friendship.].mp4", '如果有妹妹就好了 - S01E08 - 如果有恋爱与友情就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E09 [Nudity and Underwear Are All You Need].mp4", '如果有妹妹就好了 - S01E09 - 如果有全裸和内衣就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E10 [A Lack of Problems is All You Need.].mp4", '如果有妹妹就好了 - S01E10 - 如果没有烦恼就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E11 [Becoming the Protagonist is All You Need.].mp4", '如果有妹妹就好了 - S01E11 - 如果能成为主角就好了。.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E11 [Becoming the Protagonist is All You Need.].zh.ass", '如果有妹妹就好了 - S01E11 - 如果能成为主角就好了。.zh.ass'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E12 [A Sister is All You Need].mp4", '如果有妹妹就好了 - S01E12 - 如果有妹妹就好了？.mp4'),
    ("/media/Anime/如果有妹妹就好了 (2017)/Season 1/A Sister's All You Need. E12 [A Sister is All You Need].zh.ass", '如果有妹妹就好了 - S01E12 - 如果有妹妹就好了？.zh.ass'),
    ('/media/Anime/如果有妹妹就好了 (2017)/Season 1/Imouto Sae Ireba II Sp01.mp4', '如果有妹妹就好了 - S01E13.mp4'),
    ('/media/Anime/如果有妹妹就好了 (2017)/Season 1/Imouto Sae Ireba II Sp02.mp4', '如果有妹妹就好了 - S01E14.mp4'),
    ('/media/Anime/如果有妹妹就好了 (2017)/Season 1/Imouto Sae Ireba II Sp03.mp4', '如果有妹妹就好了 - S01E15.mp4'),
    ('/media/Anime/如果有妹妹就好了 (2017)/Season 1/Imouto Sae Ireba II Sp04.mp4', '如果有妹妹就好了 - S01E16.mp4'),
    ('/media/Anime/如果有妹妹就好了 (2017)/Season 1/Imouto Sae Ireba II Sp05.mp4', '如果有妹妹就好了 - S01E17.mp4'),
    ('/media/Anime/如果有妹妹就好了 (2017)/Season 1/Imouto Sae Ireba II Sp06.mp4', '如果有妹妹就好了 - S01E18.mp4'),
    ("/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E01 - We're Searching WEBRip-1080p.mkv", '我心里危险的东西 - S01E26.mkv'),
    ("/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E02 - I'm Growing Up WEBRip-1080p.mkv", '我心里危险的东西 - S01E27.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E03 - Yamada and Me WEBRip-1080p.mkv', '我心里危险的东西 - S01E28.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E04 - Yamada Likes… WEBRip-1080p.mkv', '我心里危险的东西 - S01E29.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E05 - I Want to Know WEBRip-1080p.mkv', '我心里危险的东西 - S01E30.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E06 - Yamada Likes Me WEBRip-1080p.mkv', '我心里危险的东西 - S01E31.mkv'),
    ("/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E07 - We're Overflowing WEBRip-1080p.mkv", '我心里危险的东西 - S01E32.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E08 - We Stayed Up All Night WEBRip-1080p.mkv', '我心里危险的东西 - S01E33.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E09 - We Made a Promise WEBRip-1080p.mkv', '我心里危险的东西 - S01E34.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E10 - I Want to Be Closer to Yamada WEBRip-1080p.mkv', '我心里危险的东西 - S01E35.mkv'),
    ("/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E11 - I Don't Want to Lose WEBRip-1080p.mkv", '我心里危险的东西 - S01E36.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E12 - I Want to Tell Her WEBRip-1080p.mkv', '我心里危险的东西 - S01E37.mkv'),
    ('/media/Anime/我心里危险的东西 (2023)/Season 2/The Dangers in My Heart - S02E13 - I, We, Fell in Love WEBRip-1080p.mkv', '我心里危险的东西 - S01E38.mkv'),
    ('/media/Anime/灵笼 (2019)/Specials/30分钟前瞻PV 4K.mp4', '灵笼 - S00E05 - 第5话 生态噩兆.mp4'),
    ('/media/Anime/青之箱 (2024)/Season 01/[Tsukigakirei] Ao no Hako Season 2 - 05 [WebRip 1080P AVC-8bit AAC][CHS&JPN].mp4', '青之箱 - S01E42.mp4'),
    ("/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E01 - Goku's Rival HDTV-720p.mkv", '龙珠 - S01E154.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E02 - Look Out for Launch! HDTV-720p.mkv', '龙珠 - S01E155.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E03 - Find That Stone HDTV-720p.mkv', '龙珠 - S01E156.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E04 - Milk Delivery HDTV-720p.mkv', '龙珠 - S01E157.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E04 - Milk Delivery HDTV-720p.zh.ssa', '龙珠 - S01E157.zh.ssa'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E05 - The Turtle Hermit Way HDTV-720p.mkv', '龙珠 - S01E158.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E06 - The Tournament Begins HDTV-720p.mkv', '龙珠 - S01E159.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E07 - Elimination Round HDTV-720p.mkv', '龙珠 - S01E160.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E07 - Elimination Round HDTV-720p.zh.ssa', '龙珠 - S01E160.zh.ssa'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E08 - Smells Like Trouble HDTV-720p.mkv', '龙珠 - S01E161.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E09 - Quarter Finals Begin HDTV-720p.mkv', '龙珠 - S01E162.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E10 - Monster Beast Giran HDTV-720p.mkv', '龙珠 - S01E163.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E10 - Monster Beast Giran HDTV-720p.zh.ssa', '龙珠 - S01E163.zh.ssa'),
    ("/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E11 - Krillin's Frantic Attack! HDTV-720p.mkv", '龙珠 - S01E164.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E12 - Danger From Above HDTV-720p.mkv', '龙珠 - S01E165.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E13 - The Grand Finals HDTV-720p.mkv', '龙珠 - S01E166.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E14 - Number One Under The Moon HDTV-720p.mkv', '龙珠 - S01E167.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 2/Dragon Ball - S02E15 - The Final Blow HDTV-720p.mkv', '龙珠 - S01E168.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E16 - Master Thief, Haski HDTV-720p.mkv', '龙珠 - S01E169.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E16 - Master Thief, Haski HDTV-720p.zh.ssa', '龙珠 - S01E169.zh.ssa'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E17 - Danger in the Air HDTV-720p.mkv', '龙珠 - S01E170.mkv'),
    ("/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E18 - Bulma's Bad Day HDTV-720p.mkv", '龙珠 - S01E171.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E19 - Kame House - Found! HDTV-720p.mkv', '龙珠 - S01E172.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E20 - Deep Blue Sea HDTV-720p.mkv', '龙珠 - S01E173.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E21 - Roshi Surprise HDTV-720p.mkv', '龙珠 - S01E174.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E22 - The Trap is Sprung HDTV-720p.mkv', '龙珠 - S01E175.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E23 - Beware of Robot HDTV-720p.mkv', '龙珠 - S01E176.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E24 - The Pirate Treasure HDTV-720p.mkv', '龙珠 - S01E177.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E24 - The Pirate Treasure HDTV-720p.zh.ssa', '龙珠 - S01E177.zh.ssa'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E25 - Blue, Black and Blue HDTV-720p.mkv', '龙珠 - S01E178.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E26 - Escape From Pirate Cave HDTV-720p.mkv', '龙珠 - S01E179.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E26 - Escape From Pirate Cave HDTV-720p.zh.ass', '龙珠 - S01E179.zh.ass'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E27 - Penguin Village HDTV-720p.mkv', '龙珠 - S01E180.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E28 - Strange Visitor HDTV-720p.mkv', '龙珠 - S01E181.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E29 - Arale vs. Blue HDTV-720p.mkv', '龙珠 - S01E182.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E29 - Arale vs. Blue HDTV-720p.zh.ssa', '龙珠 - S01E182.zh.ssa'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E30 - The Land of Korin HDTV-720p.mkv', '龙珠 - S01E183.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E31 - The Notorious Mercenary HDTV-720p.mkv', '龙珠 - S01E184.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E32 - Tao Attacks! HDTV-720p.mkv', '龙珠 - S01E185.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E33 - Korin Tower HDTV-720p.mkv', '龙珠 - S01E186.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E34 - Sacred Water HDTV-720p.mkv', '龙珠 - S01E187.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E35 - The Return of Goku HDTV-720p.mkv', '龙珠 - S01E188.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E36 - The Last of Mercenary Tao HDTV-720p.mkv', '龙珠 - S01E189.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E37 - Confront the Red Ribbon Army HDTV-720p.mkv', '龙珠 - S01E190.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E37 - Confront the Red Ribbon Army HDTV-720p.zh.ssa', '龙珠 - S01E190.zh.ssa'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E38 - A Real Bind HDTV-720p.mkv', '龙珠 - S01E191.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E38 - A Real Bind HDTV-720p.zh.ssa', '龙珠 - S01E191.zh.ssa'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E39 - The End of Commander Red HDTV-720p.mkv', '龙珠 - S01E192.mkv'),
    ('/media/Anime/龙珠 (1986)/Season 3/Dragon Ball - S03E40 - The Last Dragon Ball HDTV-720p.mkv', '龙珠 - S01E193.mkv'),
    ('/media/TV show/Sherlock/Season 02/Sherlock.Season.Two.Extras.2011.Bluray.1080p.DD.5.1.x264-BluHD.chn.srt', '神探夏洛克 - S03E01 - 空灵柩.srt'),
    ('/media/TV show/Sherlock/Season 02/Sherlock.Season.Two.Extras.2011.Bluray.1080p.DD.5.1.x264-BluHD.mkv', '神探夏洛克 - S03E01 - 空灵柩.mkv'),
]

# 6 部受影响的剧（由上表推导），只用于删过期的 state.json。
AFFECTED_SHOWS = sorted({os.path.dirname(os.path.dirname(o)) for o, _ in RESTORE})

ROOTS = ["/media/Anime", "/media/TV show"]


def find_new_file(show_dir: str, new_name: str) -> list[str]:
    """在剧文件夹下找新文件名的全部命中（递归）。"""
    hits = []
    for dirpath, dirnames, filenames in os.walk(show_dir):
        dirnames[:] = [d for d in dirnames if d != ".orz"]
        if new_name in filenames:
            hits.append(os.path.join(dirpath, new_name))
    return hits


def plan() -> list[tuple[str, str, str]]:
    """返回 [(旧路径, 现路径, 状态)]，状态：ok / missing / ambiguous / exists。"""
    out = []
    for old, new in RESTORE:
        show_dir = os.path.dirname(os.path.dirname(old))
        if os.path.exists(old):
            out.append((old, "", "exists"))
            continue
        hits = find_new_file(show_dir, new)
        if not hits:
            out.append((old, "", "missing"))
        elif len(hits) > 1:
            out.append((old, "; ".join(hits), "ambiguous"))
        else:
            out.append((old, hits[0], "ok"))
    return out


def remove_empty_duplicate_season_dirs(dry_run: bool) -> list[str]:
    """删空的 `Season 0N`（同剧已有 `Season N` 时），返回删掉的路径。"""
    removed = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for show in sorted(os.listdir(root)):
            show_dir = os.path.join(root, show)
            if not os.path.isdir(show_dir):
                continue
            for entry in sorted(os.listdir(show_dir)):
                if not (entry.startswith("Season 0")
                        and entry[8:].isdigit()):
                    continue
                n = int(entry[8:])
                twin = os.path.join(show_dir, f"Season {n}")
                path = os.path.join(show_dir, entry)
                if (os.path.isdir(twin) and os.path.isdir(path)
                        and not os.listdir(path)):
                    removed.append(path)
                    if not dry_run:
                        os.rmdir(path)
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 v2.6 错改的文件改回原名")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印计划，不动任何文件")
    args = ap.parse_args(argv)

    moves = plan()
    n_ok = sum(1 for _, _, s in moves if s == "ok")
    print(f"共 {len(moves)} 条记录，可恢复 {n_ok} 个，"
          f"其余见下表（missing=新文件找不到，ambiguous=找到多个，"
          f"exists=旧路径已存在，均跳过）：")
    for old, cur, status in moves:
        print(f"  [{status}] {cur or '-'} -> {old}")
    if args.dry_run:
        print("dry-run：什么都没动。确认无误后去掉 --dry-run 再跑一次。")
        return 0
    done = 0
    for old, cur, status in moves:
        if status != "ok":
            continue
        os.makedirs(os.path.dirname(old), exist_ok=True)
        os.rename(cur, old)
        done += 1
    print(f"已移回 {done} 个文件。")
    for show_dir in AFFECTED_SHOWS:
        state = os.path.join(show_dir, ".orz", "state.json")
        if os.path.exists(state):
            os.remove(state)
            print(f"已删除过期的状态文件：{state}")
    for path in remove_empty_duplicate_season_dirs(dry_run=False):
        print(f"已删除空的重名季文件夹：{path}")
    print("完成。青之箱那个文件现在是原名；启动 v2.7.2 的 orz 后，"
          "它会被改成正确的 S01E30。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
