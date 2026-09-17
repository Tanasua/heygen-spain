"""
Точка входу для Workflow 1 ("Check & Process"), запускається кожні 30 хв.

Схема без платного сховища: відео роздається просто з раннера через
cloudflared quick tunnel, а job тримається живим, поки HeyGen не завантажить
файл повністю. Факт завантаження визначається точно — по відданих байтах
(див. tunnel_server.py), а не за таймером.

Порядок кроків підібраний так, щоб не витрачати гроші даремно: виклики
OpenAI (обкладинка — найдорожча операція) робляться ТІЛЬКИ після того, як
HeyGen успішно забрав відео. Якщо тунель не піднявся — платних викликів нема.

Обкладинка зберігається в репозиторії (data/thumbnails/), бо раннер Workflow 1
знищується, а публікація відбувається у Workflow 2.
"""

import os
import shutil
import sys
import traceback

import requests
from openai import OpenAI

import state_manager
import youtube_monitor
import downloader
import heygen_client
import tunnel_server
import cloudflared_tunnel
import dub_pipeline
import thumbnail_generator
import metadata_generator
import telegram_notifier

# --- Конфігурація з env (GitHub Secrets) ---
YT_API_KEY = os.environ["YOUTUBE_API_KEY"]
SOURCE_CHANNEL_HANDLE = os.environ.get("SOURCE_CHANNEL_HANDLE", "@GlavredTV")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# "inworld" (типово) — DIY-дубляж без ліп-синку (Whisper + GPT + Inworld TTS),
# синхронно, готовий пакет віддається у Telegram одразу тут. "heygen" (ліп-синк,
# ~$4/хв — надто дорого для щоденного продакшну) лишається ручним фолбеком:
# асинхронно, Workflow 2 віддає готовий пакет пізніше.
DUB_PROVIDER = os.environ.get("DUB_PROVIDER", "inworld")

HEYGEN_API_KEY = os.environ.get("HEYGEN_API_KEY")
INWORLD_API_KEY = os.environ.get("INWORLD_API_KEY")

if DUB_PROVIDER == "inworld":
    if not INWORLD_API_KEY:
        raise RuntimeError("DUB_PROVIDER=inworld вимагає секрет INWORLD_API_KEY")
elif not HEYGEN_API_KEY:
    raise RuntimeError("DUB_PROVIDER=heygen вимагає секрет HEYGEN_API_KEY")

# Скільки чекати, поки HeyGen завантажить файл через тунель.
FETCH_TIMEOUT_SECONDS = int(os.environ.get("FETCH_TIMEOUT_SECONDS", 45 * 60))
# Перекодування для зменшення файлу: 0 = вимкнено, інакше макс. висота (1080).
TRANSCODE_MAX_HEIGHT = int(os.environ.get("TRANSCODE_MAX_HEIGHT", 0))
# 0 = без обмеження. Корисно для контрольованого тесту (обробити 1 відео,
# а не всі нові одразу).
MAX_VIDEOS_PER_RUN = int(os.environ.get("MAX_VIDEOS_PER_RUN", 0))

BASE_DIR = os.path.join(os.path.dirname(__file__), "..")
WORK_DIR = os.path.join(BASE_DIR, "tmp")
THUMBNAILS_DIR = os.path.join(BASE_DIR, "data", "thumbnails")
BIN_DIR = os.path.join(BASE_DIR, "tmp", "bin")


def _cleanup(paths: list[str]) -> None:
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"[cleanup] Не вдалось видалити {path}: {e}")


def _serve_and_submit(video_path: str, video_id: str, title: str) -> str:
    """
    Піднімає тунель, віддає відео HeyGen, чекає завантаження.
    Повертає heygen_job_id. Кидає виняток, якщо щось не вдалось.
    """
    binary = cloudflared_tunnel.ensure_cloudflared(BIN_DIR)
    url_path = f"/{video_id}.mp4"

    with tunnel_server.FileServer(video_path, url_path=url_path) as server:
        with cloudflared_tunnel.QuickTunnel(server.port, binary) as tunnel:
            # Перевіряємо тунель ПЕРЕД зверненням до HeyGen — щоб не
            # витрачати кредити на непрацюючий лінк.
            public_url = tunnel.verify(url_path, expected_size=server.file_size)

            job_id = heygen_client.submit_translation_job(
                HEYGEN_API_KEY, video_url=public_url, target_language="es",
                title=f"{title[:80]} [ES]",
            )
            print(f"[HEYGEN] Job створено: {job_id}")

            downloaded = server.wait_until_downloaded(timeout_seconds=FETCH_TIMEOUT_SECONDS)
            if not downloaded:
                raise RuntimeError(
                    f"HeyGen не завантажив відео за {FETCH_TIMEOUT_SECONDS // 60} хв "
                    f"(віддано {server.tracker.progress_percent():.1f}%). "
                    f"Job {job_id} створено, але джерело вже недоступне."
                )

            print("[HEYGEN] Відео забрано повністю — тунель можна гасити")
            return job_id


def _generate_metadata_and_thumbnail(openai_client: OpenAI, video: dict, title: str,
                                      thumb_ref_path: str, thumbnail_path: str) -> dict:
    metadata = metadata_generator.generate_spanish_metadata(openai_client, title)
    print(f"[OPENAI] Title: {metadata['title']}")

    r = requests.get(video["thumbnail_url"], timeout=30)
    r.raise_for_status()
    with open(thumb_ref_path, "wb") as f:
        f.write(r.content)

    thumbnail_generator.generate_thumbnail(
        openai_client,
        reference_image_path=thumb_ref_path,
        headline=metadata["thumbnail_headline"],
        scene_hint=title,
        output_path=thumbnail_path,
    )
    print(f"[THUMBNAIL] {thumbnail_path}")
    return metadata


def _process_with_heygen(video: dict, openai_client: OpenAI, video_id: str, title: str,
                          duration: int, video_path: str, thumb_ref_path: str,
                          thumbnail_path: str) -> None:
    serve_path = video_path
    if TRANSCODE_MAX_HEIGHT > 0:
        serve_path = os.path.join(WORK_DIR, f"{video_id}_small.mp4")
        downloader.transcode_smaller(video_path, serve_path, max_height=TRANSCODE_MAX_HEIGHT)

    try:
        # Тунель -> HeyGen -> чекаємо, поки заберуть файл
        heygen_job_id = _serve_and_submit(serve_path, video_id, title)

        # Відео більше не потрібне — звільняємо диск раннера до викликів OpenAI
        _cleanup([video_path, serve_path if serve_path != video_path else None])

        metadata = _generate_metadata_and_thumbnail(openai_client, video, title,
                                                      thumb_ref_path, thumbnail_path)

        # Фіксуємо стан (шлях відносно кореня репо — Workflow 2 його закомітить)
        state_manager.mark_translating(
            video_id, title, duration, heygen_job_id,
            es_title=metadata["title"],
            es_description=metadata["description"],
            es_tags=metadata.get("tags"),
            thumbnail_path=os.path.relpath(thumbnail_path, BASE_DIR),
        )
        telegram_notifier.notify_translating(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, title)
    finally:
        _cleanup([serve_path if serve_path != video_path else None])


def _process_with_inworld(video: dict, openai_client: OpenAI, video_id: str, title: str,
                           duration: int, video_path: str, thumb_ref_path: str,
                           thumbnail_path: str) -> None:
    """DIY-дубляж без ліп-синку — усе синхронно в межах Workflow 1, пакет
    (файл + обкладинка + метадані) одразу віддається в Telegram для ручної
    публікації."""
    dubbed_path = None
    telegram_ready_path = None
    try:
        dubbed_path = dub_pipeline.translate_video(
            openai_client, INWORLD_API_KEY, video_path, WORK_DIR, video_id,
            target_language="es",
        )
        print(f"[DUB] Готово: {dubbed_path} "
              f"({os.path.getsize(dubbed_path) / 1024 / 1024:.1f} MB)")

        metadata = _generate_metadata_and_thumbnail(openai_client, video, title,
                                                      thumb_ref_path, thumbnail_path)

        telegram_ready_path = downloader.ensure_telegram_size(dubbed_path, WORK_DIR, video_id)
        telegram_notifier.send_for_manual_publish(
            TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
            video_path=telegram_ready_path,
            thumbnail_path=thumbnail_path,
            title=metadata["title"],
            description=metadata["description"],
            tags=metadata.get("tags"),
        )

        state_manager.mark_translating(
            video_id, title, duration, heygen_job_id="inworld-dub",
            es_title=metadata["title"],
            es_description=metadata["description"],
            es_tags=metadata.get("tags"),
            thumbnail_path=os.path.relpath(thumbnail_path, BASE_DIR),
        )
        state_manager.mark_delivered(video_id)
        print(f"[DELIVERED] {video_id} -> Telegram")

        if os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
    finally:
        _cleanup([video_path, dubbed_path,
                  telegram_ready_path if telegram_ready_path != dubbed_path else None])


def process_video(video: dict, openai_client: OpenAI) -> bool:
    """Повертає False, якщо відео впало — main() використовує це, щоб
    завершити скрипт ненульовим кодом і зупинити плановий крон до ручного
    втручання (інакше помилка мовчки повторювалась би щопівгодини)."""
    video_id = video["video_id"]
    title = video["title"]
    duration = video["duration_seconds"]

    print(f"[PROCESS] {video_id} — {title} (DUB_PROVIDER={DUB_PROVIDER})")
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    video_path = None
    thumb_ref_path = os.path.join(WORK_DIR, f"{video_id}_ref.jpg")
    thumbnail_path = os.path.join(THUMBNAILS_DIR, f"{video_id}.jpg")

    try:
        video_path = downloader.download_video(video["url"], WORK_DIR, video_id)
        size_mb = os.path.getsize(video_path) / 1024 / 1024
        print(f"[DOWNLOADED] {video_path} ({size_mb:.1f} MB)")

        if DUB_PROVIDER == "inworld":
            _process_with_inworld(video, openai_client, video_id, title, duration,
                                   video_path, thumb_ref_path, thumbnail_path)
        else:
            _process_with_heygen(video, openai_client, video_id, title, duration,
                                  video_path, thumb_ref_path, thumbnail_path)
        return True

    except Exception as e:
        traceback.print_exc()
        state_manager.mark_failed(video_id, reason=str(e))
        telegram_notifier.notify_error(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                        stage="process_new_video", video_title=title, error=str(e))
        _cleanup([thumbnail_path])
        return False

    finally:
        _cleanup([video_path, thumb_ref_path])


def main():
    videos = youtube_monitor.get_latest_videos(
        YT_API_KEY, handle=SOURCE_CHANNEL_HANDLE, max_results=5
    )

    new_videos = [v for v in videos if not state_manager.is_known(v["video_id"])]

    if not new_videos:
        print("Нових відео немає.")
        return

    openai_client = OpenAI(api_key=OPENAI_API_KEY)

    # Від найстарішого до найновішого — щоб публікації йшли в природному порядку
    ordered = list(reversed(new_videos))
    if MAX_VIDEOS_PER_RUN > 0:
        ordered = ordered[:MAX_VIDEOS_PER_RUN]
        print(f"[LIMIT] MAX_VIDEOS_PER_RUN={MAX_VIDEOS_PER_RUN} — обробляю лише {len(ordered)}")

    any_failed = False
    for video in ordered:
        if not process_video(video, openai_client):
            any_failed = True

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
