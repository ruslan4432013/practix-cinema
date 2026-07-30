// Нагрузочный профиль Movies API: чтение каталога и поиск.
//
// Профиль сознательно смешивает попадания в кэш и промахи: список фильмов с
// одними и теми же параметрами кэшируется в Redis, а поиск со случайным
// запросом каждый раз уходит в Elasticsearch. Замер только по одному из них
// говорит либо про Redis, либо про ES, но не про сервис.
import http from 'k6/http';
import { check, group } from 'k6';
import { tracedHeaders, baseUrl } from './lib/common.js';

const BASE_URL = baseUrl();
const VUS = Number(__ENV.VUS || 200);
const DURATION = __ENV.DURATION || '1m';

const SEARCH_TERMS = ['star', 'war', 'love', 'dark', 'night', 'city', 'lost', 'blue'];

export const options = {
  scenarios: {
    read: {
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
    http_req_failed: ['rate<0.01'],
    // Ориентир взят из прежних замеров wrk: при <=500 соединениях p99 < 200 мс.
    'http_req_duration{scenario:read}': ['p(95)<200', 'p(99)<500'],
  },
};

export default function () {
  const headers = tracedHeaders();

  group('каталог (попадание в кэш)', () => {
    const res = http.get(`${BASE_URL}/api/v1/films?page_size=50&page_number=1`, { headers });
    check(res, { 'список фильмов 200': (r) => r.status === 200 });
  });

  group('поиск (промах кэша, идёт в ES)', () => {
    const term = SEARCH_TERMS[Math.floor(Math.random() * SEARCH_TERMS.length)];
    const res = http.get(`${BASE_URL}/api/v1/films/search?query=${term}&page_size=20`, { headers });
    check(res, { 'поиск 200': (r) => r.status === 200 });
  });

  group('жанры', () => {
    const res = http.get(`${BASE_URL}/api/v1/genres`, { headers });
    check(res, { 'жанры 200': (r) => r.status === 200 });
  });
}
