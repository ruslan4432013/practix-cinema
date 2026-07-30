// Общие помощники сценариев k6.
import exec from 'k6/execution';

export function baseUrl() {
  return (__ENV.BASE_URL || 'http://localhost').replace(/\/$/, '');
}

// crypto.randomUUID в k6 недоступен, а uuid из jslib тянет внешнюю загрузку —
// поэтому свой генератор. Криптостойкость здесь не нужна: значения идут в
// event_id и session_id, где важна только уникальность.
export function randomUUID() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// Один session_id на виртуального пользователя за итерацию: иначе каждое
// событие выглядело бы новой сессией и профиль перестал бы походить на живой.
export function sessionId() {
  return `k6-session-${exec.vu.idInTest}`;
}

export function anonymousId() {
  return `k6-anon-${exec.vu.idInTest}`;
}

export function clientHeaders() {
  const headers = {
    'Content-Type': 'application/json',
    // Реальный User-Agent: сервис разбирает его в тип устройства/ОС/браузер,
    // и на пустом заголовке этот путь кода не нагружался бы вовсе.
    'User-Agent':
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  };
  if (__ENV.ACCESS_TOKEN) {
    headers.Authorization = `Bearer ${__ENV.ACCESS_TOKEN}`;
  }
  return headers;
}

// X-Request-Id обязателен для Movies API и Auth (middleware отвечает 400 без
// него); коллектор, наоборот, генерирует его сам — см. core/request_id.py.
export function tracedHeaders() {
  return Object.assign(clientHeaders(), { 'X-Request-Id': randomUUID() });
}
