# Changelog

Все значимые изменения сервиса auth фиксируются в этом файле.
Формат соответствует [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [Unreleased] — 2026-07-01

### Added
- Эндпоинт `GET /api/v1/oauth/social/accounts` — список соцсетей,
  привязанных к аккаунту текущего пользователя.
- Эндпоинт `DELETE /api/v1/oauth/social/accounts/{account_id}` — открепление
  соцсети от аккаунта. Проверяется владение записью; открепление последнего
  способа входа у пользователя без пароля запрещено (ответ `409`), чтобы
  не заблокировать вход в аккаунт.
- Методы сервиса `OAuthService.list_social_accounts` и
  `OAuthService.unlink_social_account`.
- Схема `SocialAccountResponse`.

## [Unreleased] — 2026-06-15

### Added
- Эндпоинт `GET /api/v1/users/me` — получение профиля текущего пользователя.
- Эндпоинт `PATCH /api/v1/users/me/credentials` — смена логина и/или email
  с подтверждением текущим паролем и проверкой уникальности.
- Эндпоинт `POST /api/v1/users/check-permissions` — межсервисная проверка
  валидности access-токена и наличия требуемых ролей у пользователя.
- Метод сервиса `AuthService.change_credentials` для смены логина/email.
- Bypass проверки прав в `role_required` для суперпользователя
  (роли `admin` и `superuser` получают доступ ко всем защищённым ручкам).
- README.md и CHANGELOG.md для сервиса auth.
- Расширенный набор функциональных тестов: профиль `/me`, смена email,
  проверка прав через `/check-permissions`, bypass админа.

### Changed
- CLI-команда создания суперпользователя переименована в `createsuperuser`
  (PEP 8 / Django-стиль), сделана идемпотентной, добавлено подтверждение
  пароля; повторный запуск повышает существующего пользователя до админа.
- В `dependencies.py` константа `SUPERUSER_ROLES = {"admin", "superuser"}`
  используется в `role_required` и в `/check-permissions`.

### Removed
- Удалены устаревшие заглушки в `api/v1/users.py`
  (`POST /{user_id}/roles/{role_id}` и `DELETE /{user_id}/roles/{role_id}`):
  их функциональность полностью покрывается `POST /api/v1/roles/assign`
  и `POST /api/v1/roles/remove`.
- Удалён `traceback.print_exc()` из обработчика регистрации.
- Убраны неиспользуемые импорты (`uuid` в `auth_service.py`,
  локальные `from fastapi import HTTPException` в `roles.py`).
