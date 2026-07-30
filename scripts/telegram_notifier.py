"""
Надсилання сповіщень власнику через Telegram Bot API.
"""

import requests


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


def notify_skipped(bot_token: str, chat_id: str, title: str, url: str, duration_seconds: int) -> None:
    minutes = duration_seconds // 60
    seconds = duration_seconds % 60
    text = (
        f"⏭️ <b>Відео пропущено</b> (задовге для HeyGen: {minutes}:{seconds:02d})\n\n"
        f"{title}\n{url}\n\n"
        f"Якщо треба обробити вручну — дайте знати."
    )
    send_message(bot_token, chat_id, text)


def notify_translating(bot_token: str, chat_id: str, title: str) -> None:
    text = f"🌐 <b>Відправлено на переклад</b>\n\n{title}"
    send_message(bot_token, chat_id, text)


def notify_published(bot_token: str, chat_id: str, es_title: str, es_video_url: str) -> None:
    text = f"✅ <b>Опубліковано на іспанському каналі!</b>\n\n{es_title}\n{es_video_url}"
    send_message(bot_token, chat_id, text)


def notify_error(bot_token: str, chat_id: str, stage: str, video_title: str, error: str) -> None:
    text = f"❌ <b>Помилка на етапі: {stage}</b>\n\nВідео: {video_title}\n\n{error[:500]}"
    send_message(bot_token, chat_id, text)
