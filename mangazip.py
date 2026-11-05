#!/usr/bin/env python3
"""MangaZIP: GUI sencilla para guardar imágenes de un capítulo accesible en un ZIP.

Uso responsable: descarga únicamente imágenes que la página entregue normalmente.
No intenta saltarse inicios de sesión, captchas, DRM ni protecciones de acceso.
"""
from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
import os
import platform
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, UnidentifiedImageError
from soupsieve import SelectorSyntaxError
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "MangaZIP"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
LogFn = Callable[[str], None]
# Atributos de lazy loading, en orden de prioridad sobre el placeholder de `src`.
IMAGE_ATTRS = (
    "data-src", "data-original", "data-lazy-src", "data-url", "data-cfsrc",
    "data-ks-lazyload", "data-echo",
)
SRCSET_ATTRS = ("srcset", "data-srcset")
READER_SELECTORS = (
    ".reader-area img",                 # MangaFire y lectores similares
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

# Límites de seguridad y cortesía con el servidor.
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


def clean_name(value: str, fallback: str = "capitulo") -> str:
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
    """Devuelve candidatos srcset en el orden declarado, de menor a mayor calidad."""
    result = []
    for part in (value or "").split(","):
        item = part.strip().split()
        if item:
            url = normalize_url(item[0], base_url)
            if url:
                result.append(url)
    return result


def attrs_from_element(element, base_url: str) -> list[str]:
    """Devuelve candidatos en orden de prioridad.

    Primero los atributos de lazy loading (el `src` suele ser un placeholder),
    después la mejor variante de `srcset`, luego `src` y, por último, un
    `background-image` incrustado en el estilo.
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
            # Suele ser la variante de mayor tamaño declarada.
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
    """Extrae las URLs de las páginas y devuelve (urls, título, selector usado)."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else "capitulo"
    if not title:
        title = "capitulo"

    selected = []
    selector_used = ""
    if css_selector.strip():
        try:
            selected = soup.select(css_selector.strip())
            selector_used = css_selector.strip()
        except SelectorSyntaxError as exc:
            raise ValueError(f"Selector CSS no válido: {exc}") from exc
        if not selected:
            raise ValueError(
                "El selector CSS no encontró elementos. "
                "Prueba otro selector, por ejemplo: .reader-area img"
            )
    else:
        # Preferimos una zona de lectura conocida; así se evitan iconos, portadas y anuncios.
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
            # Fallback genérico: imágenes de la página, filtrando decoración.
            selected = [
                img
                for img in soup.find_all(["img", "source"])
                if img.name == "source" or is_likely_reader_image(img)
            ]
            selector_used = "img (detección automática)"

    urls: list[str] = []
    seen: set[str] = set()
    for element in selected:
        # Si el selector encuentra contenedores, mirar también imágenes descendientes.
        elements = [element]
        if getattr(element, "name", None) not in ("img", "source"):
            elements.extend(element.find_all(["img", "source"]))
        for item in elements:
            if getattr(item, "name", None) == "img" and not css_selector.strip() and not is_likely_reader_image(item):
                continue
            candidates = attrs_from_element(item, page_url)
            if candidates:
                candidate = candidates[0]
                if candidate not in seen:
                    seen.add(candidate)
                    urls.append(candidate)

    return urls, title, selector_used


def save_image_as_png(content: bytes, destination: Path) -> tuple[int, int]:
    """Guarda la imagen como PNG sin recompresión con pérdida.

    PNG no recupera la calidad ya perdida en un JPEG/WebP; conserva los píxeles
    decodificados. Las fuentes animadas se guardan con el primer fotograma.
    """
    with Image.open(BytesIO(content)) as original:
        original.seek(0)
        original.load()
        image = ImageOps.exif_transpose(original)

        # Mantener los modos compatibles con PNG para no convertir píxeles de más.
        png_modes = {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16", "I;16B", "I;16L"}
        if image.mode not in png_modes:
            has_alpha = (
                "A" in image.getbands()
                or "transparency" in image.info
                or image.mode in {"PA", "RGBa", "La"}
            )
            image = image.convert("RGBA" if has_alpha else "RGB")

        save_options = {"format": "PNG", "optimize": False, "compress_level": 3}
        # Conservar metadatos de color cuando existan.
        if image.info.get("icc_profile"):
            save_options["icc_profile"] = image.info["icc_profile"]
        if image.info.get("dpi"):
            save_options["dpi"] = image.info["dpi"]
        image.save(destination, **save_options)
        return image.size


def decode_html(content: bytes) -> str:
    """Decodifica el HTML respetando el charset declarado y con alternativas habituales."""
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
            raise InterruptedError("Descarga cancelada por el usuario.")
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError(
                f"el archivo supera el límite de {max_bytes // (1024 * 1024)} MB"
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
) -> tuple[bytes, str]:
    """Descarga con reintentos y backoff ante errores transitorios."""
    last_exc: Exception | None = None
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        if cancel_event.is_set():
            raise InterruptedError("Descarga cancelada por el usuario.")
        try:
            with session.get(url, headers=headers, timeout=timeout, stream=True) as response:
                if response.status_code in RETRY_STATUS and attempt < REQUEST_ATTEMPTS:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                return read_limited(response, max_bytes, cancel_event), content_type
        except requests.RequestException as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            retryable = status is None or status in RETRY_STATUS
            if attempt >= REQUEST_ATTEMPTS or not retryable:
                break
            delay = backoff * (2 ** (attempt - 1))
            log(f"Reintento {attempt + 1}/{REQUEST_ATTEMPTS} en {delay:.1f}s ({exc})")
            if cancel_event.wait(delay):
                raise InterruptedError("Descarga cancelada por el usuario.") from exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("La petición falló sin un error concreto.")


def fetch_html(
    url: str,
    use_browser: bool,
    log: LogFn,
    cancel_event: threading.Event,
) -> tuple[str, requests.Session]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    })
    if use_browser:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Has marcado la opción de JavaScript, pero Playwright no está instalado.\n"
                "Instálalo con: python -m pip install playwright\n"
                "Y después: python -m playwright install chromium\n"
                "O desmarca esa casilla para usar la descarga HTML normal."
            ) from exc
        log("Abriendo la página con Chromium para cargar JavaScript…")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT, locale="es-ES")
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1800)
                if cancel_event.is_set():
                    raise InterruptedError("Descarga cancelada por el usuario.")
                # Activar lazy-loading sin pulsar controles ni intentar resolver protecciones.
                for _ in range(5):
                    page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 700))")
                    page.wait_for_timeout(450)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(400)
                html = page.content()
                for cookie in context.cookies():
                    session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
            finally:
                browser.close()
        return html, session

    log("Leyendo el HTML del capítulo…")
    content, content_type = request_with_retries(
        session,
        url,
        headers={},
        cancel_event=cancel_event,
        log=log,
        max_bytes=MAX_HTML_BYTES,
        timeout=(12, 35),
    )
    if "text/html" not in content_type.lower() and "application/xhtml" not in content_type.lower():
        log(f"Aviso: el servidor devuelve Content-Type {content_type or '(no indicado)'}.")
    return decode_html(content), session


class MangaZIPApp:
    """Ventana principal: formulario, progreso y registro de la descarga."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MangaZIP — Descargador de capítulos")
        self.root.geometry("760x600")
        self.root.minsize(650, 520)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.current_temp_dir: Path | None = None
        self._closing = False
        self.output_dir = tk.StringVar(value=str(Path.home() / "Descargas" / "MangaZIP"))
        self.url_var = tk.StringVar()
        self.selector_var = tk.StringVar()
        self.zipname_var = tk.StringVar()
        self.browser_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Listo para descargar un capítulo.")
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        content = ttk.Frame(self.root, padding=16)
        content.pack(fill="both", expand=True)

        ttk.Label(
            content,
            text="MangaZIP",
            font=("TkDefaultFont", 18, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            content,
            text=(
                "Pega la URL de un capítulo y crea un ZIP con todas las páginas "
                "en PNG sin recompresión con pérdida."
            ),
        ).pack(anchor="w", pady=(2, 14))

        ttk.Label(content, text="URL del capítulo").pack(anchor="w")
        self.url_entry = ttk.Entry(content, textvariable=self.url_var)
        self.url_entry.pack(fill="x", pady=(4, 12))

        ttk.Label(content, text="Carpeta de destino").pack(anchor="w")
        folder_row = ttk.Frame(content)
        folder_row.pack(fill="x", pady=(4, 12))
        ttk.Entry(folder_row, textvariable=self.output_dir).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(folder_row, text="Elegir…", command=self._choose_folder).pack(
            side="left", padx=(8, 0)
        )

        ttk.Label(content, text="Nombre del ZIP (opcional; sin .zip)").pack(anchor="w")
        ttk.Entry(content, textvariable=self.zipname_var).pack(fill="x", pady=(4, 12))

        ttk.Label(
            content,
            text="Selector CSS opcional (para ajustar una web concreta)",
        ).pack(anchor="w")
        ttk.Entry(content, textvariable=self.selector_var).pack(fill="x", pady=(4, 4))
        ttk.Label(
            content,
            text="Vacío = detección automática. Ejemplo: .reader-area img",
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 8))

        ttk.Checkbutton(
            content,
            text="Renderizar JavaScript con Chromium (requiere Playwright)",
            variable=self.browser_var,
        ).pack(anchor="w", pady=(0, 10))

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(0, 10))
        self.download_btn = ttk.Button(
            buttons, text="Descargar y crear ZIP", command=self._start_download
        )
        self.download_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            buttons, text="Cancelar", command=self._cancel, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Abrir carpeta", command=self._open_folder).pack(
            side="right"
        )

        self.progress = ttk.Progressbar(content, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 6))
        ttk.Label(content, textvariable=self.status_var).pack(anchor="w", pady=(0, 6))
        ttk.Label(content, text="Registro").pack(anchor="w")

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

    def _log(self, text: str) -> None:
        self.events.put(("log", text))

    def _status(self, text: str) -> None:
        self.events.put(("status", text))

    def _progress(self, current: int, total: int) -> None:
        self.events.put(("progress", current, total))

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if chosen:
            self.output_dir.set(chosen)

    def _open_folder(self) -> None:
        folder = Path(self.output_dir.get()).expanduser()
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
            messagebox.showerror("No se pudo abrir", str(exc))

    def _start_download(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        url = self.url_var.get().strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            messagebox.showwarning("URL no válida", "Pega una URL completa que empiece por https:// o http://")
            return
        out = Path(self.output_dir.get()).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Carpeta no válida",
                f"No puedo crear la carpeta de destino:\n{exc}",
            )
            return
        self.cancel_event.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.progress.configure(value=0, maximum=100)
        self.status_var.set("Iniciando…")
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
        self.status_var.set("Cancelando… (se detendrá lo antes posible)")
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
        try:
            html, session = fetch_html(url, use_browser, self._log, self.cancel_event)
            if self.cancel_event.is_set():
                raise InterruptedError("Descarga cancelada.")
            image_urls, title, used_selector = extract_image_urls(html, url, selector)
            self._log(f"Título detectado: {title}")
            self._log(f"Selector usado: {used_selector or 'automático'}")
            if not image_urls:
                raise RuntimeError(
                    "No he encontrado imágenes en el HTML recibido. Puede que la web necesite JavaScript, "
                    "un selector CSS concreto o que no permita la descarga automática.\n"
                    "Prueba a activar la opción de Chromium o introduce un selector como .reader-area img."
                )
            if len(image_urls) > MAX_IMAGES:
                self._log(
                    f"Aviso: se han detectado {len(image_urls)} imágenes; "
                    f"se descargarán solo las primeras {MAX_IMAGES}."
                )
                image_urls = image_urls[:MAX_IMAGES]
            self._log(f"He encontrado {len(image_urls)} posibles páginas.")
            temp_dir = Path(tempfile.mkdtemp(prefix="mangazip_", dir=str(out)))
            self.current_temp_dir = temp_dir
            downloaded: list[Path] = []
            failed_count = 0
            for index, image_url in enumerate(image_urls, start=1):
                if self.cancel_event.is_set():
                    raise InterruptedError("Descarga cancelada por el usuario.")
                self._status(f"Descargando página {index} de {len(image_urls)}…")
                self._progress(index - 1, len(image_urls))
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
                    )
                    content_type = content_type.lower()
                    is_image_magic = content.startswith(
                        (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")
                    )
                    if "text/html" in content_type and not is_image_magic:
                        raise ValueError("el servidor devolvió HTML en vez de una imagen")
                    if not content:
                        raise ValueError("archivo vacío")
                    filepath = temp_dir / f"{index:03d}.png"
                    try:
                        width, height = save_image_as_png(content, filepath)
                    except (UnidentifiedImageError, OSError, ValueError) as image_exc:
                        raise ValueError(f"no se pudo decodificar la imagen: {image_exc}") from image_exc
                    downloaded.append(filepath)
                    self._log(
                        f"[{index:03d}/{len(image_urls):03d}] OK  "
                        f"{filepath.name} ({width}×{height}, PNG)"
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
                    self._log(f"[{index:03d}/{len(image_urls):03d}] ERROR  {exc}")
                self._progress(index, len(image_urls))
                if self.cancel_event.wait(DOWNLOAD_DELAY):
                    raise InterruptedError("Descarga cancelada por el usuario.")

            if self.cancel_event.is_set():
                raise InterruptedError("Descarga cancelada por el usuario.")
            if not downloaded:
                raise RuntimeError("No se ha podido descargar ninguna imagen. Revisa el registro de errores.")

            chosen_name = clean_name(zipname, "") or clean_name(title, "capitulo")
            zip_path = out / f"{chosen_name}.zip"
            if zip_path.exists():
                zip_path = out / f"{chosen_name}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
            # Los PNG ya están comprimidos: volver a comprimirlos solo gasta CPU.
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
                for filepath in downloaded:
                    archive.write(filepath, arcname=filepath.name)
            self._log(f"ZIP creado: {zip_path}")
            self._log(f"Páginas guardadas como PNG: {len(downloaded)} de {len(image_urls)}.")
            if failed_count:
                self._log(
                    f"Aviso: {failed_count} imágenes no se pudieron descargar."
                )
            summary = (
                f"Terminado: {len(downloaded)} páginas convertidas a PNG "
                f"sin recompresión con pérdida.\n\n{zip_path}"
            )
            if failed_count:
                summary += (
                    f"\n\nNo se pudieron descargar {failed_count} imágenes; "
                    "consulta el registro."
                )
            self.events.put(("done", True, summary))
        except InterruptedError as exc:
            self._log(str(exc))
            self.events.put(("done", False, str(exc)))
        except Exception as exc:  # pylint: disable=broad-except
            # Frontera del hilo: cualquier fallo inesperado debe llegar a la UI.
            self._log(f"ERROR: {exc}")
            self.events.put(("done", False, str(exc)))
        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            self.current_temp_dir = None

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Salir de MangaZIP",
                "Hay una descarga en curso. ¿Quieres cancelarla y salir?",
            ):
                return
            self._closing = True
            self.cancel_event.set()
            self.status_var.set("Cancelando y cerrando…")
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
        """Limpieza de último recurso al cerrar la aplicación."""
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
                    ok, message = event[1], event[2]
                    if self._closing:
                        continue
                    self.download_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.status_var.set("Proceso finalizado." if ok else "Proceso detenido o fallido.")
                    if ok:
                        messagebox.showinfo("MangaZIP", message)
                    elif message and message != "Descarga cancelada por el usuario.":
                        messagebox.showerror("MangaZIP", message)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    app = MangaZIPApp(root)
    try:
        root.mainloop()
    finally:
        app.cleanup_temp()


if __name__ == "__main__":
    main()
