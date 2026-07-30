"""
Локальний HTTP-сервер, який віддає ОДИН файл і відслідковує, скільки з нього
реально завантажили.

Навіщо трекінг: тунель живе лише поки виконується job GitHub Actions. З
документації HeyGen не видно, коли саме він забирає відео — одразу при submit
чи вже під час обробки. Замість того щоб вгадувати, ми точно знаємо, коли
файл віддано повністю, і тільки тоді гасимо тунель.

Підтримка Range-запитів обов'язкова: завантажувачі часто спершу роблять HEAD,
а потім тягнуть файл частинами. Сервер без 206 Partial Content такі клієнти
можуть відкинути.
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 256 * 1024


class _RangeTracker:
    """Відслідковує, які байтові інтервали файлу вже віддано."""

    def __init__(self, total_size: int):
        self.total_size = total_size
        self._intervals: list[tuple[int, int]] = []  # [start, end] включно
        self._lock = threading.Lock()
        self.active_requests = 0
        self.request_count = 0

    def add(self, start: int, end: int) -> None:
        if end < start:
            return
        with self._lock:
            self._intervals.append((start, end))
            self._intervals.sort()
            merged: list[tuple[int, int]] = []
            for s, e in self._intervals:
                if merged and s <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
            self._intervals = merged

    def is_complete(self) -> bool:
        with self._lock:
            if not self._intervals:
                return False
            first_start, first_end = self._intervals[0]
            return first_start == 0 and first_end >= self.total_size - 1

    def served_bytes(self) -> int:
        with self._lock:
            return sum(e - s + 1 for s, e in self._intervals)

    def progress_percent(self) -> float:
        if self.total_size == 0:
            return 0.0
        return 100.0 * self.served_bytes() / self.total_size


class _SingleFileHandler(BaseHTTPRequestHandler):
    file_path = None
    file_size = 0
    url_path = "/video.mp4"
    tracker: _RangeTracker = None

    protocol_version = "HTTP/1.1"
    server_version = "SingleFileServer/1.0"

    def log_message(self, fmt, *args):
        print(f"[tunnel_server] {self.address_string()} {fmt % args}")

    def _reject(self, code: int = 404):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _parse_range(self):
        """Повертає (start, end) включно, або None якщо Range не заданий."""
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return None
        spec = header[len("bytes="):].split(",")[0].strip()
        if spec.startswith("-"):
            length = int(spec[1:])
            start = max(0, self.file_size - length)
            return start, self.file_size - 1
        parts = spec.split("-")
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 and parts[1] else self.file_size - 1
        return start, min(end, self.file_size - 1)

    def do_HEAD(self):
        if self.path != self.url_path:
            return self._reject()
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(self.file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        if self.path != self.url_path:
            return self._reject()

        rng = self._parse_range()
        try:
            if rng is None:
                start, end = 0, self.file_size - 1
                status = 200
            else:
                start, end = rng
                if start >= self.file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{self.file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        except (ValueError, IndexError):
            return self._reject(400)

        length = end - start + 1

        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{self.file_size}")
        self.end_headers()

        self.tracker.active_requests += 1
        self.tracker.request_count += 1
        sent_to = start - 1
        try:
            with open(self.file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(CHUNK, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
                    sent_to += len(data)
        except (BrokenPipeError, ConnectionResetError):
            # Клієнт обірвав зв'язок — фіксуємо лише те, що встигли віддати.
            print("[tunnel_server] Клієнт обірвав з'єднання")
        finally:
            self.tracker.active_requests -= 1
            if sent_to >= start:
                self.tracker.add(start, sent_to)
            print(f"[tunnel_server] Віддано {self.tracker.progress_percent():.1f}% файлу")


class FileServer:
    """
    Піднімає HTTP-сервер на localhost, який віддає вказаний файл.

    Використання:
        with FileServer(path) as srv:
            srv.port, srv.url_path
            srv.wait_until_downloaded(timeout_seconds=1800)
    """

    def __init__(self, file_path: str, url_path: str = "/video.mp4", port: int = 0):
        self.file_path = file_path
        self.file_size = os.path.getsize(file_path)
        self.url_path = url_path
        self.tracker = _RangeTracker(self.file_size)
        self._requested_port = port
        self._httpd = None
        self._thread = None
        self.port = None

    def __enter__(self):
        handler = type("_Handler", (_SingleFileHandler,), {
            "file_path": self.file_path,
            "file_size": self.file_size,
            "url_path": self.url_path,
            "tracker": self.tracker,
        })
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._requested_port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        print(f"[tunnel_server] Слухаю 127.0.0.1:{self.port}, файл {self.file_size / 1024 / 1024:.1f} MB")
        return self

    def __exit__(self, *exc):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        print("[tunnel_server] Сервер зупинено")

    def wait_until_downloaded(self, timeout_seconds: int, grace_seconds: int = 120,
                               poll_interval: int = 5) -> bool:
        """
        Чекає, поки файл віддадуть повністю.

        Після повного завантаження витримує grace_seconds — HeyGen може
        звернутись повторно (retry або окремий запит на іншому етапі
        обробки). Якщо в цей час почався новий запит, чекаємо і його.

        Повертає True, якщо файл віддано повністю, False — якщо вийшов час.
        """
        import time
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            if self.tracker.is_complete():
                print("[tunnel_server] Файл віддано повністю, витримую grace-період")
                grace_deadline = time.time() + grace_seconds
                while time.time() < grace_deadline:
                    time.sleep(poll_interval)
                    if self.tracker.active_requests > 0:
                        print("[tunnel_server] Почався новий запит — продовжую чекати")
                        grace_deadline = time.time() + grace_seconds
                return True
            time.sleep(poll_interval)

        print(f"[tunnel_server] Таймаут. Віддано {self.tracker.progress_percent():.1f}%, "
              f"запитів: {self.tracker.request_count}")
        return False
