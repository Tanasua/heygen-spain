"""
Перевірка нових відео на вихідному каналі через YouTube Data API v3.

Використовує:
1. channels.list (за handle або channel_id) -> uploads playlist id
2. playlistItems.list -> останні відео цього плейлиста
3. videos.list -> тривалість (contentDetails.duration, формат ISO 8601)
"""

import re
from googleapiclient.discovery import build

# ТИМЧАСОВО піднято з 6*60 до 8*60 для тесту. Поверни на 6*60 після тесту.
MAX_DURATION_SECONDS = 8 * 60


def _parse_iso8601_duration(duration: str) -> int:
    """Перетворює 'PT19M1S' -> секунди."""
    match = re.match(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration
    )
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    total = 0
    if hours:
        total += int(hours) * 3600
    if minutes:
        total += int(minutes) * 60
    if seconds:
        total += int(seconds)
    return total


def get_uploads_playlist_id(youtube, channel_id: str = None, handle: str = None) -> str:
    if channel_id:
        resp = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    elif handle:
        resp = youtube.channels().list(part="contentDetails", forHandle=handle).execute()
    else:
        raise ValueError("Потрібен channel_id або handle")

    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"Канал не знайдено (channel_id={channel_id}, handle={handle})")

    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_latest_videos(api_key: str, channel_id: str = None, handle: str = None,
                       max_results: int = 5) -> list[dict]:
    """
    Повертає список останніх відео каналу:
    [{"video_id": ..., "title": ..., "published_at": ..., "duration_seconds": ...}, ...]
    Відсортовано від найновішого до найстарішого.
    """
    youtube = build("youtube", "v3", developerKey=api_key)
    uploads_playlist_id = get_uploads_playlist_id(youtube, channel_id=channel_id, handle=handle)

    playlist_resp = youtube.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=uploads_playlist_id,
        maxResults=max_results,
    ).execute()

    video_ids = [item["contentDetails"]["videoId"] for item in playlist_resp.get("items", [])]
    if not video_ids:
        return []

    videos_resp = youtube.videos().list(
        part="contentDetails,snippet",
        id=",".join(video_ids),
    ).execute()

    results = []
    for item in videos_resp.get("items", []):
        duration_seconds = _parse_iso8601_duration(item["contentDetails"]["duration"])
        results.append({
            "video_id": item["id"],
            "title": item["snippet"]["title"],
            "published_at": item["snippet"]["publishedAt"],
            "duration_seconds": duration_seconds,
            "url": f"https://www.youtube.com/watch?v={item['id']}",
            "thumbnail_url": item["snippet"]["thumbnails"].get("maxres", item["snippet"]["thumbnails"]["high"])["url"],
        })

    return results


def is_short_enough(duration_seconds: int) -> bool:
    return duration_seconds < MAX_DURATION_SECONDS
