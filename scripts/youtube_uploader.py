"""
Заливка готового відео на іспанський канал @AHORAMISMO-b5e через YouTube Data API v3.

Використовує OAuth 2.0 refresh token (не Service Account — YouTube Data API
для завантаження відео від імені каналу вимагає саме OAuth від власника каналу).
"""

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


def _get_authenticated_service(client_id: str, client_secret: str, refresh_token: str):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube"],
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_video(client_id: str, client_secret: str, refresh_token: str,
                  video_path: str, title: str, description: str,
                  thumbnail_path: str = None, tags: list[str] = None,
                  category_id: str = "25", privacy_status: str = "public") -> str:
    """
    Заливає відео, за наявності — встановлює кастомну обкладинку.
    Повертає video_id заливаного відео.

    category_id 25 = "News & Politics"
    """
    youtube = _get_authenticated_service(client_id, client_secret, refresh_token)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()

    video_id = response["id"]

    if thumbnail_path:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg"),
        ).execute()

    return video_id
