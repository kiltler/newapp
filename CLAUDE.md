# CLAUDE.md — контекст проекта NVR Monitor для Claude Code

> Этот файл — инженерная память проекта для ИИ-агента. Читай его перед любой задачей.
> README.md — пользовательская документация; здесь — архитектура, конвенции и правила
> безопасного внесения изменений. Версия проекта: см. `app/__init__.py` (`__version__`),
> сейчас 1.9.x.

---

## 1. Что это за проект

**NVR Monitor** — централизованный мониторинг видеорегистраторов **Hikvision/HiWatch** (ISAPI/XML)
и **Dahua/RVI** (CGI/JSON) у множества клиентов через (часто нестабильные) VPN-каналы.
Следит за: статусом каналов (online/offline/no_video), состоянием HDD, наличием и «дырами»
видеоархива, расхождением часов NVR, качеством картинки (компьютерное зрение), здоровьем
железа (температура/CPU), прошивками. Шлёт алерты в Telegram с анти-спамом и двусторонним
ботом-пультом. Веб-панель на server-side Jinja2, есть PWA и TV-режим для дежурки.

Дополнительно содержит два прикладных подмодуля:
- **«Заселения»** (антивор для гостиниц): ночное скачивание клипов субпотока с фильтром по
  движению/детекту человека, просмотр оператором в плеере, вердикты, Центр уведомлений,
  сверка смены. Сейчас расширяется интеграцией с **1С:Отель** (см. §12).
- **«Автобусы»**: ручной офлайн-учёт ротации дисков в автобусных DVR (без сети/пингов).

Пользователь — практикующий инженер (ИП, обслуживание видеонаблюдения в Хабаровске,
пояс UTC+10). Ценит практичность, надёжность на плохой сети и минимум лишнего.

---

## 2. Стек и запуск

**Стек:** Python 3.12 · FastAPI 0.115 · SQLAlchemy 2.0 (async) · APScheduler 3.11 ·
Jinja2 (server-side) · httpx (digest/basic auth) · Pillow+numpy (CV качества) ·
cryptography/Fernet · onvif-zeep-async · Pydantic v2 / pydantic-settings.
БД: **SQLite по умолчанию** (`sqlite+aiosqlite`), PostgreSQL (`psycopg`) в проде.
Развёртывание: Docker Compose (профили `dev`/`grafana`/`https`), Caddy для авто-HTTPS,
Prometheus+Grafana (опц.). Мобилка — Capacitor-обёртка (`mobile/`).

**Локально:**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # (в Debian-окружении Claude Code: см. --break-system-packages)
cp .env.example .env                      # задать SECRET_KEY (и Telegram при желании)
uvicorn app.main:app --reload            # http://localhost:8000
```
**С эмулятором NVR (без железа):** `MOCK_MODE=true` (эмулятор монтируется на `/mock`),
либо отдельным процессом `MOCK_PROFILE=both MOCK_PORT=8088 python -m mock.server`.
Демо-устройство: host `127.0.0.1`, порт `8088`, `admin`/`admin12345`, тип API «Авто».

**Тесты:** `pytest` (asyncio_mode=auto, гоняются против моков, без реального железа).

**Docker (прод):** `docker compose up -d` (по умолчанию Postgres из сервиса `db`).
Код `./app` смонтирован в контейнер — правки шаблонов/статики подхватываются
после `docker compose restart app` без пересборки образа (важно для офлайн-серверов).

---

## 3. Архитектура и поток данных

Слои строго разделены, зависимость сверху вниз:

```
templates/ + static/   ← Jinja2 SSR-панель, PWA, TV/mobile
      ▲
app/api/*              ← FastAPI-роутеры: HTML-страницы + JSON /api/*
      ▲
app/services/*         ← бизнес-логика (опрос, архив, алерты, ingestion, бэкап…)
      ▲
app/drivers/*          ← абстракция NVR (NVRClient ABC + Hikvision/Dahua)
      ▲
app/models.py          ← ORM (SQLAlchemy 2.0), app/database.py (движок/сессии/миграции)
```

**Ключевой принцип драйверов:** сервисы работают ТОЛЬКО с интерфейсом `NVRClient` и ничего
не знают о протоколе конкретного вендора. Новый вендор = новый класс-реализация + запись
в фабрике.

**Поток опроса (главный цикл):** `scheduler` → `poller.poll_all()` → по каждому устройству
`build_client(device)` → драйвер тянет статусы каналов/HDD/время/здоровье → `poller`
сравнивает с БД, обновляет состояние, дёргает `alerts.raise_alert/resolve_alert`
(дедуп по `scope_key`) → уведомления в Telegram. Семафор `MAX_CONCURRENT_POLLS` щадит VPN.
Суточно `archive.daily_archive_job()` проверяет вчерашний архив + глубину.

---

## 4. Карта репозитория

```
app/
  main.py            точка входа: lifespan (init_db, планировщик, бот), middleware авторизации,
                     регистрация роутеров, статика, /healthz, PWA (/sw.js, /manifest, /offline)
  config.py          Settings (pydantic-settings) — ВСЕ параметры из .env, get_settings() (lru_cache)
  database.py        async-движок, get_session(), init_db(), _lightweight_migrate() + _NEW_COLUMNS
  models.py          все ORM-таблицы (см. §5); utcnow(); строковые константы статусов
  crypto.py          encrypt()/decrypt() (Fernet, префикс "enc:"); приоритет ключей (см. §6)
  crud.py            мелкие хелперы выборок
  schemas.py         Pydantic-схемы запросов/ответов
  scheduler.py       APScheduler: poll_all, archive, quality, watchdog, auto_backup, checkin_ingest
  templatefilters.py Jinja-фильтр localtime + глобали (app_version)
  drivers/           base.py (NVRClient ABC + dataclasses) · hikvision.py (ISAPI) ·
                     dahua.py (CGI) · detect.py (автоопределение) · factory.py (build_client)
  services/          poller · archive · alerts · quality · metrics · backup · worklist ·
                     bulkops · watchdog · audit · appsettings · users · telegram · bot ·
                     checkin_ingest (ingestion заселений)
  api/               auth · devices · monitoring · dashboard · plan · buses · worklist ·
                     users_api · backup_api · checkin
  templates/ static/ SSR-панель, PWA (sw.js, manifest), стили (style.css, CSS-переменные)
mock/                эмулятор NVR (ISAPI + Dahua CGI) с digest-auth: server.py, state.py, auth.py
tests/               pytest против моков (по модулю на файл)
monitoring/          prometheus.yml + grafana dashboard
mobile/              Capacitor-обёртка PWA
Dockerfile · docker-compose.yml · Caddyfile · .env.example · CHANGELOG.md · README.md
```

---

## 5. Модель данных (`app/models.py`)

Все таблицы в одном файле. Домены:

- **Мониторинг:** `Group` (объекты/клиенты), `Device`, `Channel`, `Hdd`, `ArchiveCoverage`
  (покрытие архива канал×день), `Event` (лента событий), `AlertState` (активные алерты для
  дедупа), `Note` (журнал обслуживания), `PlanMarker` (точки камер на плане), `AuditLog`,
  `IssueAck` (квитирование проблем).
- **Автобусы:** `Bus`, `Disk`, `AssetBatch`, `Asset`, `SwapLog`, `DiskReview`.
- **Служебное:** `AppSetting` (key/value из UI), `User` (роли: admin, bus).
- **Заселения:** `CheckinHotel`, `CheckinRecorder`, `CheckinChannel` (роли `ChannelRole`:
  `entrance`/`reception`/`floor1..floor6`), `CheckinClip`, `CheckinLog` (вердикт оператора),
  `CheckinReconciliation` (сверка камера↔1С), `CheckinNotification` (Центр уведомлений),
  `CheckinIngestRun` (прогресс прогона). Статусы: `ClipStatus`, `CheckinVerdict`,
  `NotificationStatus`, `IngestRunStatus`, `RecorderModel`.

Статусы хранятся строковыми константами (классы-неймспейсы вроде `ChannelState`,
`Severity`), НЕ Python-Enum в БД. Служебные метки — `utcnow()` (tz-aware UTC).

---

## 6. КЛЮЧЕВЫЕ КОНВЕНЦИИ — соблюдать строго

### 6.1 Миграции — без Alembic
Схема создаётся `Base.metadata.create_all`. Новые колонки к существующим таблицам
добавляются через список **`_NEW_COLUMNS`** в `app/database.py` (кортежи
`(table, column, DDL-тип)`), которые `_lightweight_migrate()` накатывает идемпотентно
(проверка через `inspect`, `ALTER TABLE ... ADD COLUMN` в SAVEPOINT — сбой одной не роняет старт).
**Добавляешь колонку → добавь строку в `_NEW_COLUMNS`.** Новую таблицу create_all создаст сам.
Имена колонок в DDL — в кавычках (могут быть зарезервированными словами в Postgres).

### 6.2 Время — две разные конвенции, не путать
- **Служебные метки** (`created_at`, `synced_at`, события, алерты) — **UTC**, через `utcnow()`.
  Jinja-фильтр `localtime` показывает их в поясе `TIMEZONE`.
- **Время камер/архива/заселений** — **наивный datetime в локальном времени устройства**
  (`_parse_hik_time` отбрасывает tzinfo). `ArchiveSegment.start/end`, `CheckinClip.start_ts`,
  таймлайн плеера — всё это настенное локальное время камеры БЕЗ конверсии в UTC.
  ⚠ **Никогда не конвертируй время записи/заселения в UTC** — это ломает выравнивание
  на таймлайне (типовой баг ±10ч для Хабаровска). Регистраторы стоят в поясе сервера (`TZ`).

### 6.3 Шифрование секретов
Пароли устройств/регистраторов и прочие секреты вне веб-морды — только через
`crypto.encrypt(plain) -> "enc:..."` / `crypto.decrypt(ciphertext)`.
Приоритет мастер-ключа: `NVR_SECRET_KEY` → `SECRET_KEY` → автоген `data/secret.key`.
(`SECRET_KEY` также используется для подписи сессионной cookie в `SessionMiddleware` —
это разные назначения одного значения, если `NVR_SECRET_KEY` не задан.)

### 6.4 Алерты и уведомления
- Мониторинг устройств: `alerts.raise_alert(session, scope_key=..., ...)` — идемпотентно,
  анти-спам через `AlertState` (пока проблема активна, повтор не шлётся);
  `alerts.resolve_alert(...)` шлёт «восстановлено». Разовые — `alerts.notify_once(...)`.
  `scope_key` — стабильный идентификатор проблемы, напр. `device:5:channel:3:offline`.
  Для алертов уровня устройства бот добавляет кнопки «перезагрузить/синхр. время».
- Подмодуль «Заселения» использует СВОЙ Центр уведомлений — таблицу `CheckinNotification`
  (не `AlertState`), с тем же принципом «не плодить дубли». Новые уведомления заселений
  делай через неё.

### 6.5 Настройки, правимые из UI
Скалярные настройки, которые меняют из панели без рестарта, — через `AppSetting`
(`services/appsettings.py`: `get_int`, `set_value`), с фолбэком на `.env`. Пример: время
ночного джоба заселений (`checkin_job_hour/minute`) — оно же перепланирует APScheduler-джоб
на лету (`scheduler.reschedule_checkin_job`).

### 6.6 Аудит
Действия оператора в панели логируй: `audit.log_action(session, request, action, target, detail)`.

### 6.7 Драйверы
Интерфейс `NVRClient` (ABC, `drivers/base.py`). Обязательные методы: `get_device_info`,
`get_channel_statuses`, `get_hdd_info`, `search_archive`, `get_device_time`.
Необязательные (по умолчанию кидают `FeatureUnavailable`): `get_snapshot`, `sync_time`,
`reboot`, `get_health`, `list_tracks`, `search_activity`, `rtsp_playback_url`.
Клиент создаётся ТОЛЬКО через `factory.build_client(device, ...)` (сам расшифровывает пароль).
Урезанные прошивки (HiWatch, старые RVI) → фича помечается `unavailable` через
`probe_capabilities()`, без падения. Ошибки драйвера: `NVRConnectionError`, `NVRAuthError`,
`FeatureUnavailable` (все от `NVRError`). Автоопределение вендора — `detect.detect_api_type`.

### 6.8 Регистрация роутеров и авторизация
Новый роутер (`APIRouter`) подключается в `app/main.py` через `app.include_router(...)`.
Middleware `require_login`: если `ADMIN_PASSWORD` пуст — панель открыта; иначе нужна сессия.
Роль `bus` видит только вкладку «Автобусы» (`_BUS_PREFIXES`). Публичные пути — `_PUBLIC_PREFIXES`.
HTML-страницы отдают редирект на `/login`, `/api/*` — 401/403 JSON.

### 6.9 Планировщик
Джобы добавляются в `scheduler.start_scheduler()` (или отдельными `_add_*`-функциями)
паттерном `scheduler.add_job(coro, trigger=..., id=..., max_instances=1, coalesce=True,
replace_existing=True)`. Условные джобы включаются по настройке (напр. `quality_check_minutes>0`).

---

## 7. Функциональные модули (кратко)

- **Устройства** (`api/devices.py`, `services/poller.py`): CRUD, тест соединения,
  автоопределение API, снапшот канала, синхр. времени, перезагрузка, «сырой ответ» для диагностики.
- **Мониторинг/дашборд** (`api/monitoring.py`, `api/dashboard.py`): сводка, объекты с цветовой
  индикацией, страница устройства (каналы/HDD/календарь архива/события), история/heatmap/KPI,
  TV-режим, стена камер, план объекта, прошивки, калькулятор хранилища, аудит, режим монтажника + QR.
- **Архив** (`services/archive.py`): суточная проверка, поиск «дыр», глубина хранения, `ArchiveCoverage`.
- **Качество картинки** (`services/quality.py`): CV-анализ снапшота — тёмный/чёрный, однотонный
  (залеплен), расфокус, зависший поток (то, что NVR считает online).
- **Здоровье NVR** (`poller._check_health`): температура/CPU, алерт на перегрев.
- **Алерты/Telegram** (`services/alerts.py`, `telegram.py`, `bot.py`): анти-спам, «восстановлено»,
  двусторонний бот `/status /devices /round /cam` + кнопки, прокси к Telegram (обход DPI).
- **Worklist** (`services/worklist.py`): единый список текущих проблем + квитирование.
- **Массовые операции** (`services/bulkops.py`): опрос/синхр. времени по группе/всем.
- **Watchdog** (`services/watchdog.py`): внешний «пульс» (healthchecks.io) — сторож для сторожа.
- **Бэкап** (`services/backup.py`): экспорт/импорт данных, авто-бэкап 03:30.
- **Метрики** (`services/metrics.py`): `/metrics` для Prometheus.
- **Автобусы** (`api/buses.py`): офлайн-учёт дисковой ротации, атомарная «Замена диска», журнал.
- **Заселения** (`api/checkin.py`, `services/checkin_ingest.py`): см. §12.
- **Пользователи/вход** (`api/auth.py`, `users_api.py`, `services/users.py`): роли admin/bus.

---

## 8. Как безопасно добавлять фичу (чек-лист)

1. **Прочитай** релевантные файлы и найди аналогичный существующий паттерн — следуй ему.
2. **Модель:** таблица/поля в `models.py`; новые колонки к старым таблицам → строка(и) в
   `_NEW_COLUMNS` (`database.py`).
3. **Схемы:** Pydantic-модели ввода/вывода в `schemas.py`.
4. **Сервис:** логика в `app/services/<name>.py` (async, изолированно от FastAPI).
5. **Роут:** `APIRouter` в `app/api/<name>.py` + `include_router` в `main.py`.
6. **UI:** шаблон в `templates/`, стили через существующие CSS-переменные `style.css`.
7. **Планировщик:** при фоновой периодике — джоб в `scheduler.py`.
8. **Секреты:** через `crypto`; **настройки из UI** — через `AppSetting`; **аудит** — `audit.log_action`.
9. **Тест:** файл в `tests/` против моков (см. §10).
10. **CHANGELOG.md** (формат Keep a Changelog, на русском) + подними `__version__` в `app/__init__.py`
    по semver (фича без поломок = минорная).
11. Ничего лишнего не ломай; уважай конвенции времени и миграций (§6).

---

## 9. Стиль кода и коммуникация

- Async везде в сервисах/роутах; сессия БД — через `Depends(get_session)`.
- **Комментарии, docstring, сообщения об ошибках, тексты UI и алертов — на русском** (как во всём проекте).
- Строковые константы статусов вместо Enum в БД. Дата-классы для передачи данных драйверов.
- Ошибки не глотать молча без причины; сетевые/драйверные — своими исключениями (`NVRError` и наследники).
- Коммиты — по-русски, по смыслу. Держи изменения сфокусированными и по фазам.
- Не тащи тяжёлые зависимости без нужды. Всё, что можно, — стандартной библиотекой/уже имеющимися пакетами.

---

## 10. Тестирование

`pytest` (asyncio_mode=auto, `testpaths=tests`). Тесты идут **против встроенного мока** NVR
(`mock/server.py` — эмулятор ISAPI + Dahua CGI с digest-auth), реальное железо не нужно.
`tests/conftest.py` — общие фикстуры (обычно SQLite in-memory + мок-сервер). По файлу на модуль:
`test_poller`, `test_archive`, `test_alerts`, `test_quality`, `test_hikvision`, `test_dahua`,
`test_detect`, `test_checkin`, `test_buses`, `test_backup`, `test_users`, `test_pages`,
`test_actions`, `test_timefmt`, `test_watchdog`, `test_worklist`, `test_assets`.
**Новая логика → новый/дополненный тест того же стиля.** Проверяй `pytest` перед завершением задачи.
Для ручной проверки UI — `MOCK_MODE=true` и демо-устройство.

---

## 11. Развёртывание

- **docker-compose.yml** профили: базовый (`app` + Postgres `db`); `dev` (эмулятор `mock` на 8088);
  `grafana` (Prometheus+Grafana); `https` (Caddy авто-HTTPS по `DOMAIN`). Тома: `./data` (БД),
  `./clips` (клипы заселений, могут быть большими), `./app` смонтирован для hot-reload шаблонов.
- **SQLite ↔ Postgres:** переключается через `DATABASE_URL`. В Compose по умолчанию Postgres.
- **HTTPS/PWA извне:** задать `DOMAIN` (например DDNS от MikroTik) + проброс 80/443 → профиль `https`.
- **.env.example** — исчерпывающий список переменных с комментариями (пороги алертов, качество,
  Telegram, watchdog, вход, заселения). Сверяйся с ним, новые переменные добавляй туда же.

---

## 12. Текущая активная работа: интеграция 1С:Отель (подмодуль «Заселения»)

**Что уже есть в подмодуле:** ночной ingestion клипов субпотока с фильтром motion/person
(`checkin_ingest.py`), плеер оператора (`templates/checkin_clips.html`, нативный `<video>`,
маппинг «позиция видео → настенное время камеры»), вердикты (`CheckinLog`), Центр уведомлений
(`CheckinNotification`), заготовка сверки камера↔1С (`CheckinReconciliation` с полем `ones_count`,
пока не заполняется). Роли каналов: `reception`/`entrance`/`floor1..floor6`.
В драйвере есть `get_device_time()` / `sync_time()`.

**Что внедряется:** подтягивание заселений из **1С:Отель** и отметки на таймлайне плеера
(«в это время было возможное заселение») с точной синхронизацией часов камеры и 1С.

Ключевые архитектурные решения (детали — в отдельных промптах-заданиях):
- **Per-hotel:** конфигурация 1С и синхронизация — на КАЖДУЮ гостиницу (`CheckinHotel`)
  отдельно, привязаны к её регистраторам. Отдельная таблица `OneCConnection` (по гостинице,
  пароль через `crypto`). Событие заселения — модель `OneCCheckin` (uniq по `(hotel_id, onec_ref)`).
  Развёртывание: 3 гостиницы, **раздельные базы 1С** (у каждой свой `base_url` + креды).
- **Интеграция через стандартный OData 1С** (адаптер, чтобы позже заменить на HTTP-сервис).
  Имена объекта/реквизитов 1С — настраиваемые (зависят от редакции), НЕ хардкод.
- **Синхронизация времени:** смещение часов регистратора `time_offset_sec` меряется через
  `get_device_time()` vs время сервера; отметка ставится по формуле
  `video_offset = (event_local + offset − clip.start_ts)` + окно PRE/POST-ROLL (гость появляется
  на камере раньше проводки документа). Время заселения — наивное локальное Хабаровска (§6.2).
- **Нюанс:** комната в 1С обычно приходит ссылкой-GUID (`Номер_Key`), а не строкой — правило
  «номер→этаж» требует резолва номера через справочник номерного фонда (`$expand`/доп. запрос).

Соблюдай §6 (миграции, время, крипто, уведомления через `CheckinNotification`) и §8 (чек-лист).

---

## 13. Границы (чего не делать)

- Не вводить Alembic — миграции только через `_NEW_COLUMNS`/create_all (§6.1).
- Не конвертировать время камер/архива/заселений в UTC (§6.2).
- Не хранить секреты открытым текстом — только `crypto` (§6.3).
- Не читать внутренние таблицы БД 1С напрямую — только через OData/HTTP-сервис.
- Не ломать интерфейс `NVRClient` и изоляцию слоёв (сервисы не знают протокол вендора).
- Не переписывать работающие модули ради рефакторинга без запроса — расширяй точечно.
- Тексты/комментарии — на русском; тяжёлые зависимости — только по необходимости.
- Проверяй `pytest`; обновляй CHANGELOG + версию.
