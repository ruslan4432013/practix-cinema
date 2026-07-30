// Нагрузочный профиль ingest-ручки Analytics Collector.
//
// Смесь запросов подобрана по реальному поведению трекера: кликов и просмотров
// страниц много и они идут по одному, метки прогресса воспроизведения самые
// массовые и уходят пачкой при уходе со страницы (navigator.sendBeacon).
// Один URL, задолбанный в N потоков, такой профиль не воспроизводит.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate } from 'k6/metrics';
import { randomUUID, baseUrl, clientHeaders, sessionId, anonymousId } from './lib/common.js';

const BASE_URL = baseUrl();
const VUS = Number(__ENV.VUS || 100);
const DURATION = __ENV.DURATION || '1m';

// Отдельные метрики: k6 по умолчанию считает 429 такой же ошибкой, как 500, а
// здесь это принципиально разные события — штатный лимит против отказа сервиса.
const serverErrors = new Rate('ingest_server_errors');
const rateLimited = new Counter('ingest_rate_limited');

export const options = {
  scenarios: {
    ingest: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '20s', target: VUS },
        { duration: DURATION, target: VUS },
        { duration: '10s', target: 0 },
      ],
    },
  },
  thresholds: {
    // ГЛАВНЫЙ инвариант сервиса: приём события не отвечает 5xx никогда — даже
    // при недоступной Kafka событие уходит в буфер деградации.
    ingest_server_errors: ['rate==0'],
    // Публикация в буфер продюсера — операция на единицы миллисекунд;
    // всё, что дольше, означает, что упёрлись не в сеть.
    'http_req_duration{expected_response:true}': ['p(95)<200', 'p(99)<500'],
  },
};

function context() {
  return {
    url: `${BASE_URL}/films/${randomUUID()}`,
    locale: 'ru-RU',
    timezone: 'Europe/Moscow',
    screen_width: 1920,
    screen_height: 1080,
  };
}

function clickEvent(sid, aid) {
  return {
    event_id: randomUUID(),
    session_id: sid,
    anonymous_id: aid,
    element_type: 'film_card',
    element_id: `card-${Math.floor(Math.random() * 500)}`,
    target_id: randomUUID(),
    position: Math.floor(Math.random() * 50),
    context: context(),
  };
}

function pageViewEvent(sid, aid) {
  return {
    event_id: randomUUID(),
    session_id: sid,
    anonymous_id: aid,
    page_type: 'film',
    page_url: `${BASE_URL}/films/${randomUUID()}`,
    page_title: 'Film page',
    duration_ms: Math.floor(Math.random() * 120000),
    context: context(),
  };
}

// Пачка меток прогресса — самый массовый поток в системе.
function progressBatch(sid, aid) {
  const filmId = randomUUID();
  const duration = 5400000;
  const events = [];
  for (let i = 0; i < 20; i += 1) {
    events.push({
      event_type: 'video_progress',
      event_id: randomUUID(),
      session_id: sid,
      anonymous_id: aid,
      film_id: filmId,
      playback_position_ms: Math.min(i * 30000, duration),
      duration_ms: duration,
      quality: '1080p',
    });
  }
  return { events };
}

function post(path, body) {
  const res = http.post(`${BASE_URL}${path}`, JSON.stringify(body), {
    headers: clientHeaders(),
    // Тег нужен, чтобы 429 не попадал в порог по длительности успешных ответов.
    tags: { name: path },
  });
  serverErrors.add(res.status >= 500);
  if (res.status === 429) {
    rateLimited.add(1);
  }
  check(res, {
    'принято или отбито лимитом': (r) => r.status === 202 || r.status === 429,
  });
  return res;
}

export default function () {
  const sid = sessionId();
  const aid = anonymousId();

  // Пропорции: на один просмотр страницы приходится несколько кликов, а пачка
  // прогресса уходит примерно раз в сеанс просмотра.
  post('/api/v1/events/click', clickEvent(sid, aid));
  post('/api/v1/events/click', clickEvent(sid, aid));
  post('/api/v1/events/page-view', pageViewEvent(sid, aid));

  if (Math.random() < 0.2) {
    post('/api/v1/events/batch', progressBatch(sid, aid));
  }
}
