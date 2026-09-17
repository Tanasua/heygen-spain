"""
Завантаження відео у максимально можливій якості.

Два незалежні шляхи, в такому порядку:
1. Apify actor "epctex/youtube-video-downloader" (APIFY_API_TOKEN) — знімає з
   рук нас проблему з YouTube anti-bot (запит іде з інфраструктури Apify, не
   з датацентр-IP GitHub Actions), і не залежить від протухання cookies.
   Free-план Apify дає $5/міс кредитів, але в документації актора згадано
   окреме обмеження — можливо лише 2 запуски/міс на Free. Якщо впреться в
   ліміт чи інша помилка — падаємо на запасний варіант нижче.
2. yt-dlp + cookies + проксі (як було раніше) — запасний варіант, якщо Apify
   не налаштовано (немає токена) або впав з помилкою.
"""

import os
import subprocess

import requests

APIFY_TOKEN_ENV_VAR = "APIFY_API_TOKEN"
APIFY_ACTOR_ID = "epctex~youtube-video-downloader"
APIFY_QUALITY = "1080"

# YouTube блокує завантаження з датацентр-IP (GitHub Actions) як "підозрілі"
# ("Sign in to confirm you're not a bot") — незалежно від того, наскільки
# свіжі cookies. Два незалежні захисти комбінуються:
# 1. Cookies залогіненого акаунта (--cookies). Вміст cookies.txt (формат
#    Netscape) — у GitHub Secret YOUTUBE_COOKIES. Час від часу протухають
#    (Google ротує), тоді потрібен новий експорт.
# 2. Резидентний/mobile проксі (--proxy) — щоб запит взагалі не йшов з
#    датацентр-IP GitHub Actions. Формат: http://user:pass@host:port або
#    socks5://user:pass@host:port. GitHub Secret YT_DLP_PROXY_URL.
#    Без нього cookies самі по собі не завжди рятують (2026 дані).
COOKIES_ENV_VAR = "YOUTUBE_COOKIES"
COOKIES_PATH = "/tmp/yt_cookies.txt"
PROXY_ENV_VAR = "YT_DLP_PROXY_URL"


def _download_via_apify(video_url: str, output_dir: str, video_id: str) -> str:
    """Тягне відео через Apify actor. Кидає виняток при будь-якій помилці —
    виклик (download_video) сам вирішує, чи падати на yt-dlp."""
    token = os.environ[APIFY_TOKEN_ENV_VAR]
    run_url = f"https://api.apify.com/v2/actors/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"

    resp = requests.post(
        run_url,
        headers={"Authorization": f"Bearer {token}"},
        json={"startUrls": [video_url], "quality": APIFY_QUALITY, "storageType": "apify"},
        timeout=600,
    )
    resp.raise_for_status()
    items = resp.json()
    if not items:
        raise RuntimeError("Apify actor не повернув жодного результату")

    item = items[0]
    if item.get("status") != "succeeded":
        raise RuntimeError(f"Apify actor повернув статус {item.get('status')!r}: {item}")

    file_url = (item.get("output") or {}).get("url")
    if not file_url:
        raise RuntimeError(f"Apify actor не повернув output.url: {item}")

    output_path = os.path.join(output_dir, f"{video_id}.mp4")
    with requests.get(file_url, stream=True, timeout=600) as file_resp:
        file_resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in file_resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)

    print(f"[apify] Завантажено через Apify actor (якість {APIFY_QUALITY}p, "
          f"вартість ${item.get('totalCost', '?')})")
    return output_path


def _prepare_cookies_file() -> str | None:
    """Пише cookies з env у тимчасовий файл. Повертає шлях або None, якщо секрет не заданий."""
    cookies_content = os.environ.get(COOKIES_ENV_VAR)
    if not cookies_content:
        return None
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(cookies_content)
    return COOKIES_PATH


def _download_via_ytdlp(video_url: str, output_dir: str, video_id: str) -> str:
    """
    Завантажує відео через yt-dlp та повертає шлях до файлу.
    Формат: найкраща доступна відео+аудіо доріжка, злиті в mp4.
    """
    output_template = os.path.join(output_dir, f"{video_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
    ]

    cookies_path = _prepare_cookies_file()
    if cookies_path:
        cmd += ["--cookies", cookies_path]

    proxy_url = os.environ.get(PROXY_ENV_VAR)
    if proxy_url:
        cmd += ["--proxy", proxy_url]

    cmd.append(video_url)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp помилка для {video_url}:\n{result.stderr}")

    expected_path = os.path.join(output_dir, f"{video_id}.mp4")
    if not os.path.exists(expected_path):
        # На випадок іншого розширення після merge
        for fname in os.listdir(output_dir):
            if fname.startswith(video_id):
                return os.path.join(output_dir, fname)
        raise RuntimeError(f"Файл не знайдено після завантаження: {expected_path}")

    return expected_path


def download_video(video_url: str, output_dir: str, video_id: str) -> str:
    """Завантажує відео. Спершу Apify (якщо є токен), при помилці — yt-dlp."""
    os.makedirs(output_dir, exist_ok=True)

    if os.environ.get(APIFY_TOKEN_ENV_VAR):
        try:
            return _download_via_apify(video_url, output_dir, video_id)
        except Exception as e:
            print(f"[apify] Не вдалось завантажити через Apify, падаю на yt-dlp: {e}")

    return _download_via_ytdlp(video_url, output_dir, video_id)


def transcode_smaller(input_path: str, output_path: str, max_height: int = 1080,
                       crf: int = 21) -> str:
    """
    Перекодовує відео у менший файл (H.264 + AAC).

    Навіщо: quick tunnel не має гарантованої пропускної здатності, тож чим
    менший файл — тим коротша й надійніша передача. На вихід HeyGen усе одно
    віддає 1080p, тож тримати вихідні 20+ Мбіт/с сенсу мало.

    CRF 21 при 1080p — візуально майже без втрат; для ліп-синку в precision
    mode обличчя лишається чітким. Якщо помітите деградацію якості обличчя,
    знижуйте crf (менше значення = вища якість, більший файл).
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", f"scale=-2:'min({max_height},ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg помилка перекодування:\n{result.stderr[-2000:]}")

    before = os.path.getsize(input_path) / 1024 / 1024
    after = os.path.getsize(output_path) / 1024 / 1024
    print(f"[transcode] {before:.1f} MB -> {after:.1f} MB")
    return output_path


# Пайплайн віддає готове відео в Telegram (а не заливає на YouTube сам),
# а Telegram Bot API приймає від бота файли не більші за 50 MB.
TELEGRAM_LIMIT_MB = 49


def ensure_telegram_size(video_path: str, work_dir: str, video_id: str) -> str:
    """
    Якщо файл більший за ліміт Telegram — перекодовує в менший (720p).
    Повертає шлях до файлу, який реально треба надсилати (може співпадати
    зі вхідним, якщо він і так влазить).
    """
    size_mb = os.path.getsize(video_path) / 1024 / 1024
    if size_mb <= TELEGRAM_LIMIT_MB:
        return video_path

    print(f"[telegram] Файл {size_mb:.1f} MB > {TELEGRAM_LIMIT_MB} MB ліміту Telegram — перекодовую менше")
    smaller_path = os.path.join(work_dir, f"{video_id}_tg.mp4")
    transcode_smaller(video_path, smaller_path, max_height=720, crf=26)

    new_size_mb = os.path.getsize(smaller_path) / 1024 / 1024
    if new_size_mb > TELEGRAM_LIMIT_MB:
        raise RuntimeError(
            f"Навіть після перекодування файл {new_size_mb:.1f} MB "
            f"перевищує ліміт Telegram {TELEGRAM_LIMIT_MB} MB"
        )
    return smaller_path
