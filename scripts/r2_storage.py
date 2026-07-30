"""
Проміжне сховище Cloudflare R2 — потрібне тому, що HeyGen Video Translation API
приймає лише публічно доступне посилання на відео ("The URL must be publicly
accessible — test by opening in an incognito browser"), а файл на диску
GitHub Actions раннера ззовні недоступний.

R2 має S3-сумісний API, тож працюємо через boto3.

Використовується для двох речей:
1. Відео -> presigned URL -> передається в HeyGen.
2. Обкладинка -> зберігається між Workflow 1 і Workflow 2 (раннери
   ефемерні, тимчасова папка не переживає завершення job).

Після публікації обидва об'єкти видаляються, щоб не накопичувати сховище.

ПРИМІТКА щодо тарифів: у R2 немає плати за вихідний трафік (egress), що для
цього сценарію головне — HeyGen буде тягнути відео з R2. Безкоштовний tier
покриває певний обсяг сховища й операцій; актуальні цифри звіряйте на
https://developers.cloudflare.com/r2/pricing/ — я не маю мережевого доступу,
щоб підтвердити поточні лімiти.
"""

import os

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config

# Максимальний строк життя presigned URL для SigV4 — 7 діб.
# Беремо близько до максимуму: режим "precision" у HeyGen обробляє довго,
# і ми не знаємо напевно, чи HeyGen тягне файл одразу при submit, чи згодом
# у процесі обробки. Довгий строк прибирає цей клас помилок.
PRESIGN_EXPIRY_SECONDS = 6 * 24 * 60 * 60

# Явна конфігурація multipart: R2 вимагає однакового розміру всіх частин,
# крім останньої. boto3 і так ділить рівномірно, але фіксуємо параметри,
# щоб поведінка не залежала від версії бібліотеки.
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=64 * 1024 * 1024,
    multipart_chunksize=64 * 1024 * 1024,
    max_concurrency=4,
    use_threads=True,
)


def _get_client():
    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _bucket() -> str:
    return os.environ["R2_BUCKET"]


def upload_file(local_path: str, key: str, content_type: str) -> str:
    """Завантажує файл у R2 під вказаним ключем. Повертає ключ."""
    client = _get_client()
    client.upload_file(
        Filename=local_path,
        Bucket=_bucket(),
        Key=key,
        ExtraArgs={"ContentType": content_type},
        Config=TRANSFER_CONFIG,
    )
    return key


def generate_presigned_url(key: str, expiry_seconds: int = PRESIGN_EXPIRY_SECONDS) -> str:
    """Створює тимчасове публічне посилання на об'єкт (для HeyGen)."""
    client = _get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": key},
        ExpiresIn=expiry_seconds,
    )


def upload_video_and_get_url(local_path: str, video_id: str) -> tuple[str, str]:
    """Завантажує відео та повертає (key, presigned_url)."""
    key = f"source/{video_id}.mp4"
    upload_file(local_path, key, content_type="video/mp4")
    return key, generate_presigned_url(key)


def upload_thumbnail(local_path: str, video_id: str) -> str:
    """Завантажує обкладинку (щоб вона дожила до Workflow 2). Повертає key."""
    key = f"thumbnails/{video_id}.jpg"
    upload_file(local_path, key, content_type="image/jpeg")
    return key


def download_file(key: str, local_path: str) -> str:
    client = _get_client()
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    client.download_file(Bucket=_bucket(), Key=key, Filename=local_path)
    return local_path


def delete_keys(keys: list[str]) -> None:
    """
    Видаляє об'єкти. Помилки лише логуються — прибирання сховища не має
    ламати основний пайплайн (наприклад, вже успішну публікацію).
    """
    client = _get_client()
    for key in keys:
        if not key:
            continue
        try:
            client.delete_object(Bucket=_bucket(), Key=key)
            print(f"[r2_storage] Видалено: {key}")
        except Exception as e:
            print(f"[r2_storage] Не вдалось видалити {key}: {e}")
