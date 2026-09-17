"""
Завантаження відео у максимально можливій якості через yt-dlp.
"""

import os
import subprocess

# YouTube блокує завантаження з датацентр-IP (GitHub Actions) як "підозрілі".
# Обхід — передавати реальні cookies залогіненого акаунта через --cookies.
# Вміст файлу cookies.txt (формат Netscape) кладеться в GitHub Secret YOUTUBE_COOKIES.
COOKIES_ENV_VAR = "YOUTUBE_COOKIES"
COOKIES_PATH = "/tmp/yt_cookies.txt"

# Резидентний/ISP-проксі — без нього навіть свіжі cookies не завжди рятують
# від "Sign in to confirm you're not a bot" з датацентр-IP GitHub Actions.
# Формат: http://user:pass@host:port або socks5://user:pass@host:port.
PROXY_ENV_VAR = "YT_DLP_PROXY_URL"


def _prepare_cookies_file() -> str | None:
    """Пише cookies з env у тимчасовий файл. Повертає шлях або None, якщо секрет не заданий."""
    cookies_content = os.environ.get(COOKIES_ENV_VAR)
    if not cookies_content:
        return None
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(cookies_content)
    return COOKIES_PATH


def download_video(video_url: str, output_dir: str, video_id: str) -> str:
    """
    Завантажує відео та повертає шлях до файлу.
    Формат: найкраща доступна відео+аудіо доріжка, злиті в mp4.
    """
    os.makedirs(output_dir, exist_ok=True)
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
