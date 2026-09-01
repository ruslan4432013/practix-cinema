// Проверка лимитера выдачи рекомендаций: зона recs_read режет перебор с одного
// адреса и режет корректно.
//
// Зачем отдельный сценарий. Основной профиль (recommendations.js) раскладывает
// проектные 100 RPS по нескольким синтетическим адресам, чтобы мерить сервис, а
// не зону. Из-за этого он проходит с нулём 429 — и «лимитер не мешает» в его
// сводке неотличимо от «лимитера нет вовсе». Здесь наоборот: весь трафик идёт с
// ОДНОГО адреса и заведомо выше 50 r/s, так что 429 обязаны появиться.
//
// Пара прогонов доказывает заодно и подстановку X-Forwarded-For: если бы nginx
// её игнорировал, основной прогон на 100 RPS упёрся бы в ту же зону и тоже дал
// 429. Ноль там и сотни здесь возможны только вместе.
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import { tracedHeaders, syntheticClient, baseUrl } from './lib/common.js';

const BASE_URL = baseUrl();
// Заведомо выше зоны (50 r/s). Burst=100 nodelay поглотит первую сотню, дальше
// начинается отказ.
const LIMITER_RPS = Number(__ENV.LIMITER_RPS || 200);
const DURATION = __ENV.DURATION || '10s';

// Один адрес на весь прогон — в этом вся постановка.
const CLIENT = syntheticClient(Number(__ENV.CLIENT_INDEX || 0));

const rateLimited = new Counter('recs_rate_limited');

export const options = {
  scenarios: {
    flood: {
      executor: 'constant-arrival-rate',
      rate: LIMITER_RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
  thresholds: {
    // Зона действительно режет. Порог намеренно грубый: точная доля зависит от
    // burst и от того, насколько ровно k6 держит интенсивность, а проверяется
    // здесь факт, а не арифметика.
    recs_rate_limited: ['count>100'],
    // Режет корректно: отказ — это 429 от nginx, а не 5xx от сервиса или от
    // самой прокси. http_req_failed здесь порогом не ограничивается: 429 —
    // штатный ответ, и он же цель прогона.
    'checks{check:без 5xx}': ['rate==1.0'],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/api/v1/recommendations/popular?limit=10`, {
    headers: tracedHeaders({ 'X-Forwarded-For': CLIENT }),
  });
  if (res.status === 429) {
    rateLimited.add(1);
  }
  check(res, {
    'без 5xx': (r) => r.status < 500,
    'ответ 200 или 429': (r) => r.status === 200 || r.status === 429,
  });
}
