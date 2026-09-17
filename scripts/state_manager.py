"""
Керування станом оброблених відео у data/processed.json.

Структура запису:
{
  "video_id": {
    "status": "skipped" | "translating" | "delivered" | "failed",
    "reason": "...",              # для skipped/failed
    "title_original": "...",
    "duration_seconds": 123,
    "heygen_job_id": "...",       # для translating/delivered
    "es_title": "...",
    "es_description": "...",
    "es_tags": ["...", "..."],
    "thumbnail_path": "...",      # обкладинка в репо (data/thumbnails/<id>.jpg)
    "created_at": "ISO timestamp",
    "updated_at": "ISO timestamp"
  }
}

Пайплайн більше не заливає готове відео на YouTube сам — фінальний пакет
(файл + обкладинка + заголовок/опис/теги) віддається в Telegram-чат для
ручної публікації (див. telegram_notifier.send_for_manual_publish).
Статус "delivered" означає саме це, а не "опубліковано на YouTube".
"""

import json
import os
from datetime import datetime, timezone

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return {}
        return json.loads(content)


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_known(video_id: str) -> bool:
    return video_id in load_state()


def mark_skipped(video_id: str, title: str, duration_seconds: int, reason: str) -> None:
    state = load_state()
    state[video_id] = {
        "status": "skipped",
        "reason": reason,
        "title_original": title,
        "duration_seconds": duration_seconds,
        "created_at": _now(),
        "updated_at": _now(),
    }
    save_state(state)


def mark_translating(video_id: str, title: str, duration_seconds: int,
                      heygen_job_id: str, es_title: str, es_description: str,
                      thumbnail_path: str, es_tags: list[str] | None = None) -> None:
    state = load_state()
    state[video_id] = {
        "status": "translating",
        "title_original": title,
        "duration_seconds": duration_seconds,
        "heygen_job_id": heygen_job_id,
        "es_title": es_title,
        "es_description": es_description,
        "es_tags": es_tags or [],
        "thumbnail_path": thumbnail_path,
        "created_at": _now(),
        "updated_at": _now(),
    }
    save_state(state)


def mark_delivered(video_id: str) -> None:
    """Готовий пакет (файл+обкладинка+метадані) віддано в Telegram для ручної публікації."""
    state = load_state()
    if video_id in state:
        state[video_id]["status"] = "delivered"
        state[video_id]["updated_at"] = _now()
        save_state(state)


def mark_failed(video_id: str, reason: str) -> None:
    state = load_state()
    if video_id in state:
        state[video_id]["status"] = "failed"
        state[video_id]["reason"] = reason
        state[video_id]["updated_at"] = _now()
    else:
        state[video_id] = {
            "status": "failed",
            "reason": reason,
            "created_at": _now(),
            "updated_at": _now(),
        }
    save_state(state)


def get_pending_translations() -> dict:
    """Повертає {video_id: record} зі статусом 'translating'."""
    state = load_state()
    return {vid: rec for vid, rec in state.items() if rec.get("status") == "translating"}
