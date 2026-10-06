#!/usr/bin/env python3
"""直播节目串联单后端：Flask + SQLite。

规则概述：
- 节目有起点与总秒数；清单由赞助段（固定时长）与访谈段（目标/最短/最长）组成。
- 排程：以实际已播总时长计算剩余时间，先沿用访谈目标时长，
  再按清单顺序在各访谈的 [最短, 最长] 范围内分摊差额，恰好排到节目终点；
  无法排满或超时则给出差额并拒绝该次操作。
- 每结束当前段录入实际秒数；记录必须保留，哪怕余下清单因此无解
  （标记冲突并允许再次编辑）；已播记录不可改写。
- 备播方案：在待播编辑区把另一种访谈编排命名存为备播，沿用同样的赞助约束与
  排程规则；保存/查看均不影响正在播出的清单。备播快照保存时的待播编排
  （引用旧段 id，并记录保存点已播段数），页面按最新实播记录重算其起止时间，
  排不满时给出差额。启用时校验：保存后已播段必须与备播清单开头同序一致、
  备播引用的其余旧段仍待播、赞助段与现行待播赞助一致、且按最新实播能排到
  节目终点；任一不满足则说明原因且什么都不改，满足则一次性原子替换待播清单
  与排期并清除冲突。备播持久化（重启可查看），重置节目时一并清除。
"""

import os
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, g, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "rundown.db"))
PORT = int(os.environ.get("PORT", "8000"))

app = Flask(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS show_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    start_at TEXT NOT NULL,          -- 节目起点（ISO 时间）
    total_seconds INTEGER NOT NULL,  -- 节目总秒数
    conflict_diff INTEGER,           -- NULL=可恰好排满；>0=还差多少秒排不满；<0=超出多少秒
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position INTEGER NOT NULL,       -- 在串联单中的顺序
    type TEXT NOT NULL CHECK (type IN ('sponsor', 'interview')),
    title TEXT NOT NULL,
    fixed_seconds INTEGER,           -- 赞助段：固定时长
    target_seconds INTEGER,          -- 访谈段：目标时长
    min_seconds INTEGER,             -- 访谈段：最短
    max_seconds INTEGER,             -- 访谈段：最长
    planned_seconds INTEGER,         -- 最近一次成功排程的计划秒数
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    actual_seconds INTEGER,          -- 已播段的实际秒数（不可改写）
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS backup_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,              -- 备播方案名称（可命名）
    aired_count INTEGER NOT NULL,    -- 保存点：保存时已播段数量
    remaining_seconds INTEGER NOT NULL,  -- 保存时的剩余秒数（实际已播总时长推算）
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backup_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_id INTEGER NOT NULL REFERENCES backup_plans(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,       -- 在备播清单中的顺序
    type TEXT NOT NULL CHECK (type IN ('sponsor', 'interview')),
    title TEXT NOT NULL,
    fixed_seconds INTEGER,           -- 赞助段：固定时长（快照）
    target_seconds INTEGER,          -- 访谈段：目标时长（快照）
    min_seconds INTEGER,             -- 访谈段：最短（快照）
    max_seconds INTEGER,             -- 访谈段：最长（快照）
    ref_id INTEGER                   -- 引用保存时待播旧段的 id；新增访谈为 NULL
);
CREATE INDEX IF NOT EXISTS idx_backup_segments ON backup_segments(backup_id, position);
"""


# ---------- 数据库 ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ---------- 排程 ----------

def compute_plan(pending, remaining):
    """对余下待播清单排程。

    先沿用访谈目标时长（赞助段为固定时长），再按清单顺序在各访谈的
    [最短, 最长] 范围内分摊差额，使总时长恰好等于 remaining。

    返回 (plans, None)；无法恰好排到终点时返回
    (尽力排程的 plans, ('under', 缺秒) 或 ('over', 超秒))。
    plans 为按顺序摊到边界后的尽力结果，供展示预计起止时间。
    """
    plans, base = [], 0
    for seg in pending:
        dur = seg["fixed_seconds"] if seg["type"] == "sponsor" else seg["target_seconds"]
        plans.append(dur)
        base += dur

    diff = remaining - base
    if diff > 0:  # 有富余：按清单顺序把访谈拉长，最长到 max
        for i, seg in enumerate(pending):
            if seg["type"] != "interview":
                continue
            take = min(seg["max_seconds"] - seg["target_seconds"], diff)
            plans[i] += take
            diff -= take
            if diff == 0:
                break
        if diff > 0:
            return plans, ("under", diff)
    elif diff < 0:  # 超时：按清单顺序把访谈压短，最短到 min
        need = -diff
        for i, seg in enumerate(pending):
            if seg["type"] != "interview":
                continue
            take = min(seg["target_seconds"] - seg["min_seconds"], need)
            plans[i] -= take
            need -= take
            if need == 0:
                break
        if need > 0:
            return plans, ("over", need)
    return plans, None


def plan_error_message(perr):
    direction, secs = perr
    if direction == "under":
        return f"无法排满节目：访谈段已全部拉到最长，仍差 {secs} 秒"
    return f"节目超时：访谈段已全部压到最短，仍超出 {secs} 秒"


# ---------- 校验 ----------

def parse_segment(raw, idx):
    """解析创建/编辑时提交的一个段落，返回 (seg_dict, error)。"""
    t = raw.get("type")
    title = (raw.get("title") or "").strip()
    if t == "sponsor":
        try:
            fixed = int(raw.get("fixed_seconds"))
            if fixed <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return None, f"第 {idx + 1} 段赞助的固定时长无效（需为正整数秒）"
        return {
            "type": "sponsor",
            "title": title or f"赞助段 {idx + 1}",
            "fixed_seconds": fixed,
        }, None
    if t == "interview":
        try:
            mn = int(raw.get("min_seconds"))
            tg = int(raw.get("target_seconds"))
            mx = int(raw.get("max_seconds"))
            if not (0 < mn <= tg <= mx):
                raise ValueError
        except (TypeError, ValueError):
            return None, f"第 {idx + 1} 段访谈参数无效（需满足 0 < 最短 ≤ 目标 ≤ 最长）"
        return {
            "type": "interview",
            "title": title or f"访谈段 {idx + 1}",
            "min_seconds": mn,
            "target_seconds": tg,
            "max_seconds": mx,
        }, None
    return None, f"第 {idx + 1} 段类型无效"


def parse_pending_payload(raw, pending):
    """解析“整体替换待播清单”的提交（编辑待播与存为备播共用）。

    规则：赞助段必须与现行待播赞助段一一对应，先后顺序、id 与时长不可变；
    访谈段可新增（无 id）或引用未播旧段（带 id，可改参数），顺序可调。

    成功返回 (new_rows, None)；new_rows 为可直接用于 compute_plan 的字典列表，
    引用段带 "id"。失败返回 (None, error)。
    """
    if not isinstance(raw, list):
        return None, "提交格式无效"

    pending_by_id = {s["id"]: s for s in pending}
    old_sponsors = [s for s in pending if s["type"] == "sponsor"]

    new_rows = []
    sponsor_idx = 0
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            return None, "提交格式无效"
        if r.get("type") == "sponsor":
            # 赞助段：必须与原待播赞助段一一对应，顺序与时长不可变
            if sponsor_idx >= len(old_sponsors):
                return None, "赞助段的数量不能改变"
            old = old_sponsors[sponsor_idx]
            sponsor_idx += 1
            try:
                rid = int(r.get("id"))
                fixed = int(r.get("fixed_seconds"))
            except (TypeError, ValueError):
                return None, "赞助段参数无效"
            if rid != old["id"]:
                return None, "赞助段的先后顺序不能改变"
            if fixed != old["fixed_seconds"]:
                return None, f"赞助段「{old['title']}」的时长不能改变"
            new_rows.append(
                {
                    "id": old["id"],
                    "type": "sponsor",
                    "title": old["title"],
                    "fixed_seconds": old["fixed_seconds"],
                }
            )
        elif r.get("type") == "interview":
            seg, err = parse_segment(r, i)
            if err:
                return None, err
            if r.get("id") is not None:
                try:
                    rid = int(r.get("id"))
                except (TypeError, ValueError):
                    return None, "段落编号无效"
                old = pending_by_id.get(rid)
                if old is None or old["type"] != "interview":
                    return None, "只能编辑未播出的访谈段"
                seg["id"] = rid
            new_rows.append(seg)
        else:
            return None, f"第 {i + 1} 段类型无效"

    if sponsor_idx != len(old_sponsors):
        return None, "赞助段不能删除"

    kept_ids = [r["id"] for r in new_rows if "id" in r]
    if len(kept_ids) != len(set(kept_ids)):
        return None, "段落重复"
    return new_rows, None


def get_show(db):
    return db.execute("SELECT * FROM show_meta WHERE id = 1").fetchone()


def get_pending(db):
    return db.execute(
        "SELECT * FROM segments WHERE status = 'pending' ORDER BY position"
    ).fetchall()


def get_aired_seconds(db):
    row = db.execute(
        "SELECT COALESCE(SUM(actual_seconds), 0) AS s FROM segments WHERE status = 'done'"
    ).fetchone()
    return row["s"]


def display_duration(seg):
    """冲突状态下用于展示的时长：赞助段固定值 / 访谈段目标值。"""
    if seg["type"] == "sponsor":
        return seg["fixed_seconds"]
    return seg["target_seconds"]


# ---------- 备播方案 ----------

def get_backup_rows(db, backup):
    return db.execute(
        "SELECT * FROM backup_segments WHERE backup_id = ? ORDER BY position",
        (backup["id"],),
    ).fetchall()


def evaluate_backup(backup, brows, all_segments, remaining):
    """按最新实播情况评估一份备播能否启用。

    all_segments：当前完整清单（含已播与待播，按 position）。
    remaining：按最新实播总时长算出的剩余秒数。

    成功返回 dict：
      ok=True, remaining_rows=备播中尚未播出的部分（字典）,
      matched=保存后已播段与备播开头匹配的段数, plans=排程结果。
    失败返回 dict：ok=False, reason=原因（供页面展示与接口返回）。
    """
    done = [s for s in all_segments if s["status"] == "done"]
    pending_by_id = {s["id"]: s for s in all_segments if s["status"] == "pending"}

    # 保存点之后新播出的段，必须是备播原清单中同序的开头
    after = done[backup["aired_count"]:]
    matched = 0
    for i, d in enumerate(after):
        if i >= len(brows) or brows[i]["ref_id"] != d["id"]:
            return {
                "ok": False,
                "reason": (
                    f"保存备播后已播的第 {backup['aired_count'] + i + 1} 段"
                    f"「{d['title']}」与备播清单开头顺序不一致，无法启用"
                ),
            }
        matched += 1

    # 备播引用的其余旧段必须仍待播
    rest = brows[matched:]
    for r in rest:
        rid = r["ref_id"]
        if rid is None:
            continue  # 备播中新增的访谈段
        cur = pending_by_id.get(rid)
        if cur is None:
            return {
                "ok": False,
                "reason": f"备播引用的旧段「{r['title']}」已不在待播清单中，无法启用",
            }

    # 备播中其余赞助段必须与现行待播赞助段（顺序、id、时长）一致
    cur_sponsors = [s for s in all_segments if s["status"] == "pending" and s["type"] == "sponsor"]
    rest_sponsors = [r for r in rest if r["type"] == "sponsor"]
    if len(rest_sponsors) != len(cur_sponsors):
        return {"ok": False, "reason": "备播的赞助段与现行待播赞助段数量不一致，无法启用"}
    for rb, cb in zip(rest_sponsors, cur_sponsors):
        if rb["ref_id"] != cb["id"]:
            return {
                "ok": False,
                "reason": "备播的赞助段先后顺序与现行待播赞助段不一致，无法启用",
            }
        if rb["fixed_seconds"] != cb["fixed_seconds"]:
            return {
                "ok": False,
                "reason": f"备播赞助段「{rb['title']}」的时长与现行待播赞助段不一致，无法启用",
            }

    rest_dicts = [dict(r) for r in rest]
    plans, perr = compute_plan(rest_dicts, remaining)
    if perr:
        return {
            "ok": False,
            "reason": plan_error_message(perr) + "，无法排到节目终点",
            "plans": plans,
            "rest": rest_dicts,
            "matched": matched,
            "perr": perr,
        }
    return {
        "ok": True,
        "rest": rest_dicts,
        "matched": matched,
        "plans": plans,
        "perr": None,
    }


def build_backup_views(db, show=None, all_segments=None, aired=None):
    """构建全部备播方案的视图（供 /api/state 返回）。

    保存与查看备播都不改变正在播出的清单：这里只读取并按最新实播重算。
    """
    if show is None:
        show = get_show(db)
    if not show:
        return []
    if all_segments is None:
        all_segments = db.execute("SELECT * FROM segments ORDER BY position").fetchall()
    if aired is None:
        aired = sum(s["actual_seconds"] or 0 for s in all_segments if s["status"] == "done")
    remaining = show["total_seconds"] - aired
    start = datetime.fromisoformat(show["start_at"])

    pending_by_id = {s["id"]: s for s in all_segments if s["status"] == "pending"}
    done_by_id = {s["id"]: s for s in all_segments if s["status"] == "done"}

    views = []
    for backup in db.execute("SELECT * FROM backup_plans ORDER BY id").fetchall():
        brows = get_backup_rows(db, backup)
        ev = evaluate_backup(backup, brows, all_segments, remaining)

        if ev["ok"]:
            status, reason, diff = "ready", None, 0
        else:
            # 结构性不兼容（无排程结果）时无法给出起止时间
            if "plans" not in ev:
                status, reason, diff = "incompatible", ev["reason"], None
            else:
                status, reason = "conflict", ev["reason"]
                direction, secs = ev["perr"]
                diff = secs if direction == "under" else -secs

        segments = []
        if diff is None:
            # 不兼容：仅列出备播快照，不重算时间
            for r in brows:
                segments.append(
                    {
                        "position": r["position"],
                        "type": r["type"],
                        "title": r["title"],
                        "fixed_seconds": r["fixed_seconds"],
                        "target_seconds": r["target_seconds"],
                        "min_seconds": r["min_seconds"],
                        "max_seconds": r["max_seconds"],
                        "match_status": "unknown",
                        "est_start": None,
                        "est_end": None,
                        "planned_seconds": None,
                    }
                )
            est_start = est_end = None
        else:
            matched = ev["matched"]
            # 时间轴：已播段按实际秒数推进。匹配上的备播行会在下面的循环中计入，
            # 故起点只含“不在备播清单中的已播前缀”（如保存点之前已播的段）
            matched_actual = sum(
                (done_by_id.get(brows[i]["ref_id"])["actual_seconds"] or 0)
                for i in range(matched)
            )
            offset = aired - matched_actual
            est_start = (start + timedelta(seconds=offset)).isoformat()
            for i, r in enumerate(brows):
                if i < matched:
                    d = done_by_id.get(r["ref_id"])
                    plan = d["actual_seconds"] if d else None
                    match_status = "aired"
                else:
                    plan = ev["plans"][i - matched]
                    cur = pending_by_id.get(r["ref_id"])
                    match_status = "pending" if cur is not None else "new"
                s_start = start + timedelta(seconds=offset)
                offset += plan or 0
                s_end = start + timedelta(seconds=offset)
                segments.append(
                    {
                        "position": r["position"],
                        "type": r["type"],
                        "title": r["title"],
                        "fixed_seconds": r["fixed_seconds"],
                        "target_seconds": r["target_seconds"],
                        "min_seconds": r["min_seconds"],
                        "max_seconds": r["max_seconds"],
                        "match_status": match_status,
                        "planned_seconds": plan,
                        "est_start": s_start.isoformat(),
                        "est_end": s_end.isoformat(),
                    }
                )
            est_end = (start + timedelta(seconds=offset)).isoformat()

        views.append(
            {
                "id": backup["id"],
                "name": backup["name"],
                "created_at": backup["created_at"],
                "aired_count": backup["aired_count"],
                "status": status,
                "reason": reason,
                "diff_seconds": diff,
                "est_start": est_start,
                "est_end": est_end,
                "program_end": (start + timedelta(seconds=show["total_seconds"])).isoformat(),
                "segments": segments,
            }
        )
    return views


# ---------- 状态 ----------

def build_state():
    db = get_db()
    show = get_show(db)
    if not show:
        return {"show": None, "segments": [], "now": datetime.now().isoformat()}

    seg_rows = db.execute("SELECT * FROM segments ORDER BY position").fetchall()
    segs = [dict(r) for r in seg_rows]
    start = datetime.fromisoformat(show["start_at"])
    offset = 0
    for s in segs:
        # 已播段按实际秒数推进时间轴，待播段按计划秒数
        dur = s["actual_seconds"] if s["status"] == "done" else s["planned_seconds"]
        if dur is None:
            dur = display_duration(s)
        s["est_start"] = (start + timedelta(seconds=offset)).isoformat()
        offset += dur
        s["est_end"] = (start + timedelta(seconds=offset)).isoformat()

    aired = sum(s["actual_seconds"] or 0 for s in segs if s["status"] == "done")
    return {
        "show": {
            "start_at": show["start_at"],
            "total_seconds": show["total_seconds"],
            "conflict_diff": show["conflict_diff"],
            "aired_seconds": aired,
            "remaining_seconds": show["total_seconds"] - aired,
            "est_end": (start + timedelta(seconds=offset)).isoformat(),
        },
        "segments": segs,
        "backups": build_backup_views(db, show, seg_rows, aired),
        "now": datetime.now().isoformat(),
    }


# ---------- 页面与接口 ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(build_state())


@app.route("/api/show", methods=["POST"])
def create_show():
    """创建节目并录入整份初始清单（赞助段 + 访谈段）。"""
    db = get_db()
    if get_show(db):
        return jsonify({"ok": False, "error": "节目已存在，如需重来请先重置"}), 400

    data = request.get_json(force=True, silent=True) or {}
    try:
        start_at = datetime.fromisoformat(str(data.get("start_at", "")))
        total = int(data.get("total_seconds"))
        if total <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "节目起点或总秒数无效"}), 400

    raw_segments = data.get("segments") or []
    if not raw_segments:
        return jsonify({"ok": False, "error": "串联单不能为空"}), 400
    segs = []
    for i, raw in enumerate(raw_segments):
        seg, err = parse_segment(raw, i)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        segs.append(seg)

    plans, perr = compute_plan(segs, total)
    if perr:
        return jsonify({"ok": False, "error": plan_error_message(perr)}), 400

    db.execute(
        "INSERT INTO show_meta (id, start_at, total_seconds, conflict_diff, created_at) "
        "VALUES (1, ?, ?, NULL, ?)",
        (start_at.isoformat(), total, datetime.now().isoformat()),
    )
    for pos, (seg, plan) in enumerate(zip(segs, plans)):
        db.execute(
            "INSERT INTO segments (position, type, title, fixed_seconds, target_seconds, "
            "min_seconds, max_seconds, planned_seconds, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                pos,
                seg["type"],
                seg["title"],
                seg.get("fixed_seconds"),
                seg.get("target_seconds"),
                seg.get("min_seconds"),
                seg.get("max_seconds"),
                plan,
            ),
        )
    db.commit()
    return jsonify({"ok": True, "state": build_state()})


@app.route("/api/segments/complete", methods=["POST"])
def complete_current():
    """结束当前段（清单中第一段待播段），录入实际秒数。

    记录必须保留：即使余下清单因此无解，也保存记录并标记冲突。
    """
    db = get_db()
    show = get_show(db)
    if not show:
        return jsonify({"ok": False, "error": "尚未创建节目"}), 400
    current = db.execute(
        "SELECT * FROM segments WHERE status = 'pending' ORDER BY position LIMIT 1"
    ).fetchone()
    if not current:
        return jsonify({"ok": False, "error": "所有段落均已播出"}), 400

    data = request.get_json(force=True, silent=True) or {}
    try:
        actual = int(data.get("actual_seconds"))
        if actual <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "实际秒数无效（需为正整数）"}), 400

    # 已播记录不可改写：仅在该段仍为 pending 时落库
    db.execute(
        "UPDATE segments SET status = 'done', actual_seconds = ?, completed_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (actual, datetime.now().isoformat(), current["id"]),
    )

    aired = get_aired_seconds(db)
    remaining = show["total_seconds"] - aired
    pending = get_pending(db)
    plans, perr = compute_plan(pending, remaining)
    if perr:
        # 余下清单无解：记录保留，标记冲突，待播段按目标/固定时长展示
        conflict_diff = perr[1] if perr[0] == "under" else -perr[1]
        db.execute("UPDATE show_meta SET conflict_diff = ? WHERE id = 1", (conflict_diff,))
        for seg in pending:
            db.execute(
                "UPDATE segments SET planned_seconds = ? WHERE id = ?",
                (display_duration(seg), seg["id"]),
            )
        db.commit()
        return jsonify(
            {
                "ok": True,
                "conflict": True,
                "message": plan_error_message(perr),
                "state": build_state(),
            }
        )

    db.execute("UPDATE show_meta SET conflict_diff = NULL WHERE id = 1")
    for seg, plan in zip(pending, plans):
        db.execute(
            "UPDATE segments SET planned_seconds = ? WHERE id = ?", (plan, seg["id"])
        )
    db.commit()
    return jsonify({"ok": True, "state": build_state()})


@app.route("/api/rundown/edit", methods=["POST"])
def edit_rundown():
    """整体替换待播清单：可插入、删除、移动访谈段；
    赞助段的时长与先后顺序不可变。排程失败则拒绝此次编辑。"""
    db = get_db()
    show = get_show(db)
    if not show:
        return jsonify({"ok": False, "error": "尚未创建节目"}), 400
    pending = get_pending(db)
    if not pending:
        return jsonify({"ok": False, "error": "没有可编辑的待播段落"}), 400

    data = request.get_json(force=True, silent=True) or {}
    new_rows, err = parse_pending_payload(data.get("segments"), pending)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    # 排程：无法恰好排到终点则拒绝此次编辑
    remaining = show["total_seconds"] - get_aired_seconds(db)
    plans, perr = compute_plan(new_rows, remaining)
    if perr:
        return (
            jsonify(
                {"ok": False, "error": plan_error_message(perr) + "，本次编辑未保存"}
            ),
            400,
        )

    # 应用：删除未保留的待播访谈段，重排全部位置（已播段是清单前缀，位置不变）
    kept = {r["id"] for r in new_rows if "id" in r}
    for s in pending:
        if s["id"] not in kept:
            db.execute("DELETE FROM segments WHERE id = ?", (s["id"],))

    pos = db.execute(
        "SELECT COALESCE(MAX(position), -1) AS p FROM segments WHERE status = 'done'"
    ).fetchone()["p"] + 1
    for row, plan in zip(new_rows, plans):
        if "id" in row:
            if row["type"] == "sponsor":
                db.execute(
                    "UPDATE segments SET position = ?, planned_seconds = ? WHERE id = ?",
                    (pos, plan, row["id"]),
                )
            else:
                db.execute(
                    "UPDATE segments SET position = ?, title = ?, target_seconds = ?, "
                    "min_seconds = ?, max_seconds = ?, planned_seconds = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (
                        pos,
                        row["title"],
                        row["target_seconds"],
                        row["min_seconds"],
                        row["max_seconds"],
                        plan,
                        row["id"],
                    ),
                )
        else:
            db.execute(
                "INSERT INTO segments (position, type, title, target_seconds, "
                "min_seconds, max_seconds, planned_seconds, status) "
                "VALUES (?, 'interview', ?, ?, ?, ?, ?, 'pending')",
                (
                    pos,
                    row["title"],
                    row["target_seconds"],
                    row["min_seconds"],
                    row["max_seconds"],
                    plan,
                ),
            )
        pos += 1

    db.execute("UPDATE show_meta SET conflict_diff = NULL WHERE id = 1")
    db.commit()
    return jsonify({"ok": True, "state": build_state()})


@app.route("/api/backups", methods=["POST"])
def create_backup():
    """把编辑区中的另一种编排存为命名备播方案。

    沿用编辑待播的赞助约束与排程规则：按保存时的最新实播排程，
    无法恰好排到节目终点则拒绝保存。保存不影响正在播出的清单。
    """
    db = get_db()
    show = get_show(db)
    if not show:
        return jsonify({"ok": False, "error": "尚未创建节目"}), 400
    pending = get_pending(db)
    if not pending:
        return jsonify({"ok": False, "error": "没有可保存的待播段落"}), 400

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "请为备播方案命名"}), 400
    if len(name) > 40:
        return jsonify({"ok": False, "error": "备播名称不能超过 40 个字"}), 400

    new_rows, err = parse_pending_payload(data.get("segments"), pending)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    aired = get_aired_seconds(db)
    remaining = show["total_seconds"] - aired
    plans, perr = compute_plan(new_rows, remaining)
    if perr:
        return (
            jsonify(
                {"ok": False, "error": plan_error_message(perr) + "，该备播未保存"}
            ),
            400,
        )

    aired_count = db.execute(
        "SELECT COUNT(*) AS c FROM segments WHERE status = 'done'"
    ).fetchone()["c"]
    cur = db.execute(
        "INSERT INTO backup_plans (name, aired_count, remaining_seconds, created_at) "
        "VALUES (?, ?, ?, ?)",
        (name, aired_count, remaining, datetime.now().isoformat()),
    )
    backup_id = cur.lastrowid
    for pos, (row, plan) in enumerate(zip(new_rows, plans)):
        db.execute(
            "INSERT INTO backup_segments (backup_id, position, type, title, "
            "fixed_seconds, target_seconds, min_seconds, max_seconds, ref_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                backup_id,
                pos,
                row["type"],
                row["title"],
                row.get("fixed_seconds"),
                row.get("target_seconds"),
                row.get("min_seconds"),
                row.get("max_seconds"),
                row.get("id"),
            ),
        )
    db.commit()
    return jsonify({"ok": True, "state": build_state()})


@app.route("/api/backups/<int:backup_id>/activate", methods=["POST"])
def activate_backup(backup_id):
    """启用备播：校验通过后一次性原子替换待播清单与排期，清除冲突。

    校验：保存后已播段必须是备播原清单中同序的开头；备播引用的其余旧段
    仍待播；备播其余赞助段与现行待播赞助段（顺序、id、时长）一致；
    按最新实播时长能恰好排到节目终点。任一不满足则说明原因，
    原清单、冲突、已播记录及备播方案均不变。
    """
    db = get_db()
    show = get_show(db)
    if not show:
        return jsonify({"ok": False, "error": "尚未创建节目"}), 400
    backup = db.execute(
        "SELECT * FROM backup_plans WHERE id = ?", (backup_id,)
    ).fetchone()
    if not backup:
        return jsonify({"ok": False, "error": "备播方案不存在"}), 404

    all_segments = db.execute("SELECT * FROM segments ORDER BY position").fetchall()
    aired = get_aired_seconds(db)
    remaining = show["total_seconds"] - aired
    brows = get_backup_rows(db, backup)
    ev = evaluate_backup(backup, brows, all_segments, remaining)
    if not ev["ok"]:
        # 说明原因，原清单、冲突、已播记录及备播方案均不变
        return jsonify({"ok": False, "error": ev["reason"]}), 400

    rest = ev["rest"]
    plans = ev["plans"]
    kept = {r["ref_id"] for r in rest if r["ref_id"] is not None}

    # 一次性替换：删除未被备播保留的待播段（已播记录不动）
    for s in all_segments:
        if s["status"] == "pending" and s["id"] not in kept:
            db.execute("DELETE FROM segments WHERE id = ?", (s["id"],))

    pos = backup["aired_count"] + ev["matched"]
    for row, plan in zip(rest, plans):
        rid = row["ref_id"]
        if rid is not None:
            if row["type"] == "sponsor":
                db.execute(
                    "UPDATE segments SET position = ?, planned_seconds = ? WHERE id = ?",
                    (pos, plan, rid),
                )
            else:
                db.execute(
                    "UPDATE segments SET position = ?, title = ?, target_seconds = ?, "
                    "min_seconds = ?, max_seconds = ?, planned_seconds = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (
                        pos,
                        row["title"],
                        row["target_seconds"],
                        row["min_seconds"],
                        row["max_seconds"],
                        plan,
                        rid,
                    ),
                )
        else:
            db.execute(
                "INSERT INTO segments (position, type, title, target_seconds, "
                "min_seconds, max_seconds, planned_seconds, status) "
                "VALUES (?, 'interview', ?, ?, ?, ?, ?, 'pending')",
                (
                    pos,
                    row["title"],
                    row["target_seconds"],
                    row["min_seconds"],
                    row["max_seconds"],
                    plan,
                ),
            )
        pos += 1

    # 冲突已随新清单解决
    db.execute("UPDATE show_meta SET conflict_diff = NULL WHERE id = 1")
    db.commit()
    return jsonify({"ok": True, "state": build_state()})


@app.route("/api/backups/<int:backup_id>/delete", methods=["POST"])
def delete_backup(backup_id):
    """删除一份备播方案（不影响正在播出的清单）。"""
    db = get_db()
    cur = db.execute("DELETE FROM backup_plans WHERE id = ?", (backup_id,))
    db.execute("DELETE FROM backup_segments WHERE backup_id = ?", (backup_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"ok": False, "error": "备播方案不存在"}), 404
    return jsonify({"ok": True, "state": build_state()})


@app.route("/api/show/reset", methods=["POST"])
def reset_show():
    """删除整档节目（含全部记录与备播方案），用于重新开始。"""
    db = get_db()
    db.execute("DELETE FROM backup_segments")
    db.execute("DELETE FROM backup_plans")
    db.execute("DELETE FROM segments")
    db.execute("DELETE FROM show_meta")
    db.commit()
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
