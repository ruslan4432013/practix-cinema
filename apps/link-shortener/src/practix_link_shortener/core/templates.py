"""Шаблоны страниц ошибок.

Отдельный модуль, чтобы каталог считался один раз на процесс, а не на запрос, и
чтобы путь до него был вычислен от файла, а не от рабочего каталога: контейнер
запускается из ``/opt/app``, а пакет лежит в site-packages.

Согласование содержимого явное и простое: ``/s/{code}`` всегда отвечает HTML,
``/api/v1/*`` всегда отвечает JSON. Договариваться по ``Accept`` не нужно — одно
правило лучше одного умного правила.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / 'templates'

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
