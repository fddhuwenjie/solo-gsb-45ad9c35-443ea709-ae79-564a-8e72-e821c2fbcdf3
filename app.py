#!/usr/bin/env python3
"""直播节目串联单后端：Flask + SQLite。

规则概述：
- 节目有起点与总秒数；清单由赞助段（固定时长）与访谈段（目标/最短/最长）组成。
- 排程：以实际已播总时长计算剩余时间，先沿用访谈目标时长，
  再按清单顺序在各访谈的 [最短, 最长] 范围内分摊差额，恰好排到节目终点；
  无法排满或超时则给出差额并拒绝该次操作。
- 每结束当前段录入实际秒数；记录必须保留，哪怕余下清单因此无解
  （标记冲突并允许再次编辑）；已播记录不可改写。
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
    (None, ('under', 缺秒) 或 ('over', 超秒))。
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
            return None, ("under", diff)
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
            return None, ("over", need)
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


# ---------- 状态 ----------

def build_state():
    db = get_db()
    show = get_show(db)
    if not show:
        return {"show": None, "segments": [], "now": datetime.now().isoformat()}

    segs = [dict(r) for r in db.execute("SELECT * FROM segments ORDER BY position").fetchall()]
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
    raw = data.get("segments")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "error": "提交格式无效"}), 400

    pending_by_id = {s["id"]: s for s in pending}
    old_sponsors = [s for s in pending if s["type"] == "sponsor"]

    new_rows = []
    sponsor_idx = 0
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            return jsonify({"ok": False, "error": "提交格式无效"}), 400
        if r.get("type") == "sponsor":
            # 赞助段：必须与原待播赞助段一一对应，顺序与时长不可变
            if sponsor_idx >= len(old_sponsors):
                return jsonify({"ok": False, "error": "赞助段的数量不能改变"}), 400
            old = old_sponsors[sponsor_idx]
            sponsor_idx += 1
            try:
                rid = int(r.get("id"))
                fixed = int(r.get("fixed_seconds"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "赞助段参数无效"}), 400
            if rid != old["id"]:
                return jsonify({"ok": False, "error": "赞助段的先后顺序不能改变"}), 400
            if fixed != old["fixed_seconds"]:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"赞助段「{old['title']}」的时长不能改变",
                        }
                    ),
                    400,
                )
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
                return jsonify({"ok": False, "error": err}), 400
            if r.get("id") is not None:
                try:
                    rid = int(r.get("id"))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "段落编号无效"}), 400
                old = pending_by_id.get(rid)
                if old is None or old["type"] != "interview":
                    return jsonify({"ok": False, "error": "只能编辑未播出的访谈段"}), 400
                seg["id"] = rid
            new_rows.append(seg)
        else:
            return jsonify({"ok": False, "error": f"第 {i + 1} 段类型无效"}), 400

    if sponsor_idx != len(old_sponsors):
        return jsonify({"ok": False, "error": "赞助段不能删除"}), 400

    kept_ids = [r["id"] for r in new_rows if "id" in r]
    if len(kept_ids) != len(set(kept_ids)):
        return jsonify({"ok": False, "error": "段落重复"}), 400

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
    kept = set(kept_ids)
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


@app.route("/api/show/reset", methods=["POST"])
def reset_show():
    """删除整档节目（含全部记录），用于重新开始。"""
    db = get_db()
    db.execute("DELETE FROM segments")
    db.execute("DELETE FROM show_meta")
    db.commit()
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
