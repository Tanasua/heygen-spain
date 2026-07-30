"""
Обгортка над `cloudflared tunnel --url` (quick tunnel).

Дає публічний https://*.trycloudflare.com лінк на локальний порт раннера,
без акаунта Cloudflare і без оплати. Цей лінк передається HeyGen.

⚠️ ВАЖЛИВО про умови використання: cloudflared сам при старті виводить
попередження, що quick tunnels без акаунта — це спосіб «поекспериментувати
й спробувати», вони НЕ мають гарантії доступності, підпадають під Cloudflare
Online Services Terms of Use, і Cloudflare залишає за собою право перевіряти
їх використання на порушення цих умов. Дослівно з логу:

  "these account-less Tunnels have no uptime guarantee, are subject to the
   Cloudflare Online Services Terms of Use ... If you intend to use Tunnels
   in production you should use a pre-created named tunnel"

Тобто для постійного продакшн-навантаження Cloudflare радить named tunnel
(потрібен акаунт + свій домен). Деталі й альтернативи — у README.
"""

import os
import re
import subprocess
import threading
import time

import requests

CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
URL_PATTERN = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


class TunnelError(RuntimeError):
    pass


def ensure_cloudflared(dest_dir: str) -> str:
    """Завантажує бінарник cloudflared, якщо його ще немає. Повертає шлях."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "cloudflared")

    if os.path.exists(path) and os.access(path, os.X_OK):
        return path

    print("[tunnel] Завантажую cloudflared...")
    resp = requests.get(CLOUDFLARED_URL, stream=True, timeout=300)
    resp.raise_for_status()
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    os.chmod(path, 0o755)

    version = subprocess.run([path, "--version"], capture_output=True, text=True)
    print(f"[tunnel] {version.stdout.strip() or version.stderr.strip()}")
    return path


class QuickTunnel:
    """
    Контекстний менеджер: піднімає quick tunnel на локальний порт.

    with QuickTunnel(port, binary_path) as tunnel:
        tunnel.public_url  # https://xxx.trycloudflare.com
    """

    def __init__(self, local_port: int, binary_path: str, startup_timeout: int = 90):
        self.local_port = local_port
        self.binary_path = binary_path
        self.startup_timeout = startup_timeout
        self.public_url = None
        self._proc = None
        self._log_lines: list[str] = []

    def _read_output(self):
        for line in self._proc.stdout:
            line = line.rstrip()
            self._log_lines.append(line)
            print(f"[cloudflared] {line}")
            if self.public_url is None:
                match = URL_PATTERN.search(line)
                if match:
                    self.public_url = match.group(0)

    def __enter__(self):
        cmd = [
            self.binary_path, "tunnel",
            "--url", f"http://127.0.0.1:{self.local_port}",
            "--no-autoupdate",
            "--loglevel", "info",
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._read_output, daemon=True).start()

        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self.public_url:
                print(f"[tunnel] Публічний URL: {self.public_url}")
                return self
            if self._proc.poll() is not None:
                raise TunnelError(
                    "cloudflared завершився передчасно. Лог:\n"
                    + "\n".join(self._log_lines[-15:])
                )
            time.sleep(1)

        raise TunnelError(
            f"Не отримав trycloudflare URL за {self.startup_timeout}с. Лог:\n"
            + "\n".join(self._log_lines[-15:])
        )

    def __exit__(self, *exc):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        print("[tunnel] Тунель закрито")

    def verify(self, url_path: str, expected_size: int, attempts: int = 10) -> str:
        """
        Перевіряє, що тунель реально маршрутизує запити до нашого сервера,
        ПЕРЕД тим як віддавати посилання HeyGen. Так ми не витрачаємо
        кредити HeyGen на битий лінк.

        Повертає повний публічний URL до файлу.
        """
        full_url = self.public_url.rstrip("/") + url_path

        for attempt in range(1, attempts + 1):
            try:
                head = requests.head(full_url, timeout=20, allow_redirects=True)
                size = int(head.headers.get("Content-Length", 0))
                accepts_ranges = head.headers.get("Accept-Ranges") == "bytes"

                if head.status_code == 200 and size == expected_size:
                    # Контрольний запит на 1 байт — переконуємось, що тіло
                    # теж проходить, а не лише заголовки.
                    probe = requests.get(full_url, headers={"Range": "bytes=0-0"}, timeout=20)
                    if probe.status_code == 206 and len(probe.content) == 1:
                        print(f"[tunnel] Перевірка пройдена: {size} байт, ranges={accepts_ranges}")
                        return full_url

                print(f"[tunnel] Спроба {attempt}: код={head.status_code}, розмір={size}")
            except Exception as e:
                print(f"[tunnel] Спроба {attempt}: {e}")

            time.sleep(5)

        raise TunnelError(f"Тунель не відповідає коректно після {attempts} спроб: {full_url}")
