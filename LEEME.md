# MangaRipper

Aplicación gráfica en Python/Tkinter para intentar guardar las imágenes visibles de un capítulo público y agruparlas en un `.zip` con nombres ordenados en PNG: `001.png`, `002.png`, `003.png`, etc.

## Requisitos (Ubuntu/Linux)

- Python 3.10 o superior.
- Tkinter suele venir con Python, pero si falta:

```bash
sudo apt install python3-tk python3-venv
```

Desde la carpeta del proyecto:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python mangazip.py     # o simplemente: mangazip
```

`requirements.txt` sigue disponible si prefieres instalar las dependencias sueltas con `pip install -r requirements.txt`.

## Uso

1. Pega la URL directa del capítulo.
2. Elige la carpeta de destino.
3. Pulsa **Descargar y crear ZIP**.
4. Las páginas se convierten automáticamente a PNG y se guardan en orden numérico dentro del ZIP.

Si la detección automática incluye iconos o no encuentra las páginas, prueba un selector CSS. Por ejemplo, en algunos lectores puede ser `.reader-area img`. El selector depende de la página.

### Páginas que cargan JavaScript

La casilla de Chromium es opcional. Para usarla instala el extra `browser` y Chromium dentro del mismo entorno virtual:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

Después vuelve a abrir el programa y marca **Renderizar JavaScript con Chromium**. Esto solo renderiza la página; no resuelve captchas ni evita controles de acceso.

### Conversión a PNG y calidad

Todas las páginas se guardan como PNG (`001.png`, `002.png`, etc.). PNG es un formato sin pérdida: MangaRipper no aplica una nueva compresión con pérdida al convertirlas y conserva la transparencia cuando existe. Sin embargo, convertir un JPEG o WebP con pérdida a PNG **no puede recuperar detalles que la imagen original ya hubiera perdido**; conserva los píxeles que se decodifican desde el archivo recibido. El archivo PNG puede ocupar bastante más espacio que el original.

El ZIP se crea sin compresión adicional (`ZIP_STORED`): los PNG ya vienen comprimidos y volver a comprimirlos solo consumiría CPU sin reducir el tamaño de forma apreciable.

### Reintentos y límites

- Cada descarga reintenta hasta 3 veces los errores transitorios (timeouts, `429`, `5xx`) con una espera creciente. Los `4xx` definitivos no se reintentan.
- Se hace una pausa breve entre páginas para no saturar al servidor.
- Límites de seguridad: 1.000 páginas por capítulo, 40 MB por imagen y 8 MB de HTML.
- Al cerrar la ventana durante una descarga se cancela el proceso y se elimina el directorio temporal.

## Desarrollo

El proyecto se empaqueta con `pyproject.toml` (setuptools) y el comando `mangazip` queda disponible al instalarlo. Para ejecutar las pruebas:

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -t .
# o, si prefieres pytest:
pytest
```

## Limitaciones

- Es un extractor genérico: cada web puede cambiar su HTML y no todas funcionarán.
- Solo funciona con páginas e imágenes a las que tu navegador pueda acceder normalmente.
- No intenta saltarse logins, captchas, DRM, muros de pago ni otras protecciones.
- Respeta las condiciones del sitio y los derechos del titular del contenido.
