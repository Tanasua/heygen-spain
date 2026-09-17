"""
Точка входу для Workflow 2 ("Publish"), запускається кожні 15 хв.

Логіка:
1. Взяти всі записи зі статусом "translating" з processed.json.
2. Якщо запис "застряг" довше STALE_AFTER_HOURS — позначити failed і
   прибрати обкладинку (інакше висів би вічно, а presigned URL все одно згорить).
3. Перевірити статус job у HeyGen.
4. Якщо completed -> завантажити перекладене відео + обкладинку -> віддати
   пакет (файл+обкладинка+метадані) у Telegram для ручної публікації ->
   mark_delivered -> прибрати обкладинку.
5. Якщо failed -> mark_failed + Telegram + прибрати обкладинку.
6. Якщо pending/running -> нічого не робити, повернемось наступного разу.
"""

import os
import traceback
from datetime import datetime, timezone, timedelta

import state_manager
import heygen_client
import downloader
import telegram_notifier

HEYGEN_API_KEY = os.environ["HEYGEN_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

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


def handle_pending(video_id: str, record: dict) -> None:
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
            return

        status_info = heygen_client.check_job_status(HEYGEN_API_KEY, job_id)
        status = status_info["status"]

        if status in ("pending", "running"):
            print(f"[WAIT] {video_id} — статус HeyGen: {status}")
            return

        if status == "failed":
            reason = f"HeyGen job failed: {status_info.get('failure_message') or status_info.get('raw')}"
            state_manager.mark_failed(video_id, reason=reason)
            telegram_notifier.notify_error(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                            stage="heygen_translate", video_title=title, error=reason)
            _remove_thumbnail(record)
            return

        if status != "completed":
            print(f"[UNKNOWN STATUS] {video_id} — {status}, пропускаю до наступного разу")
            return

        if not status_info.get("url"):
            print(f"[WARN] {video_id} — статус completed, але video_url відсутній. Спробую наступного разу.")
            return

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

        # Готуємо файл під ліміт Telegram і віддаємо пакет для ручної публікації
        telegram_ready_path = downloader.ensure_telegram_size(translated_path, WORK_DIR, video_id)
        telegram_notifier.send_for_manual_publish(
            TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
            video_path=telegram_ready_path,
            thumbnail_path=local_thumbnail,
            title=record["es_title"],
            description=record["es_description"],
            tags=record.get("es_tags"),
        )
        if telegram_ready_path != translated_path and os.path.exists(telegram_ready_path):
            os.remove(telegram_ready_path)

        state_manager.mark_delivered(video_id)
        print(f"[DELIVERED] {video_id} -> Telegram")

        # Обкладинка вже надіслана — прибираємо з репо, щоб він не розпухав
        if local_thumbnail and os.path.exists(local_thumbnail):
            os.remove(local_thumbnail)
            print(f"[CLEANUP] Видалено {rel_path}")


    except Exception as e:
        traceback.print_exc()
        state_manager.mark_failed(video_id, reason=str(e))
        telegram_notifier.notify_error(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                        stage="publish_pending", video_title=title, error=str(e))
        # Обкладинку навмисно НЕ видаляємо: якщо помилка тимчасова
        # (квота YouTube, мережа), наступний запуск спробує ще раз і
        # обкладинка знадобиться. Прибереться при stale-таймауті.

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

    for video_id, record in pending.items():
        handle_pending(video_id, record)


if __name__ == "__main__":
    main()
