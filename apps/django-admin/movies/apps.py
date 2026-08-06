from django.apps import AppConfig


class MoviesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'movies'

    def ready(self) -> None:
        # Импорт ради побочного эффекта: декоратор @receiver регистрирует
        # обработчик post_save. Другого места для этого у Django нет — модуль
        # сигналов, не импортированный отсюда, просто никогда не выполнится.
        from movies import signals  # noqa: F401
