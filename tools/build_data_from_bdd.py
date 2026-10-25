#!/usr/bin/env python3
"""从 BDD 导出的 JSON 生成 shared/data/*.json。

数据来源：tools/dump_bdd.py 产出的 archive/game_data/BDD/*.json

注意：这些表来自 pmdata.bin（BDD），是**游戏运行时真正用的那份**；
      Lua 的 tabledata/*.luac 只是兜底，条数少很多
      （物品 1866 vs 2728，角色 105 vs 193，卡池 6 vs 84，活动 47 vs 227）。

用法:
    python tools/build_data_from_bdd.py --bdd <BDD 目录> --out <shared/data 目录>
"""
import argparse
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC_ITEMS = "BDD:TableData.CommonData.ItemData (pmdata.bin)"
SRC_CHARS = "BDD:TableData.CommonData.CharData (pmdata.bin)"
SRC_CURR = "BDD:TableData.CommonData.CurrencyData (pmdata.bin)"
SRC_ACT = "BDD:TableData.CommonData.ActivityData (pmdata.bin)"
SRC_TASK = "BDD:TableData.CommonData.TaskData (pmdata.bin)"
SRC_MAP = "BDD:TableData.CommonData.MainlineMapData (pmdata.bin)"
SRC_GACHA = "BDD:TableData.CommonData.GachaData (pmdata.bin)"


def char_tier(char_id, show):
    """0 = isShow 可玩；1 = 基础形态；2 = 皮肤/变体。"""
    if show is True:
        return 0
    try:
        return 1 if int(char_id) < 100000 else 2
    except (TypeError, ValueError):
        return 2


def item_tier(item_type, use_type):
    """0 = 给角色的物品；1 = 常规物品；2 = 其它。"""
    if use_type == "char":
        return 0
    try:
        if int(item_type) == 1:
            return 1
    except (TypeError, ValueError):
        pass
    return 2


def load(bdd_dir, name):
    path = os.path.join(bdd_dir, "TableData.CommonData.{}.json".format(name))
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def rows_of(obj):
    """兼容 dict / list 两种形态，统一产出 (key, record)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                yield k, v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, dict):
                yield str(i), v


def build_items(bdd_dir):
    rows = []
    for key, v in rows_of(load(bdd_dir, "ItemData") or {}):
        iid, name = v.get("itemId"), v.get("name")
        if iid is None or not name:
            continue
        itype, use_type = v.get("itemType"), v.get("useType")
        rows.append({
            "id": int(iid), "name": str(name), "type": itype,
            "quality": v.get("quality"), "use_type": use_type,
            "tier": item_tier(itype, use_type),
        })
    rows.sort(key=lambda r: r["id"])
    return {"version": 2, "source": SRC_ITEMS, "count": len(rows), "items": rows}


def build_chars(bdd_dir):
    rows = []
    for key, v in rows_of(load(bdd_dir, "CharData") or {}):
        cid, name = v.get("charId"), v.get("charName")
        if cid is None or not name:
            continue
        show = v.get("isShow")
        rows.append({
            "id": int(cid), "name": str(name),
            "name_en": v.get("charNameEN") or "",
            "title": v.get("charTitle") or "",
            "grade": v.get("charGrade"),
            "area": v.get("charArea") or "",
            "cv": v.get("charCV") or "",
            "show": bool(show), "tier": char_tier(cid, show),
        })
    rows.sort(key=lambda r: r["id"])
    return {"version": 2, "source": SRC_CHARS, "count": len(rows), "chars": rows}


def build_currencies(bdd_dir):
    rows = []
    for key, v in rows_of(load(bdd_dir, "CurrencyData") or {}):
        cid, iid = v.get("id") or key, v.get("itemId")
        if not cid:
            continue
        rows.append({"id": str(cid), "item_id": int(iid) if iid is not None else None})
    rows.sort(key=lambda r: r["id"])
    return {"version": 2, "source": SRC_CURR, "count": len(rows), "currencies": rows}


def build_activities(bdd_dir):
    rows = []
    for key, v in rows_of(load(bdd_dir, "ActivityData") or {}):
        aid = v.get("activityId")
        if aid is None:
            try:
                aid = int(key)
            except (TypeError, ValueError):
                continue
        span = v.get("span") or []
        rows.append({
            "id": int(aid),
            "name": v.get("name") or "",
            "type": v.get("type"),
            "show": bool(v.get("isShow")),
            "start": span[0] if len(span) > 0 else None,
            "end": span[1] if len(span) > 1 else None,
        })
    rows.sort(key=lambda r: r["id"])
    return {"version": 2, "source": SRC_ACT, "count": len(rows), "activities": rows}


def build_tasks(bdd_dir):
    rows = []
    for key, v in rows_of(load(bdd_dir, "TaskData") or {}):
        tid = v.get("uiD")
        if tid is None:
            try:
                tid = int(key)
            except (TypeError, ValueError):
                continue
        rows.append({
            "id": int(tid),
            "desc": v.get("Description") or "",
            "type": v.get("TaskType"),
            "pre": v.get("PreTask"),
        })
    rows.sort(key=lambda r: r["id"])
    return {"version": 2, "source": SRC_TASK, "count": len(rows), "tasks": rows}


def build_maps(bdd_dir):
    rows = []
    for key, v in rows_of(load(bdd_dir, "MainlineMapData") or {}):
        ref = v.get("referenceId") or key
        try:
            mid = int(ref)
        except (TypeError, ValueError):
            continue
        rows.append({
            "id": mid,
            "name": v.get("roomName") or v.get("themeName") or "",
            "type": v.get("mapType"),
            "power": v.get("powerId") or "",
        })
    rows.sort(key=lambda r: r["id"])
    return {"version": 2, "source": SRC_MAP, "count": len(rows), "maps": rows}


def build_stories(bdd_dir):
    rows = []
    for key, v in rows_of(load(bdd_dir, "StoryData") or {}):
        name = v.get("dialogueName")
        if not name:
            continue
        rows.append({
            "id": str(name),
            "name": str(name),
            "speech_id": v.get("speechId"),
        })
    rows.sort(key=lambda r: r["id"])
    return {"version": 2, "source": "BDD:TableData.CommonData.StoryData (pmdata.bin)",
            "count": len(rows), "stories": rows}


def build_gachas(bdd_dir):
    rows = []
    for key, v in rows_of(load(bdd_dir, "GachaData") or {}):
        gid = v.get("gachaId")
        if gid is None:
            try:
                gid = int(key)
            except (TypeError, ValueError):
                continue
        rows.append({
            "id": int(gid),
            "name": v.get("name") or "",
            "type": v.get("type"),
            "guarantee": v.get("GuaranteeTimes"),
        })
    rows.sort(key=lambda r: r["id"])
    return {"version": 2, "source": SRC_GACHA, "count": len(rows), "gachas": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bdd", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    builders = [
        ("items.json", build_items, "items"),
        ("chars.json", build_chars, "chars"),
        ("currencies.json", build_currencies, "currencies"),
        ("activities.json", build_activities, "activities"),
        ("tasks.json", build_tasks, "tasks"),
        ("maps.json", build_maps, "maps"),
        ("gachas.json", build_gachas, "gachas"),
        ("stories.json", build_stories, "stories"),
    ]
    os.makedirs(args.out, exist_ok=True)
    for fname, fn, _ in builders:
        obj = fn(args.bdd)
        path = os.path.join(args.out, fname)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=0, separators=(",", ":"))
            f.write("\n")
        print("写出 %-18s %6d 条  %8.1f KB"
              % (fname, obj["count"], os.path.getsize(path) / 1024.0))

    chars = build_chars(args.bdd)["chars"]
    build_items(args.bdd)["items"]
    print()
    print('可玩角色（tier 0）共 {} 个：'.format(sum(1 for c in chars if c["tier"] == 0)))
    for c in chars:
        if c["tier"] == 0:
            print("  %-6d %-8s %-10s grade=%s cv=%s"
                  % (c["id"], c["name"], c["title"], c["grade"], c["cv"] or "-"))


if __name__ == "__main__":
    sys.exit(main())
