"""
Точка входу для Workflow 2 ("Publish"), запускається кожні 15 хв.

Логіка:
1. Взяти всі записи зі статусом "translating" з processed.json.
2. Якщо запис "застряг" довше STALE_AFTER_HOURS — позначити failed і
   прибрати R2 (інакше висів би вічно, а presigned URL все одно згорить).
3. Перевірити статус job у HeyGen.
4. Якщо completed -> завантажити перекладене відео + обкладинку з R2 ->
   залити на іспанський канал -> mark_published + Telegram -> прибрати R2.
5. Якщо failed -> mark_failed + Telegram + прибрати R2.
6. Якщо pending/running -> нічого не робити, повернемось наступного разу.
"""

import os
import sys
import traceback
from datetime import datetime, timezone, timedelta

import state_manager
import heygen_client
import youtube_uploader
import telegram_notifier

HEYGEN_API_KEY = os.environ["HEYGEN_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

YT_CLIENT_ID = os.environ["YOUTUBE_ES_CLIENT_ID"]
YT_CLIENT_SECRET = os.environ["YOUTUBE_ES_CLIENT_SECRET"]
YT_REFRESH_TOKEN = os.environ["YOUTUBE_ES_REFRESH_TOKEN"]

BASE_DIR = os.path.join(os.path.dirname(__file__), "..")
WORK_DIR = os.path.join(BASE_DIR, "tmp")

# Запобіжник від вічно зависших job. Presigned URL живе 6 діб, тож
# тримати запис довше сенсу немає.
STALE_AFTER_HOURS = 120


def _is_stale(record: dict) -> bool:
    created_at = record.get("created_at")
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > timedelta(hours=STALE_AFTER_HOURS)


def _remove_thumbnail(record: dict) -> None:
    """Прибирає осиротілу обкладинку з репо, щоб не накопичувалась."""
    rel_path = record.get("thumbnail_path")
    if not rel_path:
        return
    path = os.path.join(BASE_DIR, rel_path)
    if os.path.exists(path):
        try:
            os.remove(path)
            print(f"[CLEANUP] Видалено {rel_path}")
        except Exception as e:
            print(f"[CLEANUP] Не вдалось видалити {rel_path}: {e}")


def handle_pending(video_id: str, record: dict) -> bool:
    """Повертає False, якщо запис впав — main() використовує це, щоб
    завершити скрипт ненульовим кодом і зупинити плановий крон до ручного
    втручання."""
    job_id = record["heygen_job_id"]
    title = record["title_original"]

    translated_path = os.path.join(WORK_DIR, f"{video_id}_es.mp4")

    try:
        if _is_stale(record):
            reason = f"HeyGen job не завершився за {STALE_AFTER_HOURS} год — позначено як failed"
            state_manager.mark_failed(video_id, reason=reason)
            telegram_notifier.notify_error(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                            stage="heygen_timeout", video_title=title, error=reason)
            _remove_thumbnail(record)
            return False

        status_info = heygen_client.check_job_status(HEYGEN_API_KEY, job_id)
        status = status_info["status"]

        if status in ("pending", "running"):
            print(f"[WAIT] {video_id} — статус HeyGen: {status}")
            return True

        if status == "failed":
            reason = f"HeyGen job failed: {status_info.get('failure_message') or status_info.get('raw')}"
            state_manager.mark_failed(video_id, reason=reason)
            telegram_notifier.notify_error(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                            stage="heygen_translate", video_title=title, error=reason)
            _remove_thumbnail(record)
            return False

        if status != "completed":
            print(f"[UNKNOWN STATUS] {video_id} — {status}, пропускаю до наступного разу")
            return True

        if not status_info.get("url"):
            print(f"[WARN] {video_id} — статус completed, але video_url відсутній. Спробую наступного разу.")
            return True

        # Готово -> завантажуємо перекладене відео
        os.makedirs(WORK_DIR, exist_ok=True)
        heygen_client.download_translated_video(status_info["url"], translated_path)
        print(f"[DOWNLOADED] {translated_path} ({os.path.getsize(translated_path) / 1024 / 1024:.1f} MB)")

        # Обкладинка з репозиторію (згенерована у Workflow 1 і закомічена)
        local_thumbnail = None
        rel_path = record.get("thumbnail_path")
        if rel_path:
            candidate = os.path.join(BASE_DIR, rel_path)
            if os.path.exists(candidate):
                local_thumbnail = candidate
            else:
                # Без обкладинки публікуємо все одно — YouTube візьме автокадр.
                print(f"[WARN] Обкладинку не знайдено: {candidate}")

        # Заливаємо на іспанський канал
        es_video_id = youtube_uploader.upload_video(
            YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN,
            video_path=translated_path,
            title=record["es_title"],
            description=record["es_description"],
            thumbnail_path=local_thumbnail,
        )

        state_manager.mark_published(video_id, es_video_id)
        telegram_notifier.notify_published(
            TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
            record["es_title"], f"https://www.youtube.com/watch?v={es_video_id}"
        )
        print(f"[PUBLISHED] {video_id} -> {es_video_id}")

        # Обкладинка вже на YouTube — прибираємо з репо, щоб він не розпухав
        if local_thumbnail and os.path.exists(local_thumbnail):
            os.remove(local_thumbnail)
            print(f"[CLEANUP] Видалено {rel_path}")

        return True

    except Exception as e:
        traceback.print_exc()
        state_manager.mark_failed(video_id, reason=str(e))
        telegram_notifier.notify_error(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                        stage="publish_pending", video_title=title, error=str(e))
        # Обкладинку навмисно НЕ видаляємо: якщо помилка тимчасова
        # (квота YouTube, мережа), наступний запуск спробує ще раз і
        # обкладинка знадобиться. Прибереться при stale-таймауті.
        return False

    finally:
        for path in (translated_path,):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    print(f"[cleanup] Не вдалось видалити {path}: {e}")


def main():
    pending = state_manager.get_pending_translations()
    if not pending:
        print("Немає відео в очікуванні публікації.")
        return

    any_failed = False
    for video_id, record in pending.items():
        if not handle_pending(video_id, record):
            any_failed = True

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
