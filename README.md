# MangaRipper

A Python/Tkinter desktop app that saves the visible pages of a public chapter and packs them into an ordered `.zip` as lossless PNG files: `001.png`, `002.png`, `003.png`, etc.

## Languages

The interface ships in English and Spanish, and you can switch language inside the app with the **Language** dropdown at the top right. The choice is remembered between runs, stored in the user configuration folder for each OS:

| OS | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\Mangaripper\settings.json` |
| macOS | `~/Library/Application Support/Mangaripper/settings.json` |
| Linux / BSD | `$XDG_CONFIG_HOME/mangaripper/settings.json` (or `~/.config/mangaripper/`) |

**Adding a language** is a matter of editing `languages.py`:

1. Add your code to `LANGUAGES`, e.g. `"fr": "Français"`.
2. Add a `TRANSLATIONS["fr"]` dictionary with the same keys as `TRANSLATIONS["en"]`.

Any missing key automatically falls back to English, so a translation can be completed incrementally; the test suite verifies that every language has the full key set.

## Requirements (Ubuntu/Linux)

- Python 3.10 or higher.
- Tkinter usually ships with Python, but if it is missing:

```bash
sudo apt install python3-tk python3-venv
```

From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python mangaripper.py # or simply: mangaripper
```

`requirements.txt` is still available if you prefer to install the dependencies on their own with `pip install -r requirements.txt`.

## Usage

1. Paste the direct chapter URL.
2. Leave the destination folder empty (you will be asked where to save before each download) or pick a folder with **Choose…**.
3. Press **Download and create ZIP**.
4. Pages are automatically converted to PNG and stored in numeric order inside the ZIP.

If automatic detection includes icons or cannot find the pages, try a CSS selector. For example, some readers use `.reader-area img`. The selector depends on the site.

### Sites that load JavaScript

The Chromium checkbox is optional. To use it, install the `browser` extra and Chromium inside the same virtual environment:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

Then reopen the app and tick **Render JavaScript with Chromium**. This only renders the page; it does not solve captchas or bypass access controls.

### PNG conversion and quality

Every page is saved as PNG (`001.png`, `002.png`, etc.). PNG is a lossless format: MangaRipper does not apply a new lossy compression conversion, and it preserves transparency when present. However, converting a lossy JPEG or WebP to PNG **cannot recover details the original file had already lost**; it keeps the decoded pixels from the received file. The PNG file can also take up much more disk space than the original.

The ZIP is created without extra compression (`ZIP_STORED`): PNG files already come compressed, so compressing them again would only burn CPU for almost no gain.

### Retries and limits

- Each download retries up to 3 times on transient errors (timeouts, `429`, `5xx`) with increasing waits. Definitive `4xx` errors are not retried.
- A short pause between pages keeps the server from being hammered.
- Safety limits: 1.000 pages per chapter, 40 MB per image, and 8 MB of HTML.
- Closing the window during a download cancels it and removes the temporary directory.

## Development

The project is packaged with `pyproject.toml` (setuptools), and the `mangaripper` console command is available once installed. To run the tests:

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -t .
# or, if you prefer pytest:
pytest
```

## Limitations

- It is a generic extractor: every site may change its HTML, and not all of them will work.
- It only works with pages and images your browser can reach normally.
- It does not attempt to bypass logins, captchas, DRM, paywalls, or other protections.
- Respect the site's terms and the rights of the content owners.
