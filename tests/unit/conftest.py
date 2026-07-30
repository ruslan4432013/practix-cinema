"""Настройка окружения юнит-тестов.

Пакеты сервиса импортируются от корня ``src`` (``from core.privacy import ...``),
как это устроено в самом сервисе через ``PYTHONPATH``. Здесь путь подставляется
программно, чтобы `pytest tests/unit` работал без обёртки из переменных
окружения.

``UGC_ENV=test`` выставляется до импорта настроек: при ``prod`` конфигурация
отказывается стартовать с секретами по умолчанию, а юнит-тесты как раз работают
с ними.
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UGC_SRC = _REPO_ROOT / 'analytics_collector' / 'src'

if str(_UGC_SRC) not in sys.path:
    sys.path.insert(0, str(_UGC_SRC))

os.environ.setdefault('UGC_ENV', 'test')
# Настройки читают .env, если он есть в рабочем каталоге. Для юнит-тестов это
# источник нестабильности: результат зависел бы от локального файла
# разработчика. Пустое значение отключает поиск файла в pydantic-settings.
os.environ.setdefault('UGC_IP_HASH_SALT', 'unit-test-salt')
