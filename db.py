import json
import os
import sqlite3

# На сервере укажи путь на постоянном диске (volume), например DB_PATH=/data/game.db
DB_PATH = os.getenv("DB_PATH", "game.db")


def run(sql, args=(), fetch=None):
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        c.commit()
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return cur.lastrowid
    finally:
        c.close()


def init():
    c = sqlite3.connect(DB_PATH)
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, eliminated INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS stages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, deadline INTEGER,
            side TEXT DEFAULT '[]', result TEXT, scored INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS predictions(
            user_id INTEGER, stage_id INTEGER, answers TEXT, score REAL DEFAULT 0,
            PRIMARY KEY(user_id, stage_id));
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        """
    )
    c.commit()
    c.close()


# ---- kv (offset обновлений, состояния диалогов, время последней проверки подписок)
def kv_get(k):
    r = run("SELECT v FROM kv WHERE k=?", (k,), "one")
    return r["v"] if r else None


def kv_set(k, v):
    if v is None:
        run("DELETE FROM kv WHERE k=?", (k,))
    else:
        run(
            "INSERT INTO kv(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )


# ---- users
def upsert_user(u):
    run(
        "INSERT INTO users(user_id, username, name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name",
        (u.id, u.username, u.full_name),
    )


def get_user(uid):
    return run("SELECT * FROM users WHERE user_id=?", (uid,), "one")


def active_users():
    return run("SELECT * FROM users WHERE eliminated=0", fetch="all")


def set_eliminated(uid):
    run("UPDATE users SET eliminated=1 WHERE user_id=?", (uid,))


# ---- stages
def create_stage(name, deadline):
    return run("INSERT INTO stages(name, deadline) VALUES(?,?)", (name, deadline))


def get_stage(sid):
    return run("SELECT * FROM stages WHERE id=?", (sid,), "one")


def all_stages():
    return run("SELECT * FROM stages ORDER BY id", fetch="all")


def open_stages(now):
    return run("SELECT * FROM stages WHERE deadline>? AND scored=0 ORDER BY deadline", (now,), "all")


def add_side(sid, text):
    st = get_stage(sid)
    side = json.loads(st["side"])
    side.append(text)
    run("UPDATE stages SET side=? WHERE id=?", (json.dumps(side, ensure_ascii=False), sid))
    return len(side)


def save_result(sid, result):
    run("UPDATE stages SET result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), sid))


def mark_scored(sid):
    run("UPDATE stages SET scored=1 WHERE id=?", (sid,))


# ---- predictions
def save_prediction(uid, sid, answers):
    run(
        "INSERT INTO predictions(user_id, stage_id, answers) VALUES(?,?,?) "
        "ON CONFLICT(user_id, stage_id) DO UPDATE SET answers=excluded.answers",
        (uid, sid, json.dumps(answers, ensure_ascii=False)),
    )


def stage_predictions(sid):
    return run("SELECT * FROM predictions WHERE stage_id=?", (sid,), "all")


def set_score(uid, sid, score):
    run("UPDATE predictions SET score=? WHERE user_id=? AND stage_id=?", (score, uid, sid))


def leaderboard(limit=10):
    return run(
        "SELECT u.user_id, u.name, u.username, COALESCE(SUM(p.score),0) AS total "
        "FROM users u LEFT JOIN predictions p ON p.user_id=u.user_id "
        "WHERE u.eliminated=0 GROUP BY u.user_id ORDER BY total DESC LIMIT ?",
        (limit,),
        "all",
    )


def stage_top(sid, limit=10):
    return run(
        "SELECT u.name, u.username, p.score FROM predictions p JOIN users u ON u.user_id=p.user_id "
        "WHERE p.stage_id=? AND u.eliminated=0 ORDER BY p.score DESC LIMIT ?",
        (sid, limit),
        "all",
    )


def user_scores(uid):
    return run(
        "SELECT s.name, p.score, s.scored FROM predictions p JOIN stages s ON s.id=p.stage_id "
        "WHERE p.user_id=? ORDER BY s.id",
        (uid,),
        "all",
    )
