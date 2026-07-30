"""
Одноразове отримання OAuth refresh token для іспанського каналу.

Запускати ЛОКАЛЬНО, на машині з браузером (не на сервері без GUI) — скрипт
підніме локальний сервер і відкриє браузер для входу в Google.

    pip install google-auth-oauthlib
    python get_refresh_token.py /шлях/до/client_secret.json

⚠️ КРИТИЧНО: увійдіть тим Google-акаунтом, який керує каналом
@AHORAMISMO-b5e. Якщо акаунт має доступ до кількох каналів (бренд-акаунти),
Google запитає, який саме вибрати — виберіть іспанський канал, інакше відео
поїдуть не туда.

⚠️ Якщо OAuth consent screen у Google Cloud має статус "Testing", виданий
refresh token, за моїми даними, перестає діяти приблизно через 7 днів, і
автоматика зупиниться з помилкою авторизації. Перед цим кроком переведіть
застосунок у статус "In production" / "Published". Деталі — у SETUP.md.
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("Помилка: вкажіть шлях до client_secret.json")
        sys.exit(1)

    client_secrets_file = sys.argv[1]

    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)

    # access_type=offline -> видається refresh_token
    # prompt=consent      -> refresh_token видається навіть при повторному вході
    credentials = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    if not credentials.refresh_token:
        print("\n❌ Google не повернув refresh_token.")
        print("Найчастіша причина — цей акаунт уже давав згоду раніше.")
        print("Відкрийте https://myaccount.google.com/permissions, приберіть доступ")
        print("для цього застосунку і запустіть скрипт знову.")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("Готово. Додайте це значення в GitHub Secrets як YOUTUBE_ES_REFRESH_TOKEN:")
    print("=" * 70)
    print(credentials.refresh_token)
    print("=" * 70)
    print("\nClient ID і Client Secret візьміть з того ж client_secret.json:")
    print("  YOUTUBE_ES_CLIENT_ID     -> поле client_id")
    print("  YOUTUBE_ES_CLIENT_SECRET -> поле client_secret")
    print("\n⚠️ Не комітьте ці значення в репозиторій і не показуйте нікому.")


if __name__ == "__main__":
    main()
