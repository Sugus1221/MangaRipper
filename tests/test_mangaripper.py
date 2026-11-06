# SPDX-License-Identifier: Unlicense
from __future__ import annotations

import threading
import unittest
import unittest.mock
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

import mangaripper
import languages as i18n


def setUpModule():
    """Run all core tests with the English locale for deterministic texts."""
    i18n.set_language("en")


class CleanNameTests(unittest.TestCase):
    def test_removes_invalid_characters(self):
        cleaned = mangaripper.clean_name('a/b\\c:d*e?f"g<h>i|j')
        self.assertNotRegex(cleaned, r'[<>:"/\\|?*]')
        self.assertTrue(cleaned)

    def test_collapses_spaces_and_strips_dots(self):
        self.assertEqual(mangaripper.clean_name("  capítulo   01 ... "), "capítulo 01")

    def test_truncates_to_120_characters(self):
        self.assertEqual(len(mangaripper.clean_name("x" * 500)), 120)

    def test_fallback_for_empty_value(self):
        self.assertEqual(mangaripper.clean_name("   ", "chapter"), "chapter")
        self.assertEqual(mangaripper.clean_name("...", "chapter"), "chapter")

    def test_escapes_windows_reserved_names(self):
        self.assertEqual(mangaripper.clean_name("CON"), "_CON")
        self.assertEqual(mangaripper.clean_name("con.txt"), "_con.txt")
        self.assertEqual(mangaripper.clean_name("console"), "console")

    def test_keeps_accents(self):
        self.assertEqual(mangaripper.clean_name("Capítulo 1"), "Capítulo 1")


class UrlTests(unittest.TestCase):
    def test_normalizes_relative_url_and_removes_fragment(self):
        self.assertEqual(
            mangaripper.normalize_url("../pag/01.jpg#top", "https://sitio.example/cap/1/"),
            "https://sitio.example/cap/pag/01.jpg",
        )

    def test_keeps_protocol_relative_url(self):
        self.assertEqual(
            mangaripper.normalize_url("//cdn.example/01.jpg", "https://sitio.example/cap"),
            "https://cdn.example/01.jpg",
        )

    def test_rejects_unsupported_schemes(self):
        for value in ("data:image/png;base64,xx", "blob:http://x", "javascript:void(0)", "ftp://x/y", ""):
            self.assertIsNone(mangaripper.normalize_url(value, "https://sitio.example/"))

    def test_parse_srcset_preserves_declared_order(self):
        self.assertEqual(
            mangaripper.parse_srcset(
                "small.jpg 480w, big.jpg 1200w, blob:https://x/y 1x",
                "https://cdn.example/",
            ),
            ["https://cdn.example/small.jpg", "https://cdn.example/big.jpg"],
        )

    def test_attrs_priority_lazy_then_srcset_then_src(self):
        element = BeautifulSoup(
            '<img data-src="real.jpg" src="placeholder.gif" '
            'srcset="small.jpg 480w, big.jpg 1200w">',
            "html.parser",
        ).img
        self.assertEqual(
            mangaripper.attrs_from_element(element, "https://cdn.example/"),
            [
                "https://cdn.example/real.jpg",
                "https://cdn.example/big.jpg",
                "https://cdn.example/placeholder.gif",
            ],
        )

    def test_attrs_uses_background_image(self):
        element = BeautifulSoup(
            '<div style="background-image: url(\'pagina.jpg\')"></div>',
            "html.parser",
        ).div
        self.assertEqual(
            mangaripper.attrs_from_element(element, "https://cdn.example/"),
            ["https://cdn.example/pagina.jpg"],
        )


class ExtractImageTests(unittest.TestCase):
    def test_prefers_data_src_over_placeholder(self):
        html = """
        <div class="reader-area">
          <img data-src="https://cdn.example/01.jpg" src="https://cdn.example/placeholder.gif">
          <img data-src="https://cdn.example/02.jpg" src="https://cdn.example/placeholder.gif">
        </div>
        """
        urls, title, selector = mangaripper.extract_image_urls(
            html, "https://sitio.example/cap/1", ".reader-area img"
        )
        self.assertEqual(
            urls, ["https://cdn.example/01.jpg", "https://cdn.example/02.jpg"]
        )
        self.assertEqual(title, "chapter")
        self.assertEqual(selector, ".reader-area img")

    def test_auto_detects_reader_selector(self):
        html = """
        <h1>Capítulo 3</h1><title>Capítulo 3</title>
        <div class="reading-content">
          <img data-src="https://cdn.example/01.jpg">
          <img data-src="https://cdn.example/02.jpg">
        </div>
        """
        urls, title, selector = mangaripper.extract_image_urls(
            html, "https://sitio.example/cap/3"
        )
        self.assertEqual(len(urls), 2)
        self.assertEqual(title, "Capítulo 3")
        self.assertEqual(selector, ".reading-content img")

    def test_generic_fallback_filters_decorations(self):
        html = """
        <body>
          <img class="logo" src="https://cdn.example/logo.png">
          <img class="page" src="https://cdn.example/01.jpg">
        </body>
        """
        urls, _title, selector = mangaripper.extract_image_urls(
            html, "https://sitio.example/cap/1"
        )
        self.assertEqual(urls, ["https://cdn.example/01.jpg"])
        self.assertEqual(selector, "img (auto-detection)")

    def test_generic_fallback_keeps_single_page(self):
        html = '<body><img class="page" src="https://cdn.example/01.jpg"></body>'
        urls, _title, selector = mangaripper.extract_image_urls(
            html, "https://sitio.example/cap/1"
        )
        self.assertEqual(urls, ["https://cdn.example/01.jpg"])
        self.assertEqual(selector, "img (auto-detection)")

    def test_selector_can_target_container(self):
        html = """
        <div class="reader-area">
          <img data-src="https://cdn.example/01.jpg">
          <img data-src="https://cdn.example/02.jpg">
        </div>
        """
        urls, _title, selector = mangaripper.extract_image_urls(
            html, "https://sitio.example/cap/1", ".reader-area"
        )
        self.assertEqual(len(urls), 2)
        self.assertEqual(selector, ".reader-area")

    def test_deduplicates_urls(self):
        html = """
        <div class="reader-area">
          <img data-src="https://cdn.example/01.jpg">
          <img src="https://cdn.example/01.jpg">
        </div>
        """
        urls, _title, _selector = mangaripper.extract_image_urls(
            html, "https://sitio.example/cap/1", ".reader-area img"
        )
        self.assertEqual(urls, ["https://cdn.example/01.jpg"])

    def test_invalid_selector_raises_value_error(self):
        with self.assertRaises(ValueError):
            mangaripper.extract_image_urls("<img>", "https://sitio.example/", "[")

    def test_selector_without_matches_raises_value_error(self):
        with self.assertRaises(ValueError):
            mangaripper.extract_image_urls(
                "<img>", "https://sitio.example/", ".no-existe img"
            )


class DecodeHtmlTests(unittest.TestCase):
    def test_utf8(self):
        self.assertIn(
            "Capítulo", mangaripper.decode_html("<title>Capítulo</title>".encode("utf-8"))
        )

    def test_declared_latin1(self):
        html = (
            '<meta charset="iso-8859-1"><title>Capítulo</title>'.encode("latin-1")
        )
        self.assertIn("Capítulo", mangaripper.decode_html(html))

    def test_cp1252_fallback(self):
        self.assertIn("Capítulo", mangaripper.decode_html("Capítulo".encode("cp1252")))

    def test_invalid_bytes_do_not_crash(self):
        self.assertIsInstance(mangaripper.decode_html(b"\xff\xfe<title>x</title>"), str)


class FakeResponse:
    def __init__(self, status: int, chunks: list[bytes], content_type: str = "image/png"):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, _size: int):
        yield from self._chunks


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ReadLimitedTests(unittest.TestCase):
    def test_reads_all_chunks(self):
        response = FakeResponse(200, [b"abc", b"def"])
        self.assertEqual(
            mangaripper.read_limited(response, 100, threading.Event()), b"abcdef"
        )

    def test_rejects_oversized_content(self):
        response = FakeResponse(200, [b"a" * 101])
        with self.assertRaises(ValueError):
            mangaripper.read_limited(response, 100, threading.Event())

    def test_cancel_interrupts_reading(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            mangaripper.read_limited(FakeResponse(200, [b"abc"]), 100, event)


class RequestWithRetriesTests(unittest.TestCase):
    def _call(self, session, **kwargs):
        return mangaripper.request_with_retries(
            session,
            "https://cdn.example/01.jpg",
            {},
            threading.Event(),
            lambda _message: None,
            max_bytes=kwargs.pop("max_bytes", 1024),
            backoff=0,
            **kwargs,
        )

    def test_retries_transient_status(self):
        session = FakeSession(
            [FakeResponse(503, [b"no"]), FakeResponse(200, [b"ok"])]
        )
        content, content_type = self._call(session)
        self.assertEqual(content, b"ok")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(session.calls, 2)

    def test_does_not_retry_definitive_4xx(self):
        session = FakeSession([FakeResponse(404, [b"no"])])
        with self.assertRaises(requests.HTTPError):
            self._call(session)
        self.assertEqual(session.calls, 1)

    def test_exhausts_attempts_on_5xx(self):
        session = FakeSession([FakeResponse(500, [b"no"]) for _ in range(3)])
        with self.assertRaises(requests.HTTPError):
            self._call(session)
        self.assertEqual(session.calls, 3)

    def test_retries_network_errors(self):
        session = FakeSession(
            [requests.ConnectionError("boom"), FakeResponse(200, [b"ok"])]
        )
        content, _content_type = self._call(session)
        self.assertEqual(content, b"ok")
        self.assertEqual(session.calls, 2)

    def test_cancel_before_request(self):
        event = threading.Event()
        event.set()
        session = FakeSession([])
        with self.assertRaises(InterruptedError):
            mangaripper.request_with_retries(
                session,
                "https://cdn.example/01.jpg",
                {},
                event,
                lambda _message: None,
                max_bytes=1024,
                backoff=0,
            )
        self.assertEqual(session.calls, 0)

    def test_size_limit(self):
        session = FakeSession([FakeResponse(200, [b"a" * 1025])])
        with self.assertRaises(ValueError):
            self._call(session, max_bytes=1024)

    def test_reports_details_to_callback(self):
        details: list[str] = []
        session = FakeSession([FakeResponse(200, [b"ok"])])
        content, _content_type = mangaripper.request_with_retries(
            session,
            "https://cdn.example/01.jpg",
            {},
            threading.Event(),
            lambda _message: None,
            max_bytes=1024,
            backoff=0,
            detail=details.append,
        )
        self.assertEqual(content, b"ok")
        joined = "\n".join(details)
        self.assertIn("GET https://cdn.example/01.jpg", joined)
        self.assertIn("HTTP 200", joined)
        self.assertIn("2 bytes", joined)

    def test_details_survive_a_retry(self):
        details: list[str] = []
        session = FakeSession(
            [FakeResponse(503, [b"no"]), FakeResponse(200, [b"ok"])]
        )
        mangaripper.request_with_retries(
            session,
            "https://cdn.example/01.jpg",
            {},
            threading.Event(),
            lambda _message: None,
            max_bytes=1024,
            backoff=0,
            detail=details.append,
        )
        joined = "\n".join(details)
        self.assertIn("attempt 1/3", joined)
        self.assertIn("HTTP 503", joined)
        self.assertIn("attempt 2/3", joined)
        self.assertIn("retryable", joined)


class SaveImageTests(unittest.TestCase):
    def test_preserves_rgba_png(self):
        buffer = BytesIO()
        Image.new("RGBA", (12, 8), (10, 20, 30, 128)).save(buffer, "PNG")
        with TemporaryDirectory() as folder:
            destination = Path(folder) / "01.png"
            size = mangaripper.save_image_as_png(buffer.getvalue(), destination)
            self.assertEqual(size, (12, 8))
            with Image.open(destination) as saved:
                self.assertEqual(saved.format, "PNG")
                self.assertEqual(saved.mode, "RGBA")
                self.assertEqual(saved.size, (12, 8))

    def test_converts_jpeg_to_rgb_png(self):
        buffer = BytesIO()
        Image.new("RGB", (20, 10), "red").save(buffer, "JPEG")
        with TemporaryDirectory() as folder:
            destination = Path(folder) / "01.png"
            size = mangaripper.save_image_as_png(buffer.getvalue(), destination)
            self.assertEqual(size, (20, 10))
            with Image.open(destination) as saved:
                self.assertEqual(saved.format, "PNG")
                self.assertEqual(saved.mode, "RGB")

    def test_applies_exif_orientation(self):
        image = Image.new("RGB", (30, 10), "red")
        exif = image.getexif()
        exif[274] = 6  # rotate 90 degrees
        buffer = BytesIO()
        image.save(buffer, "JPEG", exif=exif)
        with TemporaryDirectory() as folder:
            destination = Path(folder) / "01.png"
            self.assertEqual(
                mangaripper.save_image_as_png(buffer.getvalue(), destination),
                (10, 30),
            )

    def test_rejects_invalid_image(self):
        with TemporaryDirectory() as folder:
            with self.assertRaises(UnidentifiedImageError):
                mangaripper.save_image_as_png(
                    b"no soy una imagen", Path(folder) / "01.png"
                )


class CleanupTempTests(unittest.TestCase):
    def test_removes_temp_directory(self):
        app = mangaripper.MangaripperApp.__new__(mangaripper.MangaripperApp)
        app.worker = None
        app.cancel_event = threading.Event()
        with TemporaryDirectory() as folder:
            app.current_temp_dir = Path(folder)
            (Path(folder) / "001.png").write_bytes(b"x")
            app.cleanup_temp()
            self.assertFalse(Path(folder).exists())
        self.assertIsNone(app.current_temp_dir)

    def test_cancels_and_joins_worker(self):
        app = mangaripper.MangaripperApp.__new__(mangaripper.MangaripperApp)
        app.cancel_event = threading.Event()
        app.current_temp_dir = None
        started = threading.Event()

        def worker():
            started.set()
            app.cancel_event.wait(2)

        app.worker = threading.Thread(target=worker, daemon=True)
        app.worker.start()
        started.wait(1)
        app.cleanup_temp()
        self.assertFalse(app.worker.is_alive())


class I18nTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_every_language_has_the_same_keys(self):
        base_keys = set(i18n.TRANSLATIONS["en"])
        for code, strings in i18n.TRANSLATIONS.items():
            self.assertEqual(set(strings), base_keys, f"language {code}")
        self.assertEqual(set(i18n.LANGUAGES), set(i18n.TRANSLATIONS))

    def test_switches_language(self):
        i18n.set_language("es")
        self.assertEqual(i18n.t("ui.cancel"), "Cancelar")
        self.assertEqual(i18n.t("app.title"), "Mangaripper — Descargador de capítulos")
        i18n.set_language("en")
        self.assertEqual(i18n.t("ui.cancel"), "Cancel")

    def test_unknown_code_falls_back_to_english(self):
        i18n.set_language("xx")
        self.assertEqual(i18n.get_language(), "en")
        self.assertEqual(i18n.t("ui.cancel"), "Cancel")

    def test_missing_key_falls_back_to_the_key(self):
        self.assertEqual(i18n.t("key.that.does.not.exist"), "key.that.does.not.exist")

    def test_interpolates_values(self):
        i18n.set_language("es")
        text = i18n.t("status.downloading", current=2, total=5)
        self.assertIn("página 2 de 5", text)

    def test_current_language_name_is_shown(self):
        code = i18n.get_language()
        self.assertIn(i18n.LANGUAGES[code], list(i18n.LANGUAGES.values()))

    def test_save_and_load_language_roundtrip(self):
        with TemporaryDirectory() as folder:
            config_file = Path(folder) / "settings.json"
            with unittest.mock.patch.object(i18n, "CONFIG_FILE", config_file):
                self.assertEqual(i18n.load_language(), "en")  # no file yet
                i18n.save_language("es")
                self.assertEqual(i18n.load_language(), "es")
                i18n.save_language("xx")  # ignored
                self.assertEqual(i18n.load_language(), "es")
                config_file.write_text("not json", encoding="utf-8")
                self.assertEqual(i18n.load_language(), "en")

    def test_spanish_translates_errors(self):
        i18n.set_language("es")
        with self.assertRaises(ValueError) as context:
            mangaripper.extract_image_urls("<img>", "https://a.example/", "[")
        self.assertIn("Selector CSS no válido", str(context.exception))


class DownloadLogTests(unittest.TestCase):
    def test_writes_header_and_timestamped_details(self):
        with TemporaryDirectory() as folder:
            log = mangaripper.DownloadLog(
                Path(folder), "example.org_cap-1", {"URL": "https://x/1"}
            )
            try:
                log.detail("first line")
            finally:
                log.close()
            text = log.path.read_text(encoding="utf-8")
            self.assertIn("URL: https://x/1", text)
            self.assertIn("first line", text)
            self.assertRegex(text, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\]")
            self.assertTrue(log.path.name.endswith("_example.org_cap-1.log"))

    def test_names_stay_unique_in_the_same_second(self):
        with TemporaryDirectory() as folder:
            first = mangaripper.DownloadLog(Path(folder), "same", {})
            first.close()
            second = mangaripper.DownloadLog(Path(folder), "same", {})
            second.close()
            self.assertNotEqual(first.path, second.path)

    def test_creates_the_directory(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / "nested" / "logs"
            log = mangaripper.DownloadLog(target, "x", {})
            log.close()
            self.assertTrue(target.is_dir())

    def test_context_manager_closes_the_file(self):
        with TemporaryDirectory() as folder:
            with mangaripper.DownloadLog(Path(folder), "ctx", {}) as log:
                log.detail("inside")
            with self.assertRaises(ValueError):
                log.detail("after close")


class DefaultLogDirTests(unittest.TestCase):
    def test_prefers_logs_folder_next_to_the_app(self):
        with TemporaryDirectory() as folder:
            with unittest.mock.patch.object(
                mangaripper, "__file__", str(Path(folder) / "mangaripper.py")
            ):
                directory = mangaripper.default_log_dir()
            self.assertEqual(directory, Path(folder) / "logs")
            self.assertTrue(directory.is_dir())


class ConfigDirTests(unittest.TestCase):
    """default_config_dir() follows the conventions of the running OS."""

    def test_linux_respects_xdg_config_home(self):
        with unittest.mock.patch.object(i18n.platform, "system", return_value="Linux"), \
             unittest.mock.patch.dict(
                 os.environ, {"XDG_CONFIG_HOME": "/tmp/opencode/xdg"}
             ):
            self.assertEqual(
                i18n.default_config_dir(), Path("/tmp/opencode/xdg/mangaripper")
            )

    def test_linux_defaults_to_home_config(self):
        with unittest.mock.patch.object(i18n.platform, "system", return_value="Linux"), \
             unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(
                i18n.default_config_dir(),
                Path.home() / ".config" / "mangaripper",
            )

    def test_windows_uses_localappdata(self):
        with unittest.mock.patch.object(i18n.platform, "system", return_value="Windows"), \
             unittest.mock.patch.dict(
                 os.environ, {"LOCALAPPDATA": r"C:\Users\test\AppData\Local"}
             ):
            directory = i18n.default_config_dir()
            self.assertEqual(directory.parent, Path(r"C:\Users\test\AppData\Local"))
            self.assertEqual(directory.name, "Mangaripper")

    def test_windows_falls_back_to_appdata_path(self):
        with unittest.mock.patch.object(i18n.platform, "system", return_value="Windows"), \
             unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": ""}):
            self.assertEqual(
                i18n.default_config_dir(),
                Path.home() / "AppData" / "Local" / "Mangaripper",
            )

    def test_macos_uses_application_support(self):
        with unittest.mock.patch.object(i18n.platform, "system", return_value="Darwin"):
            self.assertEqual(
                i18n.default_config_dir(),
                Path.home() / "Library" / "Application Support" / "Mangaripper",
            )


if __name__ == "__main__":
    unittest.main()
