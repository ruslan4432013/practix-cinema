// Нагрузочный профиль Auth: регистрация, вход и обновление токенов.
//
// Нагружать вход осмысленно именно здесь: хеширование пароля (argon2) — это
// единственная в системе намеренно дорогая операция, и её стоимость видна
// только под нагрузкой. Ожидаемая картина — заметно худшая латентность, чем у
// read-only ручек, и это НЕ дефект.
//
// Профиль упирается в rate limit (RATE_LIMIT_TIMES=20 за 60 с на IP), поэтому
// доля 429 здесь считается отдельно и порогом не ограничивается: с одной
// машины все виртуальные пользователи выглядят одним клиентом.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { randomUUID, tracedHeaders, baseUrl } from './lib/common.js';

const BASE_URL = baseUrl();
const VUS = Number(__ENV.VUS || 20);
const DURATION = __ENV.DURATION || '1m';

const rateLimited = new Counter('auth_rate_limited');
const loginDuration = new Trend('auth_login_duration', true);

export const options = {
  scenarios: {
    auth: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '10s', target: VUS },
        { duration: DURATION, target: VUS },
        { duration: '10s', target: 0 },
      ],
    },
  },
  thresholds: {
    // Порог по логину, а не по всем запросам: argon2 съедает десятки
    // миллисекунд, и общая гистограмма смешала бы дорогое с дешёвым.
    auth_login_duration: ['p(95)<1000'],
    http_req_failed: ['rate<0.05'],
  },
};

export function setup() {
  // Один пользователь на прогон: цель — измерить вход, а не регистрацию.
  const login = `k6-${randomUUID().slice(0, 8)}`;
  const password = 'K6-LoadTest!2026';
  const res = http.post(
    `${BASE_URL}/api/v1/auth/register`,
    JSON.stringify({ login, password, email: `${login}@example.com` }),
    { headers: tracedHeaders() },
  );
  if (res.status !== 201) {
    throw new Error(`не удалось зарегистрировать тестового пользователя: ${res.status} ${res.body}`);
  }
  return { login, password };
}

export default function (data) {
  const res = http.post(
    `${BASE_URL}/api/v1/auth/login`,
    JSON.stringify({ login: data.login, password: data.password }),
    { headers: tracedHeaders(), tags: { name: 'login' } },
  );

  if (res.status === 429) {
    rateLimited.add(1);
    return;
  }
  loginDuration.add(res.timings.duration);
  check(res, { 'вход 200': (r) => r.status === 200 });

  const refreshToken = res.json('refresh_token');
  if (!refreshToken) {
    return;
  }

  // Обновление РОТИРУЕТ refresh-токен (старый отзывается), поэтому цепочку
  // обновлений в одной итерации строить нельзя — второй вызов получил бы 401.
  const refreshed = http.post(`${BASE_URL}/api/v1/auth/refresh`, null, {
    headers: Object.assign(tracedHeaders(), { Authorization: `Bearer ${refreshToken}` }),
    tags: { name: 'refresh' },
  });
  check(refreshed, { 'обновление 200 или 429': (r) => r.status === 200 || r.status === 429 });
}
