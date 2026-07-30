"""Подключение общих фикстур набора Movies API.

Фикстуры переехали в ``practix_testing`` (библиотека), поэтому дотнотация больше
не требует корня репозитория на ``sys.path`` — раньше именно из-за этого сервису
``tests`` в compose приходилось задавать ``PYTHONPATH=/opt/app/rest:/``.
"""

pytest_plugins = (
    'practix_testing.fixtures.common',
    'practix_testing.fixtures.elasticsearch',
    'practix_testing.fixtures.redis',
    'practix_testing.fixtures.http',
)
