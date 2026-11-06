# MangaRipper

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](https://unlicense.org)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Public domain](https://img.shields.io/badge/copyright-none-brightgreen.svg)](#license--no-copyright)

> **Made with AI · 100% open source · Public domain**
>
> MangaRipper was built with the help of an AI assistant. It is released under
> [The Unlicense](LICENSE): **there is no copyright, no license agreement to accept
> and no conditions.** Copy it, fork it, modify it, sell it, repackage it, remove the
> name — do whatever you like with it, for any purpose.

A small Python/Tkinter desktop app that saves the pages of a public chapter and packs
them into an ordered `.zip` as lossless PNG files: `001.png`, `002.png`, `003.png`…

![Python](https://img.shields.io/badge/GUI-Tkinter-informational)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

## Features

- **Simple GUI** — paste a chapter URL, press one button, get a ZIP.
- **Lossless PNG pages** — no extra lossy recompression, transparency preserved.
- **Ordered output** — pages are numbered `001.png`, `002.png`, … inside the ZIP.
- **English and Spanish** — switch language live from the app; easy to extend.
- **Empty destination by default** — it asks where to save when you start a download.
- **Optional JavaScript rendering** — Chromium/Playwright support for sites that need it.
- **Retries and safety limits** — transient errors are retried; downloads are bounded.
- **Multi-OS** — Windows, macOS and Linux/BSD, including per-OS config folders.
- **No telemetry** — the app only talks to the chapter site you give it.

## Install

Requires **Python 3.10+** and Tkinter.

### Option A — download the release ZIP

Grab the latest `Mangaripper-<version>.zip` from the
[Releases](https://github.com/Sugus1221/MangaRipper/releases) page, unzip it anywhere
and run:

```bash
python mangaripper.py
```

### Option B — clone and install

```bash
git clone https://github.com/Sugus1221/MangaRipper.git
cd MangaRipper
python -m venv .venv
```

Activate the environment:

```bash
# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Windows (cmd)
.venv\Scripts\activate.bat
```

Then install and run:

```bash
python -m pip install -e .
python mangaripper.py     # or simply: mangaripper
```

If Tkinter is missing on Linux:

```bash
sudo apt install python3-tk python3-venv
```

`requirements.txt` is available if you prefer installing the dependencies on their own
with `pip install -r requirements.txt`.

## Usage

1. Paste the direct chapter URL.
2. Leave the destination folder empty (you will be asked where to save before each
   download) or pick a folder with **Choose…**.
3. Press **Download and create ZIP**.
4. Pages are automatically converted to PNG and stored in numeric order inside the ZIP.

If automatic detection includes icons or cannot find the pages, try a CSS selector.
For example, some readers use `.reader-area img`. The selector depends on the site.

### Sites that load JavaScript

The Chromium checkbox is optional. To use it, install the `browser` extra and Chromium
inside the same virtual environment:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

Then reopen the app and tick **Render JavaScript with Chromium**. This only renders the
page; it does not solve captchas or bypass access controls.

### PNG conversion and quality

Every page is saved as PNG (`001.png`, `002.png`, etc.). PNG is a lossless format:
MangaRipper does not apply a new lossy compression conversion, and it preserves
transparency when present. However, converting a lossy JPEG or WebP to PNG **cannot
recover details the original file had already lost**; it keeps the decoded pixels from
the received file. The PNG file can also take up much more disk space than the original.

The ZIP is created without extra compression (`ZIP_STORED`): PNG files already come
compressed, so compressing them again would only burn CPU for almost no gain.

### Retries and limits

- Each download retries up to 3 times on transient errors (timeouts, `429`, `5xx`) with
  increasing waits. Definitive `4xx` errors are not retried.
- A short pause between pages keeps the server from being hammered.
- Safety limits: 1.000 pages per chapter, 40 MB per image, and 8 MB of HTML.
- Closing the window during a download cancels it and removes the temporary directory.

### Detailed logs

Every download writes its own log file into the `logs/` folder next to the
application (one file per download: `YYYY-MM-DD_HH-MM-SS_<site>_<chapter>.log`).
The log is highly detailed and timestamped, recording:

- Where the download started: URL, destination folder, ZIP name, selector,
  JavaScript mode, language, platform, Python/requests/Pillow versions.
- Every HTTP request: attempt number, status code, `Content-Type`,
  `Content-Length`, `Content-Encoding`, elapsed time and bytes received.
- The detected title, selector and the complete list of image URLs.
- Per page: path saved, pixel size, bytes on disk and per-page time.
- Retries with their waits, and full tracebacks when something fails.
- The final ZIP: path, size, entry count and total elapsed time.

If the application is installed as a package and the project folder is
read-only, logs fall back to the user data folder:
`%LOCALAPPDATA%\Mangaripper\logs` (Windows),
`~/Library/Application Support/Mangaripper/logs` (macOS) or
`$XDG_DATA_HOME/Mangaripper/logs` (Linux/BSD).

The `logs/` folder is ignored by git: logs stay on your machine.

## Languages

The interface ships in English and Spanish. Switch language inside the app with the
**Language** dropdown at the top right; the choice is remembered between runs in the
user configuration folder:

| OS | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\Mangaripper\settings.json` |
| macOS | `~/Library/Application Support/Mangaripper/settings.json` |
| Linux / BSD | `$XDG_CONFIG_HOME/mangaripper/settings.json` (or `~/.config/mangaripper/`) |

**Adding a language** is a matter of editing `languages.py`:

1. Add your code to `LANGUAGES`, e.g. `"fr": "Français"`.
2. Add a `TRANSLATIONS["fr"]` dictionary with the same keys as `TRANSLATIONS["en"]`.

Any missing key automatically falls back to English, so a translation can be completed
incrementally; the test suite verifies that every language has the full key set.

## Development

The project is packaged with `pyproject.toml` (setuptools), and the `mangaripper`
console command is available once installed. To run the tests:

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -t .
# or, if you prefer pytest:
pytest
```

Project layout:

```
mangaripper.py      # the application: GUI, downloader, ZIP writer
languages.py        # translations and per-OS config handling
tests/              # unittest suite
LICENSE             # The Unlicense (public domain)
```

## Limitations

- It is a generic extractor: every site may change its HTML, and not all of them will
  work.
- It only works with pages and images your browser can reach normally.
- It does not attempt to bypass logins, captchas, DRM, paywalls, or other protections.
- Respect the site's terms and the rights of the content owners.

## License — no copyright

This is free and unencumbered software released into the **public domain** under
[The Unlicense](https://unlicense.org). There is **no copyright** and **no restriction**:
you can use, copy, modify, merge, publish, distribute, sublicense, sell and/or do
whatever you want with this software, with or without attribution, for commercial or
non-commercial purposes.

The software is provided **“as is”**, without warranty of any kind. See [LICENSE](LICENSE)
for the full text.

Made with AI. Use it, break it, improve it — it is yours now.
