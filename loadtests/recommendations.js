// Нагрузочный профиль выдачи рекомендаций.
//
// Пороги здесь — не ориентир, а SLO из раздела 5.1 ТЗ, и k6 выходит с
// ненулевым кодом при их нарушении: p95 < 200 мс, p99 < 300 мс, доля 5xx
// меньше 0,1%. Последний порог проверяет главный инвариант сервиса: блок
// рекомендаций не имеет права уронить страницу фильма.
//
// Профиль смешивает ручки в тех же пропорциях, что и живой трафик по расчёту
// раздела 4: одна главная (популярное или, если задан ACCESS_TOKEN,
// персональное) на три страницы фильма (похожие). Гонять только похожие значило
// бы мерить один путь чтения и молчать про остальные.
import http from 'k6/http';
import exec from 'k6/execution';
import { check, group } from 'k6';
import { Counter } from 'k6/metrics';
import { tracedHeaders, syntheticClient, baseUrl } from './lib/common.js';

const BASE_URL = baseUrl();

// Запросов в одной итерации: 3 похожих + 1 главная. Из этого числа выводится
// интенсивность, поэтому оно названо, а не размазано по коду.
const REQUESTS_PER_ITERATION = 4;

// Целевая интенсивность в ЗАПРОСАХ В СЕКУНДУ — той же единице, в которой
// считает раздел 4 ТЗ (пик ~83 RPS, проектируем на 100). Executor с фиксированной
// интенсивностью воспроизводит именно эту постановку; профиль на ramping-vus без
// пауз меряет совсем другое — максимальную пропускную способность.
const RPS = Number(__ENV.RPS || 100);

// Со скольких адресов идёт нагрузка. Зона recs_read режет 50 r/s НА АДРЕС
// (infra/nginx/nginx.conf:197), а плановые 100 RPS в бою приходят с многих
// адресов — один генератор упёрся бы в лимитер и мерил бы его, а не сервис.
// Итерации раскладываются по CLIENTS синтетическим клиентам через
// X-Forwarded-For (см. syntheticClient в lib/common.js).
//
// Арифметика, которую надо соблюдать: RPS / CLIENTS должно оставаться заметно
// ниже 50. При 100 и 8 это 12,5 r/s на адрес — четырёхкратный запас, burst=100
// nodelay поглощает джиттер. Появление 429 означает, что раскладка не работает,
// и порог по recs_rate_limited валит прогон явным именем.
const CLIENTS = Number(__ENV.CLIENTS || 8);

const DURATION = __ENV.DURATION || '1m';

// Прогрев. Первое обращение к фильму идёт мимо пустого Redis в PostgreSQL и
// стоит ~550 мс (docs/recommendations.md) — это выше потолка ТЗ в 300 мс, и без
// прогрева p99 пробивала бы механика замера, а не сервис. Пороги привязаны к
// сценарию read, так что прогрев в них не входит.
const WARMUP = __ENV.WARMUP || '30s';

// Идентификаторы реальных фильмов каталога. Переменная ОБЯЗАТЕЛЬНА, и подставить
// вместо неё нечего: зашитый в сценарий список привязал бы нагрузочный тест к
// конкретному дампу базы, а синтетический идентификатор — не фильм, и выдача
// отвечает на него 404 (services/degradation.py::_ensure_film_exists), то есть
// прогон валит порог http_req_failed, ничего при этом не измерив.
const FILM_IDS = (__ENV.FILM_IDS || '').split(',').filter(Boolean);

// Останов происходит в init-контексте — до первого запроса и до того, как k6
// разберёт scenarios: падать по порогу там, где не хватает входных данных,
// значит сообщать симптом вместо причины.
if (FILM_IDS.length === 0) {
  // Сообщение однострочное намеренно: k6 печатает причину останова как одно
  // поле msg=, экранируя переносы, и многострочный рецепт превратился бы в
  // нечитаемую строку с \n и \\ вместо инструкции.
  exec.test.abort(
    'FILM_IDS не задан, а подставить вместо него нечего: запросы за похожими ушли бы к ' +
      'несуществующему фильму, выдача ответила бы 404 на каждый из них, и прогон упал бы по ' +
      'порогу http_req_failed, ничего не измерив. Нужен список идентификаторов настоящих ' +
      'фильмов витрины через запятую; взять его можно из recs-db запросом ' +
      "SELECT string_agg(film_id::text, ',') FROM (SELECT DISTINCT film_id FROM similar_item LIMIT 200) s " +
      '— готовый рецепт запуска лежит в loadtests/README.md.',
  );
}

// Главная ручка: персональная, если есть чем авторизоваться, иначе популярное.
const MAIN_PATH = __ENV.ACCESS_TOKEN ? '/api/v1/recommendations/me' : '/api/v1/recommendations/popular';

const rateLimited = new Counter('recs_rate_limited');
// Счётчики по полю source — раздел F1.4 в нагрузочном изводе. Ответ 200 не
// говорит, что отдана выдача: похожие могли деградировать в популярное, а
// персональное — подмениться им же. Без этих чисел прогон, в котором работает
// одна лестница деградации, выглядит идеально здоровым.
const sourceCounters = {
  similar: new Counter('recs_source_similar'),
  personal: new Counter('recs_source_personal'),
  popular: new Counter('recs_source_popular'),
};

export const options = {
  // p99 в сводке по умолчанию не печатается, хотя это порог из раздела 5.1 и
  // цифра, которую обязан называть отчёт: «порог пройден» без значения
  // непроверяемо.
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    // Обходит ВЕСЬ список фильмов ровно по разу и детерминированно: случайная
    // выборка оставила бы часть каталога холодной, и холод всплыл бы уже на плато.
    warmup: {
      executor: 'shared-iterations',
      exec: 'warmup',
      iterations: FILM_IDS.length,
      // Мало VU намеренно: на уже тёплой витрине прогрев идёт сотнями запросов в
      // секунду и сам подошёл бы к зоне recs_read. k6 к тому же требует
      // iterations >= vus, а список фильмов может оказаться коротким.
      vus: Math.min(4, FILM_IDS.length),
      maxDuration: WARMUP,
    },
    read: {
      executor: 'constant-arrival-rate',
      rate: Math.max(Math.round(RPS / REQUESTS_PER_ITERATION), 1),
      timeUnit: '1s',
      duration: DURATION,
      startTime: WARMUP,
      // maxVUs держится в стороне от limit_conn perip 100 (nginx.conf:201-203):
      // на 100 RPS с латентностью в десятки миллисекунд нужны единицы VU, и
      // упираться в соседний лимит на ровном месте незачем.
      preAllocatedVUs: 40,
      maxVUs: 200,
    },
  },
  thresholds: {
    // Раздел 5.1: доля неуспешных ответов меньше 0,1%. Считается по плато:
    // прогрев ходит в холодную витрину и в измеряемую долю попадать не должен.
    'http_req_failed{scenario:read}': ['rate<0.001'],
    'http_req_duration{scenario:read}': ['p(95)<200', 'p(99)<300'],
    // Отдельная проверка главного инварианта: ни одного 5xx. Он важнее
    // латентности — блок рекомендаций не имеет права уронить страницу фильма.
    'checks{check:без 5xx}': ['rate==1.0'],
    // 429 на проектной нагрузке — не «лимитер сработал», а «раскладка по
    // адресам не работает, и мы снова меряем зону вместо сервиса».
    recs_rate_limited: ['count==0'],
  },
};

function randomFilm() {
  return FILM_IDS[Math.floor(Math.random() * FILM_IDS.length)];
}

// Адрес выбирается по номеру ИТЕРАЦИИ, а не по номеру VU: при
// constant-arrival-rate виртуальные пользователи переиспользуются неравномерно,
// и «примерно поровну» превратилось бы в перекос, заметный только когда один
// адрес упрётся в 50 r/s.
function headersForCurrentIteration() {
  return tracedHeaders({ 'X-Forwarded-For': syntheticClient(exec.scenario.iterationInTest % CLIENTS) });
}

function countRateLimited(res) {
  if (res.status === 429) {
    rateLimited.add(1);
  }
}

function countAnswer(res) {
  countRateLimited(res);
  if (res.status !== 200) {
    return;
  }
  const source = res.json('source');
  if (sourceCounters[source] !== undefined) {
    sourceCounters[source].add(1);
  }
}

// Прогрев: по одному запросу на каждый фильм списка, по порядку.
export function warmup() {
  const film = FILM_IDS[exec.scenario.iterationInTest % FILM_IDS.length];
  // Считается только 429, но считается: пороги латентности к прогреву не
  // привязаны, а вот отказ здесь означает, что часть каталога осталась
  // холодной, и её цену заплатит уже измеряемое плато. Источники, наоборот, тут
  // не считаются — они должны описывать плато, а не подмешивать к нему прогрев.
  countRateLimited(
    http.get(`${BASE_URL}/api/v1/recommendations/similar/${film}?limit=10`, {
      headers: headersForCurrentIteration(),
    }),
  );
  countRateLimited(http.get(`${BASE_URL}${MAIN_PATH}?limit=10`, { headers: headersForCurrentIteration() }));
}

export default function () {
  const headers = headersForCurrentIteration();

  group('похожие (страница фильма)', () => {
    for (let i = 0; i < REQUESTS_PER_ITERATION - 1; i += 1) {
      const res = http.get(`${BASE_URL}/api/v1/recommendations/similar/${randomFilm()}?limit=10`, { headers });
      check(res, {
        'похожие 200': (r) => r.status === 200,
        'без 5xx': (r) => r.status < 500,
        // Источник обязан присутствовать всегда: по нему видно, отдана выдача
        // или деградация. Ответ без него — молчащая деградация.
        'источник назван': (r) => r.status === 200 && r.json('source') !== undefined,
      });
      countAnswer(res);
    }
  });

  group('главная (популярное или персональное)', () => {
    const res = http.get(`${BASE_URL}${MAIN_PATH}?limit=10`, { headers });
    check(res, { 'главная 200': (r) => r.status === 200, 'без 5xx': (r) => r.status < 500 });
    countAnswer(res);
  });
}
