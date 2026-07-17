"""Browser-based Google organic-result title extraction."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import socket
import subprocess
import time
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen


class GoogleSerpProtocolError(RuntimeError):
    """Raised when Chrome cannot return usable Google organic-result titles."""


class GoogleSerpVerificationRequired(GoogleSerpProtocolError):
    """A user must complete the visible Google verification challenge."""

    def __init__(self, message: str, image_base64: str | None = None) -> None:
        super().__init__(message)
        self.image_base64 = image_base64


class _GoogleResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._href: str | None = None
        self._in_heading = False
        self._parts: list[str] = []
        self.items: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
        elif tag == "h3" and self._href:
            self._in_heading, self._parts = True, []

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._in_heading:
            self.items.append({"title": " ".join("".join(self._parts).split()), "href": self._href or "", "is_ad": False})
            self._in_heading = False
        elif tag == "a":
            self._href = None


class BrowserSerpTitleClient:
    """Use the installed Chrome browser to read Google US organic-result titles.

    Google may request a verification challenge. The caller receives a clear
    error instead of fabricated results and can retry after verification.
    """

    def __init__(self, chrome_path: str | Path | None = None, *, timeout: int = 40) -> None:
        self._chrome_path = Path(chrome_path) if chrome_path else self._find_chrome()
        self._timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._port: int | None = None

    def fetch_titles(self, *, keyword: str, locale: str = "en-US", max_count: int = 20) -> list[dict[str, str | int]]:
        if not self._chrome_path:
            raise GoogleSerpProtocolError("未找到 Chrome 浏览器，无法执行 Google 标题抓取。")
        language, country = (locale.split("-", 1) + ["US"])[:2] if "-" in locale else ("en", "US")
        titles: list[dict[str, str | int]] = []
        for start in range(0, max_count, 10):
            query = urlencode({"q": keyword, "hl": language, "gl": country, "start": start})
            html = self._chrome_dump_dom(f"https://www.google.com/search?{query}")
            parser = _GoogleResultParser()
            parser.feed(html)
            page_titles = self.parse_result_items(parser.items, max_count=max_count - len(titles))
            seen = {str(item["title"]).casefold() for item in titles}
            for item in page_titles:
                if str(item["title"]).casefold() not in seen:
                    item["rank"] = len(titles) + 1
                    titles.append(item)
                    seen.add(str(item["title"]).casefold())
                if len(titles) >= max_count:
                    return titles
            if not page_titles:
                break
        if not titles:
            raise GoogleSerpProtocolError("浏览器没有读取到 Google 自然搜索标题。")
        return titles

    def _chrome_dump_dom(self, url: str) -> str:
        self._ensure_chrome()
        if self._port is None:
            raise GoogleSerpProtocolError("Chrome 调试连接不可用。")
        keep_open = False
        try:
            websocket_url = self._page_websocket_url(self._port)
            try:
                import websocket
            except ImportError as error:
                raise GoogleSerpProtocolError("本机缺少浏览器自动化连接组件。") from error
            connection = websocket.create_connection(websocket_url, timeout=self._timeout)
            try:
                self._cdp(connection, 1, "Page.enable")
                self._cdp(connection, 2, "Page.navigate", {"url": url})
                deadline = time.monotonic() + self._timeout
                while time.monotonic() < deadline:
                    ready = self._cdp(connection, 3, "Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
                    if ready.get("result", {}).get("result", {}).get("value") == "complete":
                        break
                    time.sleep(0.4)
                page = self._cdp(
                    connection,
                    4,
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify({text:document.body.innerText,html:document.documentElement.outerHTML})",
                        "returnByValue": True,
                    },
                )
                value = page.get("result", {}).get("result", {}).get("value")
                if not isinstance(value, str):
                    raise GoogleSerpProtocolError("Chrome 未能返回 Google 搜索页面。")
                document = json.loads(value)
                text = document.get("text") if isinstance(document, dict) else ""
                if not isinstance(text, str):
                    raise GoogleSerpProtocolError("Chrome 未能返回 Google 搜索页面。")
                html = document.get("html") if isinstance(document, dict) else ""
                if self.requires_verification(text):
                    keep_open = True
                    try:
                        screenshot = self._cdp(connection, 5, "Page.captureScreenshot", {"format": "png"})
                        image = screenshot.get("result", {}).get("data")
                    except GoogleSerpProtocolError:
                        image = None
                    raise GoogleSerpVerificationRequired("Google 要求浏览器验证；请在已打开的 Chrome 窗口完成验证后重新点击抓取。", image if isinstance(image, str) else None)
                if not isinstance(html, str) or not html.strip():
                    raise GoogleSerpProtocolError("Chrome 未能返回 Google 搜索页面。")
                return html
            finally:
                connection.close()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise GoogleSerpProtocolError("Chrome 启动或 Google 搜索超时。") from error
        finally:
            if not keep_open:
                self._close_chrome()

    def _ensure_chrome(self) -> None:
        if self._process is not None and self._process.poll() is None and self._port is not None:
            return
        if self._chrome_path is None:
            raise GoogleSerpProtocolError("未找到 Chrome 浏览器，无法执行 Google 标题抓取。")
        port = self._free_port()
        profile = Path(__file__).resolve().parents[3] / "data" / "google-serp-browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self._process = subprocess.Popen(
                [
                    str(self._chrome_path),
                    f"--remote-debugging-port={port}",
                    "--remote-allow-origins=*",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--lang=en-US",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._port = port
        except OSError as error:
            self._process, self._port = None, None
            raise GoogleSerpProtocolError("Chrome 无法启动。") from error

    def _close_chrome(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._process, self._port = None, None

    def _page_websocket_url(self, port: int) -> str:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as response:  # nosec B310 - local Chrome CDP only
                    targets = json.loads(response.read().decode("utf-8"))
                for target in targets:
                    if isinstance(target, dict) and target.get("type") == "page" and isinstance(target.get("webSocketDebuggerUrl"), str):
                        return target["webSocketDebuggerUrl"]
            except OSError:
                time.sleep(0.2)
        raise GoogleSerpProtocolError("Chrome 调试连接超时。")

    @staticmethod
    def _cdp(connection: Any, identifier: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        connection.send(json.dumps({"id": identifier, "method": method, "params": params or {}}))
        while True:
            message = json.loads(connection.recv())
            if message.get("id") == identifier:
                if "error" in message:
                    raise GoogleSerpProtocolError(f"Chrome 浏览器命令失败：{method}")
                return message

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def requires_verification(visible_text: str) -> bool:
        value = visible_text.casefold()
        return any(marker in value for marker in (
            "unusual traffic",
            "our systems have detected unusual traffic",
            "verify you are human",
            "verify that you are not a robot",
        ))

    @staticmethod
    def parse_result_items(items: list[dict[str, Any]], *, max_count: int) -> list[dict[str, str | int]]:
        results: list[dict[str, str | int]] = []
        seen: set[str] = set()
        for item in items:
            title, href = item.get("title"), item.get("href")
            if item.get("is_ad") or not isinstance(title, str) or not isinstance(href, str):
                continue
            title = " ".join(title.split())
            hostname = urlparse(href).hostname
            if not title or not hostname or hostname.endswith("google.com") or title.casefold() in seen:
                continue
            seen.add(title.casefold())
            results.append({"rank": len(results) + 1, "title": title, "source": hostname.removeprefix("www.")})
            if len(results) >= max_count:
                break
        return results

    @staticmethod
    def _find_chrome() -> Path | None:
        for value in (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            path = Path(value)
            if path.exists():
                return path
        return None
