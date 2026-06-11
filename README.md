# NVR Monitor

Централизованный мониторинг видеорегистраторов **Hikvision / HiWatch** (ISAPI) и
**Dahua / RVI** (CGI). Следит за десятками NVR у разных клиентов: статус каналов,
состояние HDD, наличие видеоархива, расхождение часов — с алертами в Telegram.

## Возможности

- **Устройства**: CRUD регистраторов, группировка по объектам/клиентам, шифрованное
  хранение паролей (Fernet), тест соединения и **автоопределение типа API**.
- **Драйверы через общий интерфейс `NVRClient`**: `HikvisionClient` (ISAPI/XML) и
  `DahuaClient` (CGI/JSON). Архитектура готова к добавлению новых производителей.
- **Capability-check**: при добавлении проверяется каждый ключевой эндпоинт
  (каналы / HDD / архив / время). Недоступные на урезанной прошивке (HiWatch,
  старые Dahua/RVI) помечаются как `unavailable` — без падения.
- **Мониторинг каналов**: online / offline / no_video, в т.ч. videoloss для
  аналоговых/гибридных каналов; история изменений статуса.
- **Мониторинг HDD**: объём, занято/свободно, статус (OK / error / no_disk),
  алерт при ошибке/отсутствии диска и опционально при заполнении выше порога.
- **Контроль архива** (ключевая фича): суточная проверка наличия записей за вчера,
  поиск «дыр» больше порога, **календарь архива** (канал × день) в UI.
- **Проверка времени NVR**: алерт при расхождении часов с сервером > 5 мин
  (иначе проверка архива врёт).
- **Уведомления Telegram** с анти-спамом: алерт не дублируется, пока проблема
  активна; при возврате в норму шлётся «восстановлено».
- **Дашборд**: сводка, список объектов с цветовой индикацией, страница устройства
  (каналы, HDD, календарь архива, события), лог событий.
- **Вход по паролю** в панель (опционально, через `ADMIN_PASSWORD`).
- **Действия с устройством**: снимок кадра канала (JPEG), синхронизация времени
  по нажатию, удалённая перезагрузка NVR, диагностика (сырой ответ NVR).
- **Стена камер** — мозаика кадров объекта с авто-обновлением.
- **Глубина архива**: реальная глубина хранения по каналам (сколько дней назад
  есть запись) + печатный отчёт по объекту.
- **Заглушка каналов** (не мониторить отдельный канал) и **массовые операции**
  (опрос / синхронизация времени по группе или по всем).
- **Контроль качества картинки (компьютерное зрение)**: анализ снапшота ловит
  тёмный/чёрный кадр, однотонный (залеплен объектив), расфокус и зависший поток —
  то, что NVR считает «online». Настраивается `QUALITY_CHECK_MINUTES` и порогами.
- **Снимок камеры в Telegram-алерте**: при проблеме с картинкой в чат приходит
  сам проблемный кадр.
- **Сеть**: асинхронный опрос (httpx), индивидуальные таймауты/retry на устройство,
  ограничение одновременных подключений (semaphore) — щадит VPN-каналы; алерт
  «NVR недоступен» только после N неудачных циклов подряд.
- **Mock-режим**: встроенный эмулятор ISAPI и Dahua CGI для разработки и тестов
  без реального железа.

## Стек

FastAPI · SQLAlchemy (SQLite по умолчанию / PostgreSQL) · APScheduler · httpx
(digest/basic auth) · Jinja2 (server-side дашборд) · Docker Compose.

## Структура

```
app/
  main.py            точка входа FastAPI (БД, планировщик, роуты, дашборд)
  config.py          настройки из .env
  models.py          ORM: устройства, каналы, HDD, события, архив, состояния алертов
  crypto.py          шифрование паролей (Fernet)
  scheduler.py       APScheduler: опрос + суточная проверка архива
  drivers/           base.py (NVRClient) · hikvision.py · dahua.py · detect.py · factory.py
  services/          poller.py · archive.py · alerts.py · telegram.py
  api/               devices.py · monitoring.py · dashboard.py
  templates/ static/ веб-дашборд
mock/                эмулятор NVR (ISAPI + Dahua CGI) с digest auth
tests/               тесты против моков
```

## Быстрый старт (локально)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # задайте SECRET_KEY и Telegram (см. ниже)

# (опционально) запустить эмулятор NVR в отдельном терминале:
MOCK_PROFILE=both MOCK_PORT=8088 python -m mock.server

uvicorn app.main:app --reload   # дашборд: http://localhost:8000
```

Добавьте устройство через «+ Устройство» (для демо: host `127.0.0.1`, порт `8088`,
логин `admin`, пароль `admin12345`, тип API — «Авто»).

### SECRET_KEY

Ключ шифрования паролей устройств. Сгенерировать:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> ⚠️ Без `SECRET_KEY` генерируется временный ключ — сохранённые пароли не
> расшифруются после перезапуска.

### Telegram

1. Создайте бота у [@BotFather](https://t.me/BotFather) → получите `TELEGRAM_BOT_TOKEN`.
2. Узнайте `chat_id` нужного чата/группы (например через @getidsbot).
3. Заполните в `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ENABLED=true`.

## Развёртывание на Ubuntu VPS через Docker Compose

```bash
# 1. Установить Docker + Compose plugin
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker

# 2. Получить проект и настроить окружение
git clone <repo-url> nvrmon && cd nvrmon
cp .env.example .env
# обязательно задайте SECRET_KEY и параметры Telegram в .env

# 3. Запустить (app + PostgreSQL)
sudo docker compose up -d --build

# 4. Проверить
sudo docker compose ps
curl -fsS http://localhost:8000/healthz
```

Дашборд: `http://<IP-VPS>:8000`. По умолчанию в Compose используется PostgreSQL
(сервис `db`); чтобы перейти на SQLite — задайте `DATABASE_URL=sqlite+aiosqlite:///./data/nvrmon.db`
в `.env` и уберите `DATABASE_URL` из `environment` сервиса `app`.

Эмулятор NVR для демо без железа:

```bash
sudo docker compose --profile dev up -d mock   # http://<IP>:8088
```

### Сетевая топология

Сервер мониторинга должен иметь доступ к NVR одним из способов:

1. **Локальная сеть** — прямой `IP:port` (ISAPI обычно на 80, иногда 8000).
2. **VPN** — прямой IP с задержками: задайте таймаут 10–15 с и retry на устройстве.
3. **Проброс портов** — внешний IP + нестандартный порт (`host` + `http_port`).

Опрос параллельный с ограничением `MAX_CONCURRENT_POLLS` (semaphore), чтобы не
перегрузить VPN-каналы. Недоступность по сети ≠ авария: алерт «NVR недоступен»
шлётся только после `NVR_UNREACHABLE_THRESHOLD` неудачных циклов подряд.

## Настройка (`.env`)

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `DATABASE_URL` | строка подключения к БД | SQLite в `./data` |
| `SECRET_KEY` | ключ Fernet для паролей | — (обязательно) |
| `POLL_INTERVAL_MINUTES` | период опроса | 5 |
| `ARCHIVE_CHECK_HOUR/MINUTE` | время суточной проверки архива | 09:00 |
| `MAX_CONCURRENT_POLLS` | лимит одновременных подключений | 10 |
| `NVR_UNREACHABLE_THRESHOLD` | циклов до алерта недоступности | 3 |
| `CAMERA_OFFLINE_ALERT_MINUTES` | минут offline до алерта | 10 |
| `TIME_DRIFT_ALERT_MINUTES` | порог дрейфа часов NVR | 5 |
| `ARCHIVE_GAP_ALERT_MINUTES` | порог «дыры» в архиве | 60 |
| `HDD_USAGE_ALERT_PERCENT` | порог заполнения HDD (0 = выкл.) | 0 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram-бот | — |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | вход в панель (пусто = открыта) | admin / — |
| `TZ` | часовой пояс сервера = пояс регистраторов | UTC |
| `MOCK_MODE` | смонтировать эмулятор на `/mock` | false |

## API (основное)

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/` | дашборд |
| `GET` | `/devices/{id}` | страница устройства |
| `POST` | `/api/devices/test` | тест соединения + автоопределение |
| `GET/POST` | `/api/devices` | список / создание устройства |
| `PUT/DELETE` | `/api/devices/{id}` | изменение / удаление |
| `POST` | `/api/devices/{id}/poll` | опросить сейчас |
| `POST` | `/api/devices/{id}/archive-check` | проверить архив (вчера или `?day=`) |
| `GET` | `/api/devices/{id}/archive` | календарь архива |
| `GET` | `/api/summary` | сводка |
| `GET` | `/api/events` | лог событий (фильтры `device_id`, `severity`) |

## Поддерживаемые эндпоинты NVR

**Hikvision / HiWatch (ISAPI):** `/ISAPI/System/deviceInfo`,
`/ISAPI/ContentMgmt/InputProxy/channels/status`, `/ISAPI/System/Video/inputs/channels`
(аналоговые/гибридные, videoloss), `/ISAPI/ContentMgmt/Storage/hdd`,
`/ISAPI/ContentMgmt/search`, `/ISAPI/System/time`.

**Dahua / RVI (CGI):** `magicBox.cgi` (getDeviceType/getSystemInfo/getSoftwareVersion),
`api/LogicDeviceManager/getCameraState` (+ fallback `eventManager.cgi` VideoLoss),
`storageDevice.cgi`, фабрика `mediaFileFind.cgi`, `global.cgi` (getCurrentTime).

## Тесты

```bash
pip install -r requirements.txt
pytest            # все тесты идут против встроенного mock-сервера NVR
```

## Этапность

1. **MVP** — устройства + опрос каналов + Telegram-алерты ✅
2. **HDD** — мониторинг дисков и алерты ✅
3. **Архив** — суточная проверка, календарь, контроль дыр + проверка времени ✅

Дальнейшее: ONVIF-драйвер (заглушка в `drivers/`), стрим событий videoloss,
аутентификация в веб-панель, Alembic-миграции.
