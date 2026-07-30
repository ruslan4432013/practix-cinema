-- Выполняется в theatre-db ПОСЛЕ загрузки database_dump.sql (только при
-- инициализации пустого тома). Дамп содержит системные таблицы Django и историю
-- миграций под СТАНДАРТНУЮ модель пользователя, что несовместимо с кастомной
-- моделью users.User. Здесь мы удаляем эти системные таблицы и их записи в
-- django_migrations, чтобы `migrate` пересоздал их согласованно с новой моделью.
-- Данные контента (схема content) и записи миграций приложения movies остаются
-- нетронутыми, поэтому контент не пересоздаётся и не теряется.

DROP TABLE IF EXISTS public.django_admin_log CASCADE;
DROP TABLE IF EXISTS public.auth_user_groups CASCADE;
DROP TABLE IF EXISTS public.auth_user_user_permissions CASCADE;
DROP TABLE IF EXISTS public.auth_group_permissions CASCADE;
DROP TABLE IF EXISTS public.auth_permission CASCADE;
DROP TABLE IF EXISTS public.auth_group CASCADE;
DROP TABLE IF EXISTS public.auth_user CASCADE;
DROP TABLE IF EXISTS public.django_session CASCADE;
DROP TABLE IF EXISTS public.django_content_type CASCADE;

DELETE FROM public.django_migrations
 WHERE app IN ('admin', 'auth', 'contenttypes', 'sessions');
