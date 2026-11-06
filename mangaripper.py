#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
"""Mangaripper: a small GUI to save the images of an accessible chapter into a ZIP.

Responsible use: download only images the page serves normally.
It does not try to bypass logins, captchas, DRM, or access protections.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from io import BytesIO
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, UnidentifiedImageError, __version__ as PILLOW_VERSION
from soupsieve import SelectorSyntaxError
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from languages import (
    LANGUAGES,
    get_language,
    load_language,
    save_language,
    set_language,
    t,
)

APP_NAME = "Mangaripper"
APP_VERSION = "0.2.0"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
LogFn = Callable[[str], None]
# Lazy-loading attributes, in priority order over the `src` placeholder.
IMAGE_ATTRS = (
    "data-src", "data-original", "data-lazy-src", "data-url", "data-cfsrc",
    "data-ks-lazyload", "data-echo",
)
SRCSET_ATTRS = ("srcset", "data-srcset")
READER_SELECTORS = (
    ".chapter-images img",            # MangaFire (contenedor principal observado)
    ".reading-content .page-chapter img",  # MangaFire alternativa
    ".reader-area img",               # MangaFire y lectores similares
    ".reading-content img",
    ".chapter-content img",
    ".reader-content img",
    ".reader-main img",
    ".reader-images img",
    ".reader-page img",
    ".chapter-container img",
    ".container-chapter-reader img",
    ".reading-detail .page-chapter img",
    "#readerarea img",
    "#reader img",
    ".viewer img",
    "article img",
)

# Safety limits and server-friendly behaviour.
MAX_IMAGES = 1000
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_HTML_BYTES = 8 * 1024 * 1024
REQUEST_ATTEMPTS = 3
RETRY_BACKOFF = 1.0
DOWNLOAD_DELAY = 0.3
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def clean_name(value: str, fallback: str = "chapter") -> str:
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", value or "")
    value = re.sub(r"\s+", " ", value).strip(" ._-\t\n")
    value = value[:120].strip(" ._-\t\n")
    if value and value.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    return value or fallback


def normalize_url(value: str, base_url: str) -> str | None:
    value = (value or "").strip().strip('"\'')
    if not value or value.startswith(("data:", "blob:", "javascript:", "about:")):
        return None
    absolute = urljoin(base_url, value)
    absolute, _fragment = urldefrag(absolute)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return absolute


def parse_srcset(value: str, base_url: str) -> list[str]:
    """Return srcset candidates in declared order, usually smallest to largest."""
    result = []
    for part in (value or "").split(","):
        item = part.strip().split()
        if item:
            url = normalize_url(item[0], base_url)
            if url:
                result.append(url)
    return result


def attrs_from_element(element, base_url: str) -> list[str]:
    """Return candidates in priority order.

    Lazy-loading attributes come first (the `src` is often a placeholder),
    then the best `srcset` variant, then `src`, and finally an inline
    `background-image`.
    """
    found = []
    attrs = getattr(element, "attrs", {}) or {}

    def attr_text(name: str) -> str:
        value = attrs.get(name)
        if isinstance(value, list):
            return " ".join(str(v) for v in value)
        return str(value) if value else ""

    for attr in IMAGE_ATTRS:
        url = normalize_url(attr_text(attr), base_url)
        if url:
            found.append(url)

    for attr in SRCSET_ATTRS:
        candidates = parse_srcset(attr_text(attr), base_url)
        if candidates:
            # Usually the largest declared variant.
            found.append(candidates[-1])

    url = normalize_url(attr_text("src"), base_url)
    if url:
        found.append(url)

    style = attr_text("style")
    match = re.search(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)", style, re.I)
    if match:
        url = normalize_url(match.group(1), base_url)
        if url:
            found.append(url)
    return found


def is_likely_reader_image(img) -> bool:
    context = " ".join([
        str(img.get("class", "")), str(img.get("id", "")),
        str(img.parent.get("class", "") if img.parent else ""),
        str(img.parent.get("id", "") if img.parent else ""),
        str(img.get("alt", "")),
    ]).lower()
    bad_words = ("avatar", "logo", "banner", "advert", "ads-", "icon", "flag", "cover", "poster", "thumbnail", "thumb-")
    return not any(word in context for word in bad_words)


def extract_image_urls(
    html: str,
    page_url: str,
    css_selector: str = "",
) -> tuple[list[str], str, str]:
    """Extract page URLs and return (urls, title, selector used)."""
    soup = BeautifulSoup(html, "html.parser")
    fallback_title = t("fallback.chapter")
    title = (
        soup.title.get_text(" ", strip=True) if soup.title else fallback_title
    )
    if not title:
        title = fallback_title

    selected = []
    selector_used = ""
    if css_selector.strip():
        try:
            selected = soup.select(css_selector.strip())
            selector_used = css_selector.strip()
        except SelectorSyntaxError as exc:
            raise ValueError(t("error.invalid_selector", error=exc)) from exc
        if not selected:
            raise ValueError(t("error.selector_no_match"))
    else:
        # Prefer a known reader area to avoid icons, covers, and adverts.
        best = []
        for selector in READER_SELECTORS:
            try:
                matches = soup.select(selector)
            except SelectorSyntaxError:
                matches = []
            if len(matches) > len(best):
                best = matches
                selector_used = selector
        if len(best) >= 2:
            selected = best
        else:
            # Generic fallback: page images, filtering out decoration.
            selected = [
                img
                for img in soup.find_all(["img", "source"])
                if img.name == "source" or is_likely_reader_image(img)
            ]
            selector_used = t("selector.generic")

    urls: list[str] = []
    seen: set[str] = set()
    for element in selected:
        # If the selector matches containers, also inspect descendant images.
        elements = [element]
        if getattr(element, "name", None) not in ("img", "source"):
            elements.extend(element.find_all(["img", "source"]))
        for item in elements:
            is_img = getattr(item, "name", None) == "img"
            if is_img and not css_selector.strip() and not is_likely_reader_image(item):
                continue
            candidates = attrs_from_element(item, page_url)
            if candidates:
                candidate = candidates[0]
                if candidate not in seen:
                    seen.add(candidate)
                    urls.append(candidate)

    return urls, title, selector_used


def save_image_as_png(content: bytes, destination: Path) -> tuple[int, int]:
    """Save the image as PNG without lossy re-compression.

    PNG cannot restore quality already lost in a JPEG/WebP source; it keeps the
    decoded pixels. Animated sources are saved using their first frame.
    """
    with Image.open(BytesIO(content)) as original:
        original.seek(0)
        original.load()
        image = ImageOps.exif_transpose(original)

        # Keep PNG-compatible modes to avoid unnecessary pixel conversions.
        png_modes = {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16", "I;16B", "I;16L"}
        if image.mode not in png_modes:
            has_alpha = (
                "A" in image.getbands()
                or "transparency" in image.info
                or image.mode in {"PA", "RGBa", "La"}
            )
            image = image.convert("RGBA" if has_alpha else "RGB")

        save_options = {"format": "PNG", "optimize": False, "compress_level": 3}
        # Preserve colour metadata when present.
        if image.info.get("icc_profile"):
            save_options["icc_profile"] = image.info["icc_profile"]
        if image.info.get("dpi"):
            save_options["dpi"] = image.info["dpi"]
        image.save(destination, **save_options)
        return image.size


def decode_html(content: bytes) -> str:
    """Decode HTML using the declared charset and common fallbacks."""
    match = re.search(
        rb"<meta[^>]+charset\s*=\s*[\"']?([a-zA-Z0-9_\-]+)",
        content[:4096],
        re.I,
    )
    encodings = []
    if match:
        encodings.append(match.group(1).decode("ascii", "ignore"))
    encodings.extend(["utf-8", "cp1252"])
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


def read_limited(
    response: requests.Response,
    max_bytes: int,
    cancel_event: threading.Event,
) -> bytes:
    data = bytearray()
    for chunk in response.iter_content(65536):
        if cancel_event.is_set():
            raise InterruptedError(t("log.cancelled"))
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError(
                t("error.too_large", limit=max_bytes // (1024 * 1024))
            )
    return bytes(data)


def request_with_retries(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    cancel_event: threading.Event,
    log: LogFn,
    *,
    max_bytes: int,
    timeout: tuple[int, int] = (12, 45),
    backoff: float = RETRY_BACKOFF,
    detail: LogFn | None = None,
) -> tuple[bytes, str]:
    """Download with retries and backoff for transient errors."""
    last_exc: Exception | None = None
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        if cancel_event.is_set():
            if detail:
                detail("Cancelled by the user before the request.")
            raise InterruptedError(t("log.cancelled"))
        started = time.monotonic()
        if detail:
            detail(f"GET {url} (attempt {attempt}/{REQUEST_ATTEMPTS})")
        try:
            with session.get(url, headers=headers, timeout=timeout, stream=True) as response:
                elapsed = time.monotonic() - started
                if detail:
                    headers_seen = response.headers
                    detail(
                        "  -> HTTP {status}{reason} | Content-Type: {ctype} | "
                        "Content-Length: {length} | Content-Encoding: {encoding} | "
                        "headers in {elapsed:.3f}s".format(
                            status=response.status_code,
                            reason=f" {response.reason}"
                            if getattr(response, "reason", "")
                            else "",
                            ctype=headers_seen.get("Content-Type") or "-",
                            length=headers_seen.get("Content-Length") or "-",
                            encoding=headers_seen.get("Content-Encoding") or "-",
                            elapsed=elapsed,
                        )
                    )
                if response.status_code in RETRY_STATUS and attempt < REQUEST_ATTEMPTS:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                body = read_limited(response, max_bytes, cancel_event)
                if detail:
                    detail(
                        f"  -> received {len(body)} bytes in "
                        f"{time.monotonic() - started:.3f}s"
                    )
                return body, content_type
        except requests.RequestException as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            retryable = status is None or status in RETRY_STATUS
            if detail:
                detail(
                    f"  request failed: {exc!r} (status={status}, "
                    f"retryable={retryable})"
                )
            if attempt >= REQUEST_ATTEMPTS or not retryable:
                break
            delay = backoff * (2 ** (attempt - 1))
            log(
                t(
                    "log.retry",
                    attempt=attempt + 1,
                    attempts=REQUEST_ATTEMPTS,
                    delay=delay,
                    error=exc,
                )
            )
            if detail:
                detail(f"  waiting {delay:.2f}s before retrying")
            if cancel_event.wait(delay):
                if detail:
                    detail("Cancelled by the user while waiting for a retry.")
                raise InterruptedError(t("log.cancelled")) from exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(t("error.request_failed"))


def fetch_html(
    url: str,
    use_browser: bool,
    log: LogFn,
    cancel_event: threading.Event,
    *,
    detail: LogFn | None = None,
) -> tuple[str, requests.Session, list[str]]:
    """Fetch the chapter HTML with requests or optionally with Playwright.
    
    Returns (html, session, captured_image_urls) where captured_image_urls
    contains image URLs intercepted from network responses during Playwright
    rendering (useful for sites with virtualized DOM like MangaFire).
    """
    language = get_language()
    ui_locale = "es-ES" if language == "es" else "en-US"
    accept_language = (
        "es-ES,es;q=0.9,en;q=0.7" if language == "es" else "en-US,en;q=0.9,es;q=0.7"
    )
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": accept_language,
    })
    if detail:
        detail(f"User-Agent: {USER_AGENT}")
        detail(f"Accept-Language: {accept_language}")
    captured_urls: list[str] = []
    if use_browser:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(t("error.playwright_missing")) from exc
        log(t("log.opening_chromium"))
        if detail:
            detail(f"Playwright: launching headless Chromium (locale={ui_locale})")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT, locale=ui_locale)
            page = context.new_page()
            # Intercept image responses from common manga CDNs.
            def handle_response(response):
                try:
                    u = response.url
                    if any(cdn in u for cdn in ("mfcdn", "webtoon-phinf", "cdn.manga", "mangadex")) \
                       and u.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        if u not in captured_urls:
                            captured_urls.append(u)
                            if detail:
                                detail(f"  [network] captured: {u}")
                except Exception:
                    pass
            page.on("response", handle_response)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if detail:
                    status = response.status if response is not None else "?"
                    detail(f"  page.goto -> HTTP {status} | final URL: {page.url}")
                page.wait_for_timeout(1800)
                if cancel_event.is_set():
                    raise InterruptedError(t("log.cancelled"))
                # Trigger lazy loading without clicking controls or bypassing protections.
                # Scroll until no new images are captured for several consecutive scrolls.
                # No hard limit on total scrolls — only stop when images stop appearing.
                scrolls_without_new = 0
                max_scrolls_without_new = 10
                # No max_total_scrolls: keep going until the page truly stops loading.
                last_captured_count = len(captured_urls)
                scroll_num = 0
                while True:
                    if cancel_event.is_set():
                        raise InterruptedError(t("log.cancelled"))
                    scroll_num += 1
                    page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 1000))")
                    page.wait_for_timeout(800)
                    current_count = len(captured_urls)
                    if current_count > last_captured_count:
                        if detail:
                            detail(
                                f"  scroll {scroll_num}: +{current_count - last_captured_count} new images "
                                f"(total {current_count})"
                            )
                        last_captured_count = current_count
                        scrolls_without_new = 0
                    else:
                        scrolls_without_new += 1
                        if detail:
                            detail(
                                f"  scroll {scroll_num}: no new images ({scrolls_without_new}/"
                                f"{max_scrolls_without_new})"
                            )
                    if scrolls_without_new >= max_scrolls_without_new:
                        if detail:
                            detail(
                                f"  stopping: no new images for {max_scrolls_without_new} consecutive scrolls "
                                f"(total scrolls: {scroll_num}, total images: {current_count})"
                            )
                        break
                # Final sweep to the very bottom to catch any remaining images.
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
                # Scroll back to top so relative URLs resolve correctly if needed.
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(400)
                html = page.content()
                cookies = context.cookies()
                for cookie in cookies:
                    session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
            finally:
                browser.close()
        if detail:
            detail(f"  rendered HTML: {len(html)} characters | {len(cookies)} cookies | {len(captured_urls)} network images")
        return html, session, captured_urls

    log(t("log.reading_html"))
    content, content_type = request_with_retries(
        session,
        url,
        headers={},
        cancel_event=cancel_event,
        log=log,
        max_bytes=MAX_HTML_BYTES,
        timeout=(12, 35),
        detail=detail,
    )
    if "text/html" not in content_type.lower() and "application/xhtml" not in content_type.lower():
        if detail:
            detail(
                f"  unexpected Content-Type for HTML: {content_type or '(none)'}"
            )
        log(
            t(
                "log.content_type_warning",
                content_type=content_type or t("log.content_type_none"),
            )
        )
    html = decode_html(content)
    if detail:
        detail(f"  HTML decoded: {len(html)} characters")
    return html, session, []

    log(t("log.reading_html"))
    content, content_type = request_with_retries(
        session,
        url,
        headers={},
        cancel_event=cancel_event,
        log=log,
        max_bytes=MAX_HTML_BYTES,
        timeout=(12, 35),
        detail=detail,
    )
    if "text/html" not in content_type.lower() and "application/xhtml" not in content_type.lower():
        if detail:
            detail(
                f"  unexpected Content-Type for HTML: {content_type or '(none)'}"
            )
        log(
            t(
                "log.content_type_warning",
                content_type=content_type or t("log.content_type_none"),
            )
        )
    html = decode_html(content)
    if detail:
        detail(f"  HTML decoded: {len(html)} characters")
    return html, session


def default_output_dir() -> Path:
    """Fallback suggestion (the user's real download folder) when the
    destination field is empty."""
    home = Path.home()
    for name in ("Downloads", "Descargas"):
        folder = home / name
        if folder.is_dir():
            return folder / APP_NAME
    return home / "Downloads" / APP_NAME


def default_log_dir() -> Path:
    """Folder for the detailed logs.

    It prefers ``logs/`` next to the application (handy when running from the
    repository). If that place is read-only (for example an installed package),
    it falls back to the user data folder of the current OS.
    """
    local = Path(__file__).resolve().parent / "logs"
    try:
        local.mkdir(parents=True, exist_ok=True)
        probe = local / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return local
    except OSError:
        pass

    home = Path.home()
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif system == "Darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    fallback = base / "Mangaripper" / "logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


class DownloadLog:
    """Highly detailed, timestamped log of a single download."""

    def __init__(
        self,
        directory: Path,
        label: str,
        header: dict[str, str],
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stem = clean_name(label, "download")[:80] or "download"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = directory / f"{stamp}_{stem}.log"
        counter = 1
        while path.exists():
            path = directory / f"{stamp}_{stem}_{counter}.log"
            counter += 1
        self.path = path
        self._started = time.monotonic()
        self._handle = path.open("w", encoding="utf-8")
        try:
            self._handle.write(f"{APP_NAME} {APP_VERSION} — download log\n")
            self._handle.write("=" * 78 + "\n")
            for key, value in header.items():
                self._handle.write(f"{key}: {value}\n")
            self._handle.write("=" * 78 + "\n")
            self._handle.flush()
        except BaseException:
            self._handle.close()
            raise

    def detail(self, message: str) -> None:
        """Append one timestamped line (with elapsed time) and flush it."""
        elapsed = time.monotonic() - self._started
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._handle.write(f"[{stamp}] (+{elapsed:8.3f}s) {message}\n")
        self._handle.flush()

    def close(self) -> None:
        """Write a closing line and release the file handle."""
        try:
            self.detail(f"Log finished (total {time.monotonic() - self._started:.3f}s).")
        finally:
            self._handle.close()

    def __enter__(self) -> "DownloadLog":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()


class NullDownloadLog:
    """No-op log used when the detailed log file cannot be created."""

    path = None

    def detail(self, _message: str) -> None:
        """Discard the line."""

    def close(self) -> None:
        """Nothing to close."""


class MangaripperApp:
    """Main window: form, progress, and download log."""

    def __init__(self, root: tk.Tk) -> None:
        set_language(load_language())
        self.root = root
        self.root.title(t("app.title"))
        self.root.geometry("760x600")
        self.root.minsize(650, 520)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.current_temp_dir: Path | None = None
        self._closing = False
        self.output_dir = tk.StringVar(value="")
        self.url_var = tk.StringVar()
        self.selector_var = tk.StringVar()
        self.zipname_var = tk.StringVar()
        self.browser_var = tk.BooleanVar(value=False)
        self.language_var = tk.StringVar(value=LANGUAGES[get_language()])
        self._language_codes = {name: code for code, name in LANGUAGES.items()}
        self.status_var = tk.StringVar(value=t("status.ready"))
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        content = ttk.Frame(self.root, padding=16)
        content.pack(fill="both", expand=True)

        header = ttk.Frame(content)
        header.pack(fill="x")
        ttk.Label(
            header,
            text=APP_NAME,
            font=("TkDefaultFont", 18, "bold"),
        ).pack(side="left")
        language_row = ttk.Frame(header)
        language_row.pack(side="right")
        self.language_label = ttk.Label(language_row, text=t("ui.language"))
        self.language_label.pack(side="left", padx=(0, 6))
        self.language_box = ttk.Combobox(
            language_row,
            textvariable=self.language_var,
            state="readonly",
            width=10,
            values=list(self._language_codes),
        )
        self.language_box.pack(side="left")
        self.language_box.bind("<<ComboboxSelected>>", self._on_language_change)

        self.subtitle_label = ttk.Label(content, text=t("app.subtitle"))
        self.subtitle_label.pack(anchor="w", pady=(10, 14))

        self.url_label = ttk.Label(content, text=t("ui.url"))
        self.url_label.pack(anchor="w")
        self.url_entry = ttk.Entry(content, textvariable=self.url_var)
        self.url_entry.pack(fill="x", pady=(4, 12))

        self.folder_label = ttk.Label(content, text=t("ui.folder"))
        self.folder_label.pack(anchor="w")
        folder_row = ttk.Frame(content)
        folder_row.pack(fill="x", pady=(4, 12))
        ttk.Entry(folder_row, textvariable=self.output_dir).pack(
            side="left", fill="x", expand=True
        )
        self.choose_button = ttk.Button(
            folder_row, text=t("ui.choose"), command=self._choose_folder
        )
        self.choose_button.pack(side="left", padx=(8, 0))
        self.folder_hint = ttk.Label(
            content,
            text=t("ui.folder_hint"),
            foreground="#666666",
        )
        self.folder_hint.pack(anchor="w")

        self.zipname_label = ttk.Label(content, text=t("ui.zipname"))
        self.zipname_label.pack(anchor="w")
        ttk.Entry(content, textvariable=self.zipname_var).pack(fill="x", pady=(4, 12))

        self.selector_label = ttk.Label(content, text=t("ui.selector"))
        self.selector_label.pack(anchor="w")
        ttk.Entry(content, textvariable=self.selector_var).pack(fill="x", pady=(4, 4))
        self.selector_hint = ttk.Label(
            content,
            text=t("ui.selector_hint"),
            foreground="#666666",
        )
        self.selector_hint.pack(anchor="w", pady=(0, 8))

        self.browser_check = ttk.Checkbutton(
            content,
            text=t("ui.use_browser"),
            variable=self.browser_var,
        )
        self.browser_check.pack(anchor="w", pady=(0, 10))

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(0, 10))
        self.download_btn = ttk.Button(
            buttons, text=t("ui.download"), command=self._start_download
        )
        self.download_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            buttons, text=t("ui.cancel"), command=self._cancel, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.open_folder_btn = ttk.Button(
            buttons, text=t("ui.open_folder"), command=self._open_folder
        )
        self.open_folder_btn.pack(side="right")

        self.progress = ttk.Progressbar(content, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 6))
        ttk.Label(content, textvariable=self.status_var).pack(anchor="w", pady=(0, 6))
        self.log_label = ttk.Label(content, text=t("ui.log"))
        self.log_label.pack(anchor="w")

        log_frame = ttk.Frame(content)
        log_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.log_text = tk.Text(
            log_frame,
            height=10,
            wrap="word",
            state="disabled",
            font=("TkFixedFont", 9),
        )
        scrollbar = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _retranslate(self) -> None:
        """Update every visible translatable widget for the current locale."""
        self.root.title(t("app.title"))
        self.language_label.configure(text=t("ui.language"))
        self.subtitle_label.configure(text=t("app.subtitle"))
        self.url_label.configure(text=t("ui.url"))
        self.folder_label.configure(text=t("ui.folder"))
        self.folder_hint.configure(text=t("ui.folder_hint"))
        self.choose_button.configure(text=t("ui.choose"))
        self.zipname_label.configure(text=t("ui.zipname"))
        self.selector_label.configure(text=t("ui.selector"))
        self.selector_hint.configure(text=t("ui.selector_hint"))
        self.browser_check.configure(text=t("ui.use_browser"))
        self.download_btn.configure(text=t("ui.download"))
        self.cancel_btn.configure(text=t("ui.cancel"))
        self.open_folder_btn.configure(text=t("ui.open_folder"))
        self.log_label.configure(text=t("ui.log"))
        if not (self.worker and self.worker.is_alive()):
            self.status_var.set(t("status.ready"))

    def _on_language_change(self, _event=None) -> None:
        name = self.language_var.get()
        code = self._language_codes.get(name)
        if not code or code == get_language():
            return
        set_language(code)
        save_language(code)
        self._retranslate()

    def _log(self, text: str) -> None:
        self.events.put(("log", text))

    def _status(self, text: str) -> None:
        self.events.put(("status", text))

    def _progress(self, current: int, total: int) -> None:
        self.events.put(("progress", current, total))

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title=t("dialog.choose_folder"),
            initialdir=self.output_dir.get() or str(Path.home()),
        )
        if chosen:
            self.output_dir.set(chosen)

    def _open_folder(self) -> None:
        destination = self.output_dir.get().strip() or str(default_output_dir())
        folder = Path(destination).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            system = platform.system()
            if system == "Windows":
                # pylint: disable-next=no-member
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif system == "Darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showerror(t("dialog.open_failed_title"), str(exc))

    def _start_download(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        url = self.url_var.get().strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            messagebox.showwarning(
                t("dialog.invalid_url_title"),
                t("dialog.invalid_url"),
            )
            return
        destination = self.output_dir.get().strip()
        if not destination:
            chosen = filedialog.askdirectory(
                title=t("dialog.choose_folder"),
                initialdir=str(default_output_dir()),
            )
            if not chosen:
                self.status_var.set(t("status.no_folder"))
                return
            self.output_dir.set(chosen)
            destination = chosen
        out = Path(destination).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                t("dialog.invalid_folder_title"),
                t("dialog.invalid_folder", error=exc),
            )
            return
        self.cancel_event.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.progress.configure(value=0, maximum=100)
        self.status_var.set(t("status.starting"))
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.worker = threading.Thread(
            target=self._download_worker,
            args=(
                url,
                out,
                self.selector_var.get().strip(),
                self.zipname_var.get().strip(),
                self.browser_var.get(),
            ),
            daemon=True,
        )
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set(t("status.cancelling"))
        self.cancel_btn.configure(state="disabled")

    def _download_worker(
        self,
        url: str,
        out: Path,
        selector: str,
        zipname: str,
        use_browser: bool,
    ) -> None:
        temp_dir: Path | None = None
        started = time.monotonic()
        log: "DownloadLog | NullDownloadLog" = NullDownloadLog()
        detail: LogFn = log.detail
        try:
            try:
                parsed = urlparse(url)
                label = f"{parsed.netloc}_{Path(parsed.path).stem or 'chapter'}"
                candidate = DownloadLog(
                    default_log_dir(),
                    label,
                    {
                        "Started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "App": f"{APP_NAME} {APP_VERSION}",
                        "URL": url,
                        "Destination folder": str(out),
                        "ZIP name": zipname or "(automatic)",
                        "CSS selector": selector or "(automatic)",
                        "JavaScript rendering": "yes" if use_browser else "no",
                        "Interface language": get_language(),
                        "Platform": platform.platform(),
                        "Python": sys.version.split()[0],
                        "requests": requests.__version__,
                        "Pillow": PILLOW_VERSION,
                    },
                )
                detail = candidate.detail
                log = candidate
                self._log(t("log.detail_file", path=candidate.path))
            except OSError as exc:
                self._log(t("log.detail_file_error", error=exc))

            html, session, captured_urls = fetch_html(
                url,
                use_browser,
                self._log,
                self.cancel_event,
                detail=detail,
            )
            if self.cancel_event.is_set():
                raise InterruptedError(t("log.cancelled"))
            image_urls, title, used_selector = extract_image_urls(html, url, selector)
            # If Playwright captured image URLs from network (e.g. MangaFire virtualized DOM),
            # merge them: use DOM order for images present in both, then append network-only ones.
            if captured_urls:
                detail(
                    f"Merging {len(image_urls)} DOM URLs with "
                    f"{len(captured_urls)} network-captured URLs"
                )
                dom_set = set(image_urls)
                # Keep DOM order for URLs that exist in both
                merged = list(image_urls)
                # Append captured URLs not already in DOM, preserving capture order
                for u in captured_urls:
                    if u not in dom_set:
                        merged.append(u)
                image_urls = merged
                used_selector = used_selector or "dom+network-capture"
            self._log(t("log.title", title=title))
            self._log(
                t(
                    "log.selector",
                    selector=used_selector or t("selector.auto"),
                )
            )
            detail(f"Chapter title: {title}")
            detail(f"Selector used: {used_selector or '(automatic)'}")
            if not image_urls:
                raise RuntimeError(t("error.no_images"))
            if len(image_urls) > MAX_IMAGES:
                self._log(
                    t(
                        "log.too_many_images",
                        count=len(image_urls),
                        limit=MAX_IMAGES,
                    )
                )
                detail(
                    "Image limit reached: "
                    f"{len(image_urls)} detected, only the first {MAX_IMAGES} "
                    "will be downloaded"
                )
                image_urls = image_urls[:MAX_IMAGES]
            self._log(t("log.found_pages", count=len(image_urls)))
            detail(f"Image URLs found: {len(image_urls)}")
            for position, candidate in enumerate(image_urls, start=1):
                detail(f"  [{position:03d}] {candidate}")
            temp_dir = Path(tempfile.mkdtemp(prefix="mangaripper_", dir=str(out)))
            self.current_temp_dir = temp_dir
            detail(f"Temporary folder: {temp_dir}")
            downloaded: list[Path] = []
            failed_count = 0
            for index, image_url in enumerate(image_urls, start=1):
                if self.cancel_event.is_set():
                    raise InterruptedError(t("log.cancelled"))
                self._status(
                    t("status.downloading", current=index, total=len(image_urls))
                )
                self._progress(index - 1, len(image_urls))
                detail(
                    f"Page {index}/{len(image_urls)}: downloading "
                    f"{image_url} (Referer: {url})"
                )
                page_started = time.monotonic()
                try:
                    content, content_type = request_with_retries(
                        session,
                        image_url,
                        headers={
                            "Referer": url,
                            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        },
                        cancel_event=self.cancel_event,
                        log=self._log,
                        max_bytes=MAX_IMAGE_BYTES,
                        detail=detail,
                    )
                    content_type = content_type.lower()
                    is_image_magic = content.startswith(
                        (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")
                    )
                    detail(
                        f"  Content-Type: {content_type or '(none)'} | "
                        f"image signature: {'yes' if is_image_magic else 'no'}"
                    )
                    if "text/html" in content_type and not is_image_magic:
                        raise ValueError(t("error.html_instead_of_image"))
                    if not content:
                        raise ValueError(t("error.empty_file"))
                    filepath = temp_dir / f"{index:03d}.png"
                    try:
                        width, height = save_image_as_png(content, filepath)
                    except (UnidentifiedImageError, OSError, ValueError) as image_exc:
                        raise ValueError(
                            t("error.decode_failed", error=image_exc)
                        ) from image_exc
                    downloaded.append(filepath)
                    self._log(
                        t(
                            "log.page_ok",
                            index=index,
                            total=len(image_urls),
                            filename=filepath.name,
                            width=width,
                            height=height,
                        )
                    )
                    detail(
                        f"  saved {filepath} | {width}x{height} px | "
                        f"{filepath.stat().st_size} bytes on disk | "
                        f"{time.monotonic() - page_started:.3f}s"
                    )
                except InterruptedError:
                    raise
                except (
                    requests.RequestException,
                    OSError,
                    ValueError,
                    Image.DecompressionBombError,
                ) as exc:
                    failed_count += 1
                    self._log(
                        t("log.page_error", index=index, total=len(image_urls), error=exc)
                    )
                    detail(
                        f"  page failed after "
                        f"{time.monotonic() - page_started:.3f}s: {exc!r}"
                    )
                self._progress(index, len(image_urls))
                if self.cancel_event.wait(DOWNLOAD_DELAY):
                    raise InterruptedError(t("log.cancelled"))

            if self.cancel_event.is_set():
                raise InterruptedError(t("log.cancelled"))
            if not downloaded:
                raise RuntimeError(t("error.no_downloads"))

            chosen_name = clean_name(zipname, "") or clean_name(
                title, t("fallback.chapter")
            )
            zip_path = out / f"{chosen_name}.zip"
            if zip_path.exists():
                zip_path = out / f"{chosen_name}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
            # PNG files are already compressed: re-compressing them wastes CPU.
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
                for filepath in downloaded:
                    archive.write(filepath, arcname=filepath.name)
            self._log(t("log.zip_created", path=zip_path))
            self._log(
                t("log.pages_saved", saved=len(downloaded), total=len(image_urls))
            )
            if failed_count:
                self._log(t("log.failed_warning", count=failed_count))
            detail(
                f"ZIP created: {zip_path} | {zip_path.stat().st_size} bytes | "
                f"{len(downloaded)} entries | ZIP_STORED"
            )
            detail(
                f"Finished OK: {len(downloaded)} pages saved, "
                f"{failed_count} failed, total "
                f"{time.monotonic() - started:.3f}s"
            )
            summary = t(
                "summary.done",
                saved=len(downloaded),
                path=zip_path,
            )
            if failed_count:
                summary += "\n\n" + t("summary.failed", count=failed_count)
            self.events.put(("done", True, summary, False))
        except InterruptedError as exc:
            self._log(str(exc))
            detail(f"Cancelled by the user: {exc}")
            self.events.put(("done", False, str(exc), True))
        except Exception as exc:  # pylint: disable=broad-except
            # Thread boundary: any unexpected failure must reach the UI.
            self._log(t("log.error", error=exc))
            detail(f"ERROR: {exc!r}")
            detail(traceback.format_exc())
            self.events.put(("done", False, str(exc), False))
        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            self.current_temp_dir = None
            log.close()

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                t("dialog.close_title"),
                t("dialog.close_running"),
            ):
                return
            self._closing = True
            self.cancel_event.set()
            self.status_var.set(t("status.cancelling_close"))
            self.download_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
            self.root.withdraw()
            self._wait_and_close()
        else:
            self.root.destroy()

    def _wait_and_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(150, self._wait_and_close)
        else:
            self.root.destroy()

    def cleanup_temp(self) -> None:
        """Last-resort cleanup when shutting the application down."""
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.worker.join(timeout=2.0)
        if self.current_temp_dir and self.current_temp_dir.exists():
            shutil.rmtree(self.current_temp_dir, ignore_errors=True)
        self.current_temp_dir = None

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", event[1] + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(event[1])
                elif kind == "progress":
                    current, total = event[1], event[2]
                    self.progress.configure(maximum=max(total, 1), value=current)
                elif kind == "done":
                    ok, message, cancelled = event[1], event[2], event[3]
                    if self._closing:
                        continue
                    self.download_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.status_var.set(
                        t("status.finished") if ok else t("status.failed")
                    )
                    if ok:
                        messagebox.showinfo(t("dialog.title"), message)
                    elif message and not cancelled:
                        messagebox.showerror(t("dialog.title"), message)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    app = MangaripperApp(root)
    try:
        root.mainloop()
    finally:
        app.cleanup_temp()


if __name__ == "__main__":
    main()
