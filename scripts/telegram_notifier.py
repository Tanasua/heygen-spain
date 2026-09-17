"""
Надсилання сповіщень власнику через Telegram Bot API.
"""

import os

import requests

# Реальний ліміт Telegram Bot API на файл, надісланий ботом (без local
# Bot API server) — 50 MB. Лишаємо запас.
TELEGRAM_FILE_LIMIT_MB = 49


def send_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        # Навмисно не кидаємо помилку далі — збій сповіщення не має
        # ламати основний пайплайн.
        print(f"[telegram_notifier] Не вдалось надіслати повідомлення: {e}")


def _send_file(bot_token: str, chat_id: str, method: str, field_name: str,
               file_path: str, caption: str | None = None) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        data["parse_mode"] = "HTML"
    with open(file_path, "rb") as f:
        resp = requests.post(url, data=data, files={field_name: f}, timeout=300)
    resp.raise_for_status()


def notify_translating(bot_token: str, chat_id: str, title: str) -> None:
    text = f"🌐 <b>Відправлено на переклад</b>\n\n{title}"
    send_message(bot_token, chat_id, text)


def send_for_manual_publish(bot_token: str, chat_id: str, video_path: str,
                             thumbnail_path: str | None, title: str,
                             description: str, tags: list[str] | None = None) -> None:
    """
    Замість автозаливки на YouTube — віддає готовий пакет у Telegram-чат
    для ручної публікації: обкладинка, заголовок+опис+теги окремим
    повідомленням, і сам файл відео.

    Кидає виняток, якщо файл перевищує ліміт Telegram Bot API (50 MB) —
    виклик має підготувати файл потрібного розміру заздалегідь
    (див. downloader.ensure_telegram_size).
    """
    size_mb = os.path.getsize(video_path) / 1024 / 1024
    if size_mb > TELEGRAM_FILE_LIMIT_MB:
        raise RuntimeError(
            f"Файл {size_mb:.1f} MB перевищує ліміт Telegram Bot API "
            f"({TELEGRAM_FILE_LIMIT_MB} MB) — надіслати не можу"
        )

    if thumbnail_path and os.path.exists(thumbnail_path):
        _send_file(bot_token, chat_id, "sendPhoto", "photo", thumbnail_path, caption=title)

    tags = tags or []
    tags_line = " ".join(f"#{t.lstrip('#').replace(' ', '_')}" for t in tags if t.strip())
    text = f"🎬 <b>Готово до ручної публікації</b>\n\n<b>{title}</b>\n\n{description}"
    if tags_line:
        text += f"\n\n<b>Теги:</b> {tags_line}"
    send_message(bot_token, chat_id, text)

    _send_file(bot_token, chat_id, "sendDocument", "document", video_path, caption=title)


def notify_error(bot_token: str, chat_id: str, stage: str, video_title: str, error: str) -> None:
    text = f"❌ <b>Помилка на етапі: {stage}</b>\n\nВідео: {video_title}\n\n{error[:500]}"
    send_message(bot_token, chat_id, text)
