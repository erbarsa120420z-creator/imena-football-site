from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import os
import sqlite3

from flask import Flask, jsonify, request, send_from_directory
from scraper import load_cache, refresh_data

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("IMENA_DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("IMENA_DB_PATH", str(DATA_DIR / "imena.sqlite3")))
app = Flask(__name__, static_folder=str(ROOT), static_url_path="")

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS player_progress (
            player TEXT PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            series INTEGER NOT NULL DEFAULT 0,
            goals INTEGER NOT NULL DEFAULT 0,
            best INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS game_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player TEXT NOT NULL,
            goals INTEGER NOT NULL,
            xp INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_game_scores_created ON game_scores(created_at DESC)")
init_db()

@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")

@app.get("/api/data")
def api_data():
    data = load_cache()
    stale = True
    if data.get("updated_at"):
        try:
            stamp = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
            stale = datetime.now(timezone.utc) - stamp > timedelta(minutes=30)
        except Exception:
            stale = True
    if not data.get("players") or not data.get("matches") or stale:
        try:
            data = refresh_data()
        except Exception as exc:
            cached = load_cache()
            if cached.get("players") or cached.get("matches"):
                cached["last_error"] = str(exc)
                return jsonify(cached)
            return jsonify({"error": str(exc), **cached}), 503
    return jsonify(data)

@app.post("/api/refresh")
def api_refresh():
    try:
        return jsonify(refresh_data())
    except Exception as exc:
        return jsonify({"error": str(exc), **load_cache()}), 502

@app.get("/api/game-data")
def api_game_data():
    with db() as conn:
        progress = {r["player"]: {k: r[k] for k in ("xp", "series", "goals", "best")} for r in conn.execute("SELECT * FROM player_progress")}
        scores = [dict(r) for r in conn.execute("SELECT player, goals, xp, rating, created_at AS date FROM game_scores ORDER BY rating DESC, goals DESC, created_at DESC LIMIT 50")]
    return jsonify({"progress": progress, "scores": scores})

@app.post("/api/game/series")
def api_game_series():
    payload = request.get_json(silent=True) or {}
    player = str(payload.get("player", "")).strip()
    if not player or len(player) > 120:
        return jsonify({"error": "Не указано имя игрока"}), 400
    try:
        goals = int(payload.get("goals", -1))
        xp = int(payload.get("xp", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректные результаты"}), 400
    if not 0 <= goals <= 5 or not 0 <= xp <= 100:
        return jsonify({"error": "Результат вне допустимого диапазона"}), 400
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("""INSERT INTO player_progress(player,xp,series,goals,best,updated_at)
            VALUES(?,?,1,?,?,?)
            ON CONFLICT(player) DO UPDATE SET
              xp=player_progress.xp+excluded.xp,
              series=player_progress.series+1,
              goals=player_progress.goals+excluded.goals,
              best=MAX(player_progress.best,excluded.best),
              updated_at=excluded.updated_at""", (player, xp, goals, goals, now))
        row = conn.execute("SELECT xp FROM player_progress WHERE player=?", (player,)).fetchone()
        rating = min(99, 60 + int(row["xp"]) // 25)
        conn.execute("INSERT INTO game_scores(player,goals,xp,rating,created_at) VALUES(?,?,?,?,?)",
                     (player, goals, xp, rating, now))
        conn.execute("""DELETE FROM game_scores WHERE id NOT IN
                     (SELECT id FROM game_scores ORDER BY rating DESC, goals DESC, created_at DESC LIMIT 100)""")
    return jsonify({"ok": True, **api_game_data().get_json()})

@app.get("/api/health")

@app.post("/telegram-webhook")
def telegram_webhook():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return jsonify({"error": "Telegram token is not configured"}), 500

    update = request.get_json(silent=True) or {}
    message = update.get("message", {})
    chat = message.get("chat", {})
    text_message = message.get("text", "")

    if text_message.startswith("/start") and chat.get("id"):
        import requests

        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "⚽ Открыть сайт команды",
                    "web_app": {
                        "url": "https://imena-football-site.onrender.com"
                    }
                }
            ]]
        }

        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat["id"],
                    "text": (
                        "⚽ Добро пожаловать в футбольную команду «Имена»!\n\n"
                        "Открывай сайт, смотри игроков, матчи, таблицу и статистику."
                    ),
                    "reply_markup": keyboard
                },
                timeout=10
            ).raise_for_status()
        except requests.RequestException:
            app.logger.exception("Не удалось отправить сообщение Telegram")
            return jsonify({"error": "Telegram message failed"}), 502

    return jsonify({"ok": True})

def health():
    with db() as conn:
        conn.execute("SELECT 1")
    return jsonify({"ok": True, "database": "sqlite", "persistent_path": str(DB_PATH)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
