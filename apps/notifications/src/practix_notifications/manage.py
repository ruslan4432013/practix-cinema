"""Точка входа Django-команд: ``python -m practix_notifications.manage <команда>``.

Файл лежит ВНУТРИ пакета, а не рядом с ``pyproject.toml``, как обычный
``manage.py``. Причина — инвариант репозитория «нет PYTHONPATH, точки входа
полностью квалифицированы» (CLAUDE.md): все шесть сервисов запускаются как
``uvicorn practix_x.main:app`` или ``python -m practix_x.main``, и седьмой не
должен зависеть от текущего каталога.

Этой же командой запускаются воркер (``run_worker``) и планировщик
(``run_scheduler``) — см. профиль ``notifications`` в compose.
"""

import os
import sys


def main() -> None:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'practix_notifications.settings')
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
