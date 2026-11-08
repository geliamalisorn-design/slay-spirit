# API-контракт MVP

## Аккаунты и прогресс

Аутентификация хранится в HttpOnly cookie `slay_auth` (SameSite=Lax, срок 30 дней); bearer token принимается для API-клиентов. Пароли хешируются PBKDF2-SHA256 с уникальной случайной солью. SQLite schema расширяется при запуске через `CREATE TABLE IF NOT EXISTS` и проверку существующих колонок; существующие игровые записи не удаляются.

- `POST /api/auth/register` — `display_name`, `email`, `password`; создаёт пользователя и cookie-сессию.
- `POST /api/auth/login` — вход; после восьми неуспешных попыток за 10 минут для email/IP возвращает `429`.
- `POST /api/auth/logout` — удаляет серверный токен и истекает cookie.
- `GET /api/me` — текущий пользователь либо `{"user": null}`.
- `GET /api/tutorial` — состояние туториала.
- `POST /api/tutorial/progress` — следующий шаг туториала (`step`: 1–4); перескок через шаг отклоняется backend.
- `POST /api/tutorial/restart` — начать повторное прохождение с первого шага, сохранив статус завершения и открытые уровни.
- `GET /api/levels` — открытые уровни текущего пользователя.
- `GET /api/profile` — агрегаты, рекомендации и история с ответами текущего пользователя.
- `GET /api/scenario-filters` — только периоды и рынки, для которых доступна полная сессия из пяти утверждённых сценариев.
- `POST /api/profile/preferences` — сохраняет `period` и `geography` на сервере после проверки достаточного покрытия данными. Применяется к новым сессиям.

`POST /api/session` требует авторизацию и `{"level":"easy|medium|hard"}`. Сервер проверяет туториал и разблокированный уровень. `GET /api/session` и `POST /api/answer` проверяют владельца сессии. Старые игровые записи сохраняются с `user_id = NULL`; новые пользовательские сессии привязаны к аккаунту.

При создании сессии сервер выбирает пять сценариев по сохранённым предпочтениям. Если достаточного набора нет, запрос отклоняется с `409 INSUFFICIENT_SCENARIOS`; неполная сессия не создаётся.

## `POST /api/session`

Создаёт сессию. Ответ содержит `session_id`, `round_index`, `state`, `current` и `history`.

`current` до решения включает `chart` (60 нормированных точек), `intro`, `factors`, `quiz` без `correct_option`, `companies`, `glossary` и `threshold_pct`.

## `GET /api/session`

Требует `X-Session-Id`. Возвращает сохранённое состояние либо `404`.

## `POST /api/answer`

Тело:

```json
{
  "session_id": "…",
  "round_index": 0,
  "company": "SBER",
  "year": 2020,
  "month": 1,
  "forecast": "up|flat|down",
  "strategy": "buy|hold|sell",
  "rationale": "минимум 5 символов",
  "factors": [0],
  "quiz_answers": [{"id": "q1", "option": 0}]
}
```

После успешной фиксации ответ содержит reveal-поля `reveal_prices`, `actual_forecast`, `alternatives`, `score`, `quiz_result`, `summary` и источники. Повторный запрос старого `round_index` возвращает сохранённый результат без повторного начисления.

Коды ошибок: `400` для неверного ответа, `404` для неизвестной сессии, `409` для устаревшего индекса, `503 DATA_UNAVAILABLE` для отсутствующего approved dataset.

## `GET /api/tutorial/practice` и `POST /api/tutorial/practice`

Оба маршрута требуют авторизацию. GET возвращает первый проверенный учебный сценарий с 60 торговыми днями, без будущих цен и правильных ответов. POST принимает поля раундового ответа (`company`, `year`, `month`, `forecast`, `rationale`, `strategy`, `factors`, `quiz_answers`), проверяет их сервером и только затем возвращает разбор с последующими 30 сессиями. Учебный ответ не сохраняется в `answers` и не попадает в статистику; backend отмечает прохождение practice в профиле пользователя, чтобы защитить завершение обязательного туториала.
