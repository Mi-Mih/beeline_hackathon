# 🚀 Локальный запуск

Ниже описаны два сценария:

1. быстрый запуск обычным Python с матрицей поездок из входного JSON;
2. полный запуск через Docker Compose с локальным OSRM.

Все команды выполняются из корня репозитория.

## 🐍 1. Запуск обычным Python

### 📋 Требования

- Python 3.12 или новее;
- `pip` и модуль `venv`;
- для frontend-тестов — Node.js (необязательно).

### 📦 Установка

Linux/macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

### 🌐 Запуск веб-сервиса в демо-режиме

В демо-режиме backend использует готовую матрицу `travel.legs` из входного
JSON и не требует OSRM.

Linux/macOS:

```bash
ROUTING_MATRIX_MODE=input python -m backend --host 127.0.0.1 --port 4173
```

Windows PowerShell:

```powershell
$env:ROUTING_MATRIX_MODE = "input"
python -m backend --host 127.0.0.1 --port 4173
```

Открыть <http://127.0.0.1:4173>. Остановить процесс можно сочетанием `Ctrl+C`.

Для публичного поиска только тестовых адресов и дорожной геометрии встроенных
демо-наборов можно дополнительно задать `GEOCODING_MODE=public_demo`. Не
используйте этот режим с клиентскими данными. В Windows эквивалентный сценарий
уже оформлен в `scripts/start-demo.ps1`:

```powershell
.\scripts\start-demo.ps1
```

Без `public_demo` поиск адресов ожидает локальный геокодер по адресу
`http://127.0.0.1:4190`; сам расчёт по готовому примеру при этом работает.

### 🧮 Запуск только планировщика

```bash
python -m backend.math_core examples/sample-input.json > plan.json
```

После установки пакета доступна та же команда через entry point:

```bash
beeline-planner examples/sample-input.json > plan.json
```

Для перепланирования можно передать `--reason replan`. Вместо имени файла
разрешён `-`, тогда JSON читается из stdin.

### ✅ Проверка

В другом терминале:

```bash
curl http://127.0.0.1:4173/api/health
curl http://127.0.0.1:4173/api/demo
```

Ожидаемый health-ответ:

```json
{"status":"ok","contract":"plan-output/1.0"}
```

Запуск тестов:

```bash
python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs
```

Вторую команду можно пропустить, если Node.js не установлен.

## 🐳 2. Запуск через Docker Compose с OSRM

Этот сценарий поднимает четыре компонента: `backend`, `routing-gateway`,
`osrm-car` и, при необходимости, `osrm-foot`. Он использует реальную дорожную
матрицу, поэтому до первого `compose up` необходимо собрать граф Москвы.

### 📋 Требования

- Linux;
- Docker Engine с Compose plugin;
- Python 3.12 для первого запуска builder;
- достаточно диска и памяти для исходного PBF и сборки OSRM; лимит runtime-
  контейнера по умолчанию — 4 ГБ.

Docker socket монтируется в backend для управляемого обновления графа. Такой
контур следует запускать только на доверенном локальном хосте.

### ⚙️ Подготовка окружения

Установить Python-зависимости, как в разделе выше, затем создать локальный
каталог данных:

```bash
mkdir -p .runtime/routing/{config,sources,quarantine,versions,state,logs}
cp deploy/routing/routing.env.example .runtime/routing.env
```

Отредактировать `.runtime/routing.env`. Для запуска из репозитория минимально
нужно заменить абсолютные серверные пути:

```dotenv
ROUTING_DATA_ROOT=/absolute/path/to/repository/.runtime/routing
ROUTING_STATE_DB=/absolute/path/to/repository/.runtime/routing/state/routing.sqlite3
ROUTING_UPLOAD_DIR=/absolute/path/to/repository/.runtime/routing/quarantine
ROUTING_UPDATE_COMMAND="python /absolute/path/to/repository/scripts/build_moscow_osrm.py --compose-file /absolute/path/to/repository/deploy/routing/compose.yml"
```

Путь должен быть абсолютным. Если пешеходные маршруты не нужны, установить:

```dotenv
ROUTING_WITH_FOOT=0
OSRM_FOOT_URL=
```

Если они нужны, оставить `ROUTING_WITH_FOOT=1` и
`OSRM_FOOT_URL=http://osrm-foot:5000`, а команды Compose запускать с профилем
`foot`.

### 🗺️ Первая сборка дорожного графа

Собрать служебный образ Osmium:

```bash
docker build -f deploy/routing/Dockerfile.osmium -t beeline-osmium:local .
```

Загрузить переменные из env-файла, создать SQLite и запустить builder:

```bash
set -a
source .runtime/routing.env
set +a
python -c 'from backend.map_manager import manager_from_env; manager_from_env()'
python scripts/build_moscow_osrm.py --job-id initial
```

Builder скачивает PBF Центрального федерального округа, извлекает Москву,
строит MLD-графы, проверяет их и создаёт ссылку
`$ROUTING_DATA_ROOT/active`. Операция требует доступа в интернет и может занять
значительное время. Если доступен заранее подготовленный граф, каталог
`active` должен содержать `car/moscow.osrm*` и `manifest.json`; для включённого
пешеходного профиля также нужен `foot/moscow.osrm*`.

### ▶️ Запуск контейнеров

Без пешеходного профиля:

```bash
docker compose --env-file .runtime/routing.env \
  -f deploy/routing/compose.yml up -d --build --wait
```

С пешеходным профилем:

```bash
docker compose --env-file .runtime/routing.env \
  -f deploy/routing/compose.yml --profile foot up -d --build --wait
```

Сервисы публикуются только на loopback:

- интерфейс и API: <http://127.0.0.1:4173>;
- Routing Gateway: <http://127.0.0.1:4180>;
- OSRM доступен только внутри сети Compose.

### ✅ Проверка Docker-развёртывания

```bash
docker compose --env-file .runtime/routing.env \
  -f deploy/routing/compose.yml ps
curl http://127.0.0.1:4173/api/health
curl http://127.0.0.1:4180/health
```

Логи всех компонентов:

```bash
docker compose --env-file .runtime/routing.env \
  -f deploy/routing/compose.yml logs -f
```

### ⏹️ Остановка

```bash
docker compose --env-file .runtime/routing.env \
  -f deploy/routing/compose.yml down
```

Команда не удаляет граф, SQLite и загруженные PBF: они находятся в
`ROUTING_DATA_ROOT`. Для последующего запуска повторная сборка графа не нужна.

## 🔧 Основные переменные окружения

| Переменная | Значение и назначение |
| --- | --- |
| `ROUTING_MATRIX_MODE` | `input` — матрица из JSON; `gateway` — обязательная матрица из Routing Gateway. |
| `ROUTING_GATEWAY_URL` | URL gateway для backend; по умолчанию `http://127.0.0.1:4180`. |
| `ROUTING_DATA_ROOT` | Постоянный каталог исходников, версий графа, состояния и загрузок. |
| `ROUTING_STATE_DB` | SQLite Map Manager; по умолчанию `.runtime/routing.sqlite3`. |
| `TRAVEL_HISTORY_DB` | Отдельная SQLite фактических поездок; без значения используется база Map Manager. |
| `ROUTING_UPDATE_COMMAND` | Фиксированная команда builder для обновлений из UI. |
| `ROUTING_WITH_FOOT` | Строить пешеходный граф: `1` или `0`. |
| `OSRM_CAR_URL`, `OSRM_FOOT_URL` | Адреса профилей OSRM для gateway. |
| `BACKEND_PORT` | Порт backend на хосте в Compose, по умолчанию `4173`. |
| `ROUTING_GATEWAY_PORT` | Порт gateway на хосте в Compose, по умолчанию `4180`. |
| `GEOCODING_MODE` | `local` (по умолчанию) или только для тестов `public_demo`. |
| `LOCAL_GEOCODER_URL` | Локальный Nominatim, по умолчанию `http://127.0.0.1:4190`. |

Полный пример переменных: `deploy/routing/routing.env.example`.

## 🩺 Типовые проблемы

`routing gateway is unavailable`
: Backend запущен в режиме `gateway`, но gateway/OSRM недоступен. Для быстрого
  демо используйте `ROUTING_MATRIX_MODE=input`; для штатного режима проверьте
  `/health` gateway и контейнер `osrm-car`.

Compose не может смонтировать `active/manifest.json`
: Дорожный граф ещё не построен или `ROUTING_DATA_ROOT` указывает не на тот
  абсолютный каталог. Выполните первую сборку и проверьте ссылку `active`.

Поиск адреса отвечает ошибкой, но пример рассчитывается
: Это ожидаемо без локального Nominatim. Геокодер не нужен для расчёта уже
  подготовленного JSON с координатами и матрицей.

`No module named backend` или не найден `ortools`
: Активируйте `.venv`, выполняйте команды из корня репозитория и повторите
  `python -m pip install -e '.[test]'`.

Порт 4173 или 4180 занят
: Остановите другой процесс либо задайте `BACKEND_PORT` и
  `ROUTING_GATEWAY_PORT` в `.runtime/routing.env`. Для Python используйте
  аргумент `--port`.

Дополнительные production-операции, расписание обновлений и rollback описаны в
[`deploy/routing/README.md`](deploy/routing/README.md).
