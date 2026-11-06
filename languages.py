"""Translation layer for Mangaripper.

Adding a language means adding one entry to ``LANGUAGES`` and one dictionary
of the same keys to ``TRANSLATIONS``. Missing keys fall back to English, so a
new translation can be completed incrementally.
"""
from __future__ import annotations

import json
import os
import platform
from pathlib import Path

DEFAULT_LANGUAGE = "en"

LANGUAGES: dict[str, str] = {
    "en": "English",
    "es": "Español",
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        # Window and form
        "app.title": "Mangaripper — Chapter downloader",
        "app.subtitle": (
            "Paste a chapter URL and create a ZIP with every page as a lossless PNG."
        ),
        "ui.language": "Language",
        "ui.url": "Chapter URL",
        "ui.folder": "Destination folder",
        "ui.folder_hint": "Empty: you are asked where to save before each download.",
        "ui.choose": "Choose…",
        "ui.zipname": "ZIP name (optional; without .zip)",
        "ui.selector": "Optional CSS selector (for a specific site)",
        "ui.selector_hint": "Empty = automatic detection. Example: .reader-area img",
        "ui.use_browser": "Render JavaScript with Chromium (requires Playwright)",
        "ui.download": "Download and create ZIP",
        "ui.cancel": "Cancel",
        "ui.open_folder": "Open folder",
        "ui.log": "Log",
        # Status line
        "status.ready": "Ready to download a chapter.",
        "status.no_folder": "No destination folder was selected.",
        "status.starting": "Starting…",
        "status.downloading": "Downloading page {current} of {total}…",
        "status.cancelling": "Cancelling… (it will stop as soon as possible)",
        "status.cancelling_close": "Cancelling and closing…",
        "status.finished": "Finished.",
        "status.failed": "Stopped or failed.",
        # Dialogs
        "dialog.title": "Mangaripper",
        "dialog.invalid_url_title": "Invalid URL",
        "dialog.invalid_url": "Paste a full URL starting with https:// or http://",
        "dialog.invalid_folder_title": "Invalid folder",
        "dialog.invalid_folder": "I can't create the destination folder:\n{error}",
        "dialog.open_failed_title": "Could not open",
        "dialog.close_title": "Exit Mangaripper",
        "dialog.close_running": (
            "A download is in progress. Do you want to cancel it and exit?"
        ),
        "dialog.choose_folder": "Choose the destination folder",
        # Log
        "log.reading_html": "Reading the chapter HTML…",
        "log.opening_chromium": "Opening the page with Chromium to load JavaScript…",
        "log.content_type_warning": (
            "Warning: the server returns Content-Type {content_type}."
        ),
        "log.content_type_none": "(not specified)",
        "log.retry": "Retry {attempt}/{attempts} in {delay:.1f}s ({error})",
        "log.title": "Detected title: {title}",
        "log.selector": "Selector used: {selector}",
        "log.too_many_images": (
            "Warning: {count} images detected; only the first {limit} will be downloaded."
        ),
        "log.found_pages": "Found {count} possible pages.",
        "log.page_ok": "[{index:03d}/{total:03d}] OK  {filename} ({width}×{height}, PNG)",
        "log.page_error": "[{index:03d}/{total:03d}] ERROR  {error}",
        "log.zip_created": "ZIP created: {path}",
        "log.pages_saved": "Pages saved as PNG: {saved} of {total}.",
        "log.failed_warning": "Warning: {count} images could not be downloaded.",
        "log.cancelled": "Download cancelled by the user.",
        "log.error": "ERROR: {error}",
        # Summary
        "summary.done": (
            "Done: {saved} pages converted to PNG without lossy re-compression."
            "\n\n{path}"
        ),
        "summary.failed": "Could not download {count} images; check the log.",
        # Errors
        "error.no_images": (
            "I couldn't find any images in the received HTML. The site may need "
            "JavaScript, a specific CSS selector, or it may not allow automatic "
            "downloads.\nTry enabling Chromium or entering a selector such as "
            ".reader-area img."
        ),
        "error.no_downloads": "No image could be downloaded. Check the error log.",
        "error.invalid_selector": "Invalid CSS selector: {error}",
        "error.selector_no_match": (
            "The CSS selector found no elements. Try another selector, for "
            "example: .reader-area img"
        ),
        "error.playwright_missing": (
            "You enabled the JavaScript option, but Playwright is not installed.\n"
            "Install it with: python -m pip install playwright\n"
            "Then run: python -m playwright install chromium\n"
            "Or uncheck that box to use the normal HTML download."
        ),
        "error.html_instead_of_image": "the server returned HTML instead of an image",
        "error.empty_file": "empty file",
        "error.decode_failed": "could not decode the image: {error}",
        "error.too_large": "the file exceeds the {limit} MB limit",
        "error.request_failed": "The request failed without a specific error.",
        # Extraction helpers
        "selector.auto": "automatic",
        "selector.generic": "img (auto-detection)",
        "fallback.chapter": "chapter",
    },
    "es": {
        # Ventana y formulario
        "app.title": "Mangaripper — Descargador de capítulos",
        "app.subtitle": (
            "Pega la URL de un capítulo y crea un ZIP con todas las páginas "
            "en PNG sin recompresión con pérdida."
        ),
        "ui.language": "Idioma",
        "ui.url": "URL del capítulo",
        "ui.folder": "Carpeta de destino",
        "ui.folder_hint": "Vacío: se pregunta dónde guardar antes de cada descarga.",
        "ui.choose": "Elegir…",
        "ui.zipname": "Nombre del ZIP (opcional; sin .zip)",
        "ui.selector": "Selector CSS opcional (para una web concreta)",
        "ui.selector_hint": "Vacío = detección automática. Ejemplo: .reader-area img",
        "ui.use_browser": "Renderizar JavaScript con Chromium (requiere Playwright)",
        "ui.download": "Descargar y crear ZIP",
        "ui.cancel": "Cancelar",
        "ui.open_folder": "Abrir carpeta",
        "ui.log": "Registro",
        # Estado
        "status.ready": "Listo para descargar un capítulo.",
        "status.no_folder": "No se ha elegido carpeta de destino.",
        "status.starting": "Iniciando…",
        "status.downloading": "Descargando página {current} de {total}…",
        "status.cancelling": "Cancelando… (se detendrá lo antes posible)",
        "status.cancelling_close": "Cancelando y cerrando…",
        "status.finished": "Proceso finalizado.",
        "status.failed": "Proceso detenido o fallido.",
        # Diálogos
        "dialog.title": "Mangaripper",
        "dialog.invalid_url_title": "URL no válida",
        "dialog.invalid_url": "Pega una URL completa que empiece por https:// o http://",
        "dialog.invalid_folder_title": "Carpeta no válida",
        "dialog.invalid_folder": "No puedo crear la carpeta de destino:\n{error}",
        "dialog.open_failed_title": "No se pudo abrir",
        "dialog.close_title": "Salir de Mangaripper",
        "dialog.close_running": (
            "Hay una descarga en curso. ¿Quieres cancelarla y salir?"
        ),
        "dialog.choose_folder": "Elige la carpeta de destino",
        # Registro
        "log.reading_html": "Leyendo el HTML del capítulo…",
        "log.opening_chromium": (
            "Abriendo la página con Chromium para cargar JavaScript…"
        ),
        "log.content_type_warning": (
            "Aviso: el servidor devuelve Content-Type {content_type}."
        ),
        "log.content_type_none": "(no indicado)",
        "log.retry": "Reintento {attempt}/{attempts} en {delay:.1f}s ({error})",
        "log.title": "Título detectado: {title}",
        "log.selector": "Selector usado: {selector}",
        "log.too_many_images": (
            "Aviso: se han detectado {count} imágenes; se descargarán solo "
            "las primeras {limit}."
        ),
        "log.found_pages": "He encontrado {count} posibles páginas.",
        "log.page_ok": "[{index:03d}/{total:03d}] OK  {filename} ({width}×{height}, PNG)",
        "log.page_error": "[{index:03d}/{total:03d}] ERROR  {error}",
        "log.zip_created": "ZIP creado: {path}",
        "log.pages_saved": "Páginas guardadas como PNG: {saved} de {total}.",
        "log.failed_warning": "Aviso: {count} imágenes no se pudieron descargar.",
        "log.cancelled": "Descarga cancelada por el usuario.",
        "log.error": "ERROR: {error}",
        # Resumen
        "summary.done": (
            "Terminado: {saved} páginas convertidas a PNG sin recompresión "
            "con pérdida.\n\n{path}"
        ),
        "summary.failed": (
            "No se pudieron descargar {count} imágenes; consulta el registro."
        ),
        # Errores
        "error.no_images": (
            "No he encontrado imágenes en el HTML recibido. Puede que la web "
            "necesite JavaScript, un selector CSS concreto o que no permita la "
            "descarga automática.\nPrueba a activar la opción de Chromium o "
            "introduce un selector como .reader-area img."
        ),
        "error.no_downloads": (
            "No se ha podido descargar ninguna imagen. Revisa el registro de errores."
        ),
        "error.invalid_selector": "Selector CSS no válido: {error}",
        "error.selector_no_match": (
            "El selector CSS no encontró elementos. Prueba otro selector, "
            "por ejemplo: .reader-area img"
        ),
        "error.playwright_missing": (
            "Has marcado la opción de JavaScript, pero Playwright no está instalado.\n"
            "Instálalo con: python -m pip install playwright\n"
            "Y después: python -m playwright install chromium\n"
            "O desmarca esa casilla para usar la descarga HTML normal."
        ),
        "error.html_instead_of_image": (
            "el servidor devolvió HTML en vez de una imagen"
        ),
        "error.empty_file": "archivo vacío",
        "error.decode_failed": "no se pudo decodificar la imagen: {error}",
        "error.too_large": "el archivo supera el límite de {limit} MB",
        "error.request_failed": "La petición falló sin un error concreto.",
        # Ayudantes de extracción
        "selector.auto": "automático",
        "selector.generic": "img (detección automática)",
        "fallback.chapter": "capitulo",
    },
}

_state = {"code": DEFAULT_LANGUAGE}

def default_config_dir() -> Path:
    """Configuration folder following the conventions of each OS."""
    home = Path.home()
    system = platform.system()
    if system == "Windows":
        # %LOCALAPPDATA% (usually C:\Users\<user>\AppData\Local\Mangaripper).
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return base / "Mangaripper"
    if system == "Darwin":
        # macOS: ~/Library/Application Support/Mangaripper.
        return home / "Library" / "Application Support" / "Mangaripper"
    # Linux and the rest: respect XDG_CONFIG_HOME when defined.
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "mangaripper"


CONFIG_DIR = default_config_dir()
CONFIG_FILE = CONFIG_DIR / "settings.json"


def available_languages() -> dict[str, str]:
    """Return a copy of the code-to-display-name language map."""
    return dict(LANGUAGES)


def get_language() -> str:
    return _state["code"]


def set_language(code: str) -> None:
    valid = code if code in TRANSLATIONS else DEFAULT_LANGUAGE
    _state["code"] = valid


def t(key: str, **kwargs) -> str:
    """Translate a key, falling back to English and then to the key itself."""
    text = TRANSLATIONS.get(_state["code"], {}).get(key)
    if text is None:
        text = TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)
    return text.format(**kwargs) if kwargs else text


def load_language() -> str:
    """Read the saved language, returning English when unavailable."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_LANGUAGE
    code = data.get("language") if isinstance(data, dict) else None
    return code if code in TRANSLATIONS else DEFAULT_LANGUAGE


def save_language(code: str) -> None:
    """Persist the language choice; failures are non-fatal."""
    if code not in TRANSLATIONS:
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps({"language": code}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
