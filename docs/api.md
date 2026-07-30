# Ключевые эндпоинты

Полные контракты — в Swagger каждого сервиса: Movies API `/api/openapi`,
Analytics Collector `/api/analytics/openapi`, UGC API `/api/ugc/openapi`,
Auth — `/api/openapi` его контейнера.

## Movies API — `/api/v1`

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/api/v1/films` | Список фильмов (сортировка, фильтр по жанру) |
| GET | `/api/v1/films/search` | Полнотекстовый поиск фильмов |
| GET | `/api/v1/films/{id}` | Детальная карточка (детали гейтятся подпиской через Auth) |
| GET | `/api/v1/genres` / `/api/v1/genres/{id}` | Жанры |
| GET | `/api/v1/persons/search` | Полнотекстовый поиск персон |
| GET | `/api/v1/persons/{id}` | Персона с фильмографией |

## Auth — `/api/v1/auth`

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/register` | Регистрация |
| POST | `/login` | Вход (access + refresh) |
| POST | `/refresh` | Обновление (ротация refresh-токена) |
| POST | `/logout` | Выход из текущей сессии |
| POST | `/logout-all` | Выход из всех сессий |
| POST | `/change-password` | Смена пароля |
| GET | `/login-history` | История входов |

### Users — `/api/v1/users`

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/me` | Текущий пользователь |
| PATCH | `/me/credentials` | Изменение логина/email |
| POST | `/check-permissions` | Межсервисная проверка прав |

### Roles — `/api/v1/roles` (только admin)

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/` | Создать роль |
| GET | `/` | Список ролей |
| POST | `/assign` | Назначить роль пользователю |
| POST | `/remove` | Отобрать роль |
| PATCH | `/{id}` | Изменить роль |
| DELETE | `/{id}` | Удалить роль |

### OAuth — `/api/v1/oauth` (при `OAUTH_ENABLED`)

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/yandex/login` | Редирект на Yandex |
| GET | `/yandex/callback` | Callback: выпуск наших JWT |
| GET | `/social/accounts` | Список привязанных соцаккаунтов |
| DELETE | `/social/accounts/{account_id}` | Отвязать соцаккаунт |

## Analytics Collector — `/api/v1/events` (аутентификация опциональна)

| Метод | URL | Описание |
|-------|-----|----------|
| POST | `/click` | Клик по элементу интерфейса |
| POST | `/page-view` | Просмотр страницы и время на ней |
| POST | `/custom` | Кастомное событие (тип в поле `event_type`) |
| POST | `/batch` | Пачка до 50 событий любых типов (для `sendBeacon`) |
| GET | `/health/live` · `/health/ready` | Пробы живости и готовности |
| GET | `/metrics` | Метрики Prometheus |

Все ручки приёма отвечают **202 Accepted**; поле `status` в ответе показывает судьбу события: `accepted` / `buffered` / `duplicate` / `dropped`. Swagger — на `/api/analytics/openapi` (не `/api/openapi`: тот занят Movies API).

## UGC API — `/api/v1/{likes,ratings,bookmarks,reviews}`

| Метод | URL | Описание |
|-------|-----|----------|
| PUT · DELETE | `/likes/{film_id}` | Поставить/изменить и снять оценку 0..10 |
| GET | `/likes/me` | Понравившиеся фильмы пользователя |
| GET | `/ratings/{film_id}` | Рейтинг фильма: среднее, гистограмма, лайки и дизлайки по порогу |
| PUT · DELETE | `/bookmarks/{film_id}` | Закладка «посмотреть позже», идемпотентно |
| GET | `/bookmarks` | Закладки пользователя |
| POST | `/reviews` | Написать рецензию (одна на фильм от пользователя) |
| GET | `/reviews?film_id=…&sort=new\|useful\|rating` | Рецензии фильма в трёх порядках |
| PUT · DELETE | `/reviews/{review_id}/vote` | Голос за полезность и его отзыв |

Запись требует токена; `user_id` берётся только из подписи и в теле запроса
запрещён. Swagger — на `/api/ugc/openapi`.
