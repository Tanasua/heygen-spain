"""
Клієнт для HeyGen Video Translation API v3 (звірено з офіційною документацією:
https://docs.heygen.com/ -> Video Translation - Precision).

Режим: "precision" — використовує avatar inference для якісного ліп-синку
(рекомендовано HeyGen саме для talking-head відео з активною мімікою — це
наш кейс). Повільніше і дорожче за "speed", але саме "precision" дає
реалістичний ліп-синк, який і був у вимозі власника каналу.

Якщо витрати виявляться завеликими — можна перемкнути DEFAULT_MODE на "speed"
(швидше, дешевше, гірший ліп-синк) без зміни решти коду.
"""

import requests

BASE_URL = "https://api.heygen.com/v3"
DEFAULT_MODE = "precision"

# HeyGen приймає повні назви мов (наприклад "Spanish"), а не ISO-коди.
LANGUAGE_NAMES = {
    "es": "Spanish",
}


class HeyGenError(RuntimeError):
    pass


def submit_translation_job(api_key: str, video_url: str, target_language: str = "es",
                            mode: str = DEFAULT_MODE, title: str = "auto-dub -> ES") -> str:
    """
    Надсилає відео на переклад. video_url має бути публічно доступним
    (HeyGen сам його завантажить — перевірте, що лінк відкривається в
    інкогніто-вкладці браузера).

    Повертає video_translation_id для подальшого опитування статусу.
    """
    language_name = LANGUAGE_NAMES.get(target_language, target_language)

    url = f"{BASE_URL}/video-translations"
    headers = {
        "accept": "application/json",
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "video": {"type": "url", "url": video_url},
        "output_languages": [language_name],
        "mode": mode,
        "title": title,
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    if resp.status_code >= 400:
        raise HeyGenError(f"HeyGen submit job failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    ids = data.get("data", {}).get("video_translation_ids", [])
    if not ids:
        raise HeyGenError(f"Не вдалось отримати video_translation_id з відповіді: {data}")

    return ids[0]


def check_job_status(api_key: str, video_translation_id: str) -> dict:
    """
    Повертає {"status": "pending"|"running"|"completed"|"failed", "url": "..." | None}

    Статуси за документацією HeyGen:
    - pending   -> у черзі
    - running   -> йде avatar inference
    - completed -> готово, video_url доступний
    - failed    -> дивитись failure_message
    """
    url = f"{BASE_URL}/video-translations/{video_translation_id}"
    headers = {"accept": "application/json", "x-api-key": api_key}

    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise HeyGenError(f"HeyGen status check failed ({resp.status_code}): {resp.text}")

    data = resp.json().get("data", {})
    status = data.get("status", "unknown")
    video_url = data.get("video_url")
    failure_message = data.get("failure_message")

    return {
        "status": status,
        "url": video_url,
        "failure_message": failure_message,
        "raw": data,
    }


def download_translated_video(video_url: str, output_path: str) -> str:
    resp = requests.get(video_url, stream=True, timeout=300)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return output_path
