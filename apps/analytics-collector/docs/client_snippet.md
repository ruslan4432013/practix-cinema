# Клиентский трекер

Референсная реализация браузерного трекера. Показывает, как сервис
предполагается использовать с фронтенда, и объясняет решения, которые неочевидны
до столкновения с реальным поведением браузеров.

Код ниже — пример для документации, а не собираемый артефакт: в проекте нет
фронтенда, куда его можно было бы подключить.

---

## Что здесь важно

**Просмотр страницы отправляется дважды.** Один раз при открытии (факт
просмотра) и один раз при уходе — уже с `duration_ms`. Иначе время на странице
измерить нечем: в момент открытия оно ещё неизвестно.

**Уход со страницы ловится по `visibilitychange`, а не по `beforeunload`.**
Мобильные браузеры (в первую очередь Safari на iOS) не гарантируют вызов
`beforeunload` при сворачивании вкладки или переключении приложения — а на
мобильных это основной способ «уйти со страницы». Событие
`visibilitychange → hidden` срабатывает надёжно.

**Отправка при уходе — через `navigator.sendBeacon`.** Обычный `fetch` при
выгрузке страницы браузер вправе отменить. `sendBeacon` передаёт запрос
браузеру, и тот доставляет его уже после закрытия вкладки.

Отсюда два следствия, под которые подстроен сервер:

* `sendBeacon` **не умеет ставить произвольные заголовки** — ни
  `Authorization`, ни `X-Request-Id`. Поэтому ingest-ручки не требуют
  `X-Request-Id` (в отличие от `rest/` и `auth/`, где его отсутствие даёт 400),
  а аутентификация опциональна. Beacon-события приходят анонимными — это
  осознанная плата за то, чтобы они приходили вообще.
* `sendBeacon` отправляет тело с типом `text/plain`, если не использовать
  `Blob`. Пример ниже явно оборачивает тело в `Blob` с
  `type: 'application/json'`.

**События копятся в очереди и уходят пачкой.** Клик на каждый элемент — это
запрос на каждый клик; при активном пользователе их десятки в минуту. Очередь с
отправкой по таймеру или по достижении размера пачки снижает число запросов на
порядок. Ограничение пачки (50 событий) совпадает с серверным `UGC_MAX_BATCH_SIZE`.

**`anonymous_id` живёт в `localStorage`, `session_id` — в `sessionStorage`.**
Первый должен переживать закрытие браузера (по нему связываются визиты одного
пользователя), второй — жить ровно одну вкладку-сессию.

---

## Метки прогресса просмотра

`video_progress` — самый массовый поток в системе, и обращаться с ним нужно
аккуратнее остальных событий. Правила, заложенные в трекер:

* **Тик раз в 30 секунд.** Чаще не нужно: кривая досмотра строится по бакетам с
  шагом 5 % длительности, и более частые метки только увеличат объём.
* **Молчать на паузе и при скрытой вкладке.** Иначе фильм, оставленный на паузе
  в фоновой вкладке, породит метки за всю ночь и попадёт в «досмотренные».
* **Слать пачками через `/api/v1/events/batch`.** Отдельный HTTP-запрос на
  каждый тик расточителен: у активного зрителя это по два запроса в минуту на
  каждую открытую вкладку с плеером.
* **`event_id` считать детерминированно** от `(session_id, film_id, номер
  бакета)`. Тогда ретрай сети, повторный тик после перемотки назад или
  дублирующая отправка из очереди не создадут дубликат ни в Redis-дедупликации
  коллектора, ни в аналитическом хранилище.
* **Долю просмотра не присылать** — её считает сервер из `playback_position_ms`
  и `duration_ms`. Присланному клиентом проценту доверять нельзя.

Порядок объёма: миллион одновременных зрителей при тике раз в 30 секунд — это
около 33 тысяч событий в секунду. Отсюда отдельный топик с 12 партициями и
семидневным хранением, см. [`kafka_topics.md`](kafka_topics.md).

---

## Реализация

```javascript
(function () {
  'use strict';

  const ENDPOINT = '/api/v1/events';
  const BATCH_SIZE = 50;          // совпадает с серверным UGC_MAX_BATCH_SIZE
  const FLUSH_INTERVAL_MS = 5000;

  // --- идентификаторы -----------------------------------------------------
  const storageId = (storage, key) => {
    let value = storage.getItem(key);
    if (!value) {
      value = crypto.randomUUID();
      storage.setItem(key, value);
    }
    return value;
  };

  const anonymousId = storageId(localStorage, 'ugc_anonymous_id');
  const sessionId = storageId(sessionStorage, 'ugc_session_id');

  // Токен доступа, если пользователь вошёл. Ставится приложением после логина.
  const accessToken = () => localStorage.getItem('access_token');

  const pageContext = () => ({
    url: location.href,
    referrer: document.referrer || null,
    screen_width: screen.width,
    screen_height: screen.height,
    viewport_width: window.innerWidth,
    viewport_height: window.innerHeight,
    locale: navigator.language,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  });

  // --- очередь ------------------------------------------------------------
  let queue = [];

  const enqueue = (event) => {
    queue.push({
      // event_id генерируется на клиенте: он же ключ идемпотентности, и при
      // повторной отправке пачки сервер погасит дубликат.
      event_id: crypto.randomUUID(),
      event_timestamp: new Date().toISOString(),
      session_id: sessionId,
      anonymous_id: anonymousId,
      context: pageContext(),
      ...event,
    });
    if (queue.length >= BATCH_SIZE) flush();
  };

  const flush = (useBeacon = false) => {
    if (queue.length === 0) return;
    const batch = queue.splice(0, BATCH_SIZE);
    const body = JSON.stringify({ events: batch });
    const url = `${ENDPOINT}/batch`;

    if (useBeacon) {
      // Blob нужен, чтобы Content-Type был application/json:
      // по умолчанию sendBeacon отправляет text/plain.
      navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
      return;
    }

    const headers = { 'Content-Type': 'application/json' };
    const token = accessToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;

    fetch(url, { method: 'POST', headers, body, keepalive: true })
      .catch(() => {
        // Сеть недоступна — возвращаем события в очередь, но не даём ей расти
        // бесконечно: при долгом офлайне память вкладки важнее полноты данных.
        queue = batch.concat(queue).slice(0, BATCH_SIZE * 4);
      });
  };

  setInterval(() => flush(false), FLUSH_INTERVAL_MS);

  // --- просмотр страницы и время на ней ------------------------------------
  let pageEnteredAt = Date.now();
  let pageViewSent = false;

  const trackPageView = (pageType, entityId = null) => {
    pageEnteredAt = Date.now();
    pageViewSent = true;
    enqueue({
      event_type: 'page_view',
      page_type: pageType,
      page_url: location.href,
      page_title: document.title,
      entity_id: entityId,
    });
  };

  const trackPageLeave = () => {
    if (!pageViewSent) return;
    enqueue({
      event_type: 'page_view',
      page_type: document.body.dataset.pageType || 'other',
      page_url: location.href,
      duration_ms: Date.now() - pageEnteredAt,
    });
    // Уход со страницы: только sendBeacon переживёт закрытие вкладки.
    flush(true);
  };

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') trackPageLeave();
    else pageEnteredAt = Date.now();
  });

  // --- клики: одно делегирование вместо обработчика на каждый элемент ------
  // Элементы размечаются атрибутами: data-ugc-element="film" data-ugc-id="<uuid>"
  document.addEventListener(
    'click',
    (event) => {
      const target = event.target.closest('[data-ugc-element]');
      if (!target) return;
      enqueue({
        event_type: 'click',
        element_type: target.dataset.ugcElement,
        element_id: target.id || null,
        element_text: (target.textContent || '').trim().slice(0, 256),
        target_id: target.dataset.ugcId || null,
        page_url: location.href,
        position: target.dataset.ugcPosition ? Number(target.dataset.ugcPosition) : null,
      });
    },
    { capture: true },
  );

  // --- метки прогресса просмотра ------------------------------------------
  // По этим меткам аналитика строит кривую досмотра: у брошенного просмотра
  // события video_completed не существует, и последняя метка — единственный
  // способ узнать, где зритель ушёл.
  const PROGRESS_TICK_MS = 30_000;

  function attachProgressTracker(video, filmId) {
    let lastBucket = -1;

    const tick = () => {
      // Не шлём на паузе и при скрытой вкладке: это не просмотр, а метки
      // копились бы всё время, пока вкладка висит в фоне.
      if (video.paused || document.visibilityState !== 'visible') return;

      const durationMs = Math.round(video.duration * 1000);
      if (!durationMs) return;
      const positionMs = Math.round(video.currentTime * 1000);

      // Номер 30-секундного бакета. Он же делает event_id детерминированным:
      // ретрай сети или повторный тик после перемотки назад не создадут
      // дубль ни в Redis-дедупликации коллектора, ни в ClickHouse.
      const bucket = Math.floor(positionMs / PROGRESS_TICK_MS);
      if (bucket === lastBucket) return;
      lastBucket = bucket;

      enqueue({
        event_id: uuidV5Like(`${sessionId}:${filmId}:${bucket}`),
        event_type: 'video_progress',
        film_id: filmId,
        playback_position_ms: positionMs,
        duration_ms: durationMs,
        quality: currentQuality || null,
        is_paused: false,
      });
    };

    const timer = setInterval(tick, PROGRESS_TICK_MS);
    video.addEventListener('ended', () => clearInterval(timer));
    return () => clearInterval(timer);
  }

  // --- публичный API для приложения ---------------------------------------
  window.ugc = {
    trackPageView,

    trackQualityChange: (filmId, from, to, positionMs, isAuto = false) =>
      enqueue({
        event_type: 'video_quality_change',
        film_id: filmId,
        from_quality: from,
        to_quality: to,
        playback_position_ms: positionMs,
        is_auto: isAuto,
      }),

    // Метки прогресса просмотра. Самый массовый поток, поэтому у него свои
    // правила — см. раздел «Метки прогресса» ниже.
    trackProgress: attachProgressTracker,

    trackVideoCompleted: (filmId, durationMs, watchedMs, quality = null) =>
      enqueue({
        event_type: 'video_completed',
        film_id: filmId,
        duration_ms: durationMs,
        watched_ms: watchedMs,
        quality,
      }),

    trackSearchFilters: (query, filters, resultsCount) =>
      enqueue({
        event_type: 'search_filter_used',
        query,
        // filters: [{ field: 'genre', value: 'sci-fi' }, ...]
        filters,
        results_count: resultsCount,
      }),

    flush: () => flush(false),
  };
})();
```

---

## Подключение

```html
<body data-page-type="film">
  <div class="card"
       data-ugc-element="film"
       data-ugc-id="3d825f60-9fff-4dfe-b294-1a45fa1e115d"
       data-ugc-position="3">…</div>

  <script src="/static/js/ugc-tracker.js"></script>
  <script>
    ugc.trackPageView('film', '3d825f60-9fff-4dfe-b294-1a45fa1e115d');

    const player = document.querySelector('video');
    player.addEventListener('ended', () => {
      ugc.trackVideoCompleted(filmId, player.duration * 1000, player.currentTime * 1000);
    });
  </script>
</body>
```

## Что стоит доработать в реальном фронтенде

* **Досмотр до конца по `ended` фиксирует не всё.** Пользователь, домотавший до
  титров и закрывший вкладку, события не даст. Практичнее считать досмотром
  достижение порога (90–95 % длительности) по `timeupdate`. Витрина
  `film_completion_daily` в хранилище так и устроена: «досмотрел» — это
  `completion_rate >= 0.9`, а не факт события `video_completed`.
* **`uuidV5Like` в примере — заглушка.** Нужна детерминированная функция от
  строки, дающая валидный UUID (например, SHA-1 от
  `session_id:film_id:bucket`, приведённый к формату UUID v5). Без неё
  дедупликация меток прогресса по `event_id` не работает.
* **Офлайн-очередь в памяти теряется при закрытии вкладки.** Для полноты данных
  очередь стоит хранить в `IndexedDB` и досылать при следующем открытии.
* **Согласие на сбор данных.** Трекер должен молчать, пока пользователь не дал
  согласие, и уметь выключаться по `Do Not Track` / `Global Privacy Control`.
