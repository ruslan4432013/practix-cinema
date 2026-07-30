"""Делает `tools/` импортируемым для тестов.

Скрипты в `tools/` — консольные утилиты для CI и разработчика, а не пакет: их
запускают по пути (`python tools/notify_telegram.py`), и распространять их
незачем. Правка `sys.path` — цена этого решения, и она ограничена тестами.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
