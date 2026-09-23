"""
Одноразова ініціалізація стану ПЕРЕД першим справжнім запуском.

Проблема, яку вирішує: пайплайн вважає «новим» будь-яке відео, якого немає в
processed.json. При першому запуску це означає до 5 відео одразу — тобто
кілька перекладів HeyGen і кілька генерацій обкладинок за один раз.

Цей скрипт позначає всі поточні відео каналу як уже відомі (status:
"bootstrap"), щоб автоматика підхопила лише те, що зʼявиться ПОТІМ.

Запуск (локально, один раз):
    export YOUTUBE_API_KEY=...
    export SOURCE_CHANNEL_HANDLE=@...
    cd scripts && python bootstrap_state.py

Після цього закомітьте data/processed.json у репозиторій.
"""

import os
from datetime import datetime, timezone

import state_manager
import youtube_monitor

YT_API_KEY = os.environ["YOUTUBE_API_KEY"]
SOURCE_CHANNEL_HANDLE = os.environ.get("SOURCE_CHANNEL_HANDLE", "").strip()
if not SOURCE_CHANNEL_HANDLE:
    raise RuntimeError(
        "Не задано SOURCE_CHANNEL_HANDLE (хендл вихідного каналу, напр. \"@example\")."
    )
# Беремо із запасом, щоб нічого зі старого не проскочило.
HOW_MANY = int(os.environ.get("BOOTSTRAP_COUNT", 10))


def main():
    videos = youtube_monitor.get_latest_videos(
        YT_API_KEY, handle=SOURCE_CHANNEL_HANDLE, max_results=HOW_MANY
    )

    if not videos:
        print("Не знайдено жодного відео — перевірте YOUTUBE_API_KEY і handle каналу.")
        return

    state = state_manager.load_state()
    added = 0
    now = datetime.now(timezone.utc).isoformat()

    for video in videos:
        vid = video["video_id"]
        if vid in state:
            continue
        state[vid] = {
            "status": "bootstrap",
            "reason": "existed_before_automation_started",
            "title_original": video["title"],
            "duration_seconds": video["duration_seconds"],
            "created_at": now,
            "updated_at": now,
        }
        added += 1
        mins, secs = divmod(video["duration_seconds"], 60)
        print(f"  позначено: [{mins}:{secs:02d}] {video['title'][:70]}")

    state_manager.save_state(state)
    print(f"\nГотово. Додано записів: {added}. Всього в стані: {len(state)}.")
    print("Тепер закомітьте data/processed.json:")
    print("  git add data/processed.json && git commit -m 'chore: bootstrap state' && git push")


if __name__ == "__main__":
    main()
