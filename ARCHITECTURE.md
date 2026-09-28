# 🏗️ Архитектура проекта

Техническое описание текущей реализации планировщика выездов инженеров. Проект рассчитан на Python 3.12+, использует OR-Tools для построения маршрутов, стандартную библиотеку Python для HTTP и отдельный OSRM MLD для дорожных расстояний. Основной сценарий развёртывания — один Linux-сервер с Docker Compose.

Общее описание продукта приведено в [`README.md`](README.md), а команды
локального запуска и развёртывания — в [`RUN.md`](RUN.md).

## 🔄 Схема взаимодействия

```text
Браузер (frontend/)
    │ GET /, /api/demo, /api/map/*, /api/travel-observations/status
    │ POST /api/plan, /api/travel-observations/import; GET /api/route
    ▼
Backend HTTP (backend/web.py, порт 4173)
    ├── нормализация и проверка входа → backend/math_core/ → plan-output 1.0
    ├── режим gateway → Routing Gateway (порт 4180) → OSRM Table
    ├── геометрия карты → Routing Gateway → OSRM Route
    └── Map Manager → SQLite и фоновый builder → Docker Compose/OSRM
                                                │
                                      osrm-car / osrm-foot (порт 5000
                                      только внутри сети Compose)
```

В браузере нет собственного солвера. `frontend/` получает JSON от backend и отображает назначения, список заявок, карту и управление версиями дорожного графа. `backend/math_core/` не обращается к HTTP и OSRM: на вход ему передают уже готовую матрицу `travel.legs`.

## 🗂️ Каталоги и ответственность

| Путь | Назначение |
| --- | --- |
| `backend/math_core/models.py`, `contract.py` | Объекты задачи, разбор и семантическая проверка `plan-input 1.0`. |
| `backend/math_core/select.py`, `routing.py` | Выбор планировщика и основной open VRPTW на OR-Tools Routing. |
| `backend/math_core/replan.py` | Логика событий в течение дня: вставка обычной заявки в прежний план или пересчёт при аварии. |
| `backend/math_core/greedy.py`, `local_search.py`, `assignment.py`, `simulate.py` | Начальное решение, резервный поиск, переназначение полных маршрутов и проверка допустимости/времени. |
| `backend/math_core/cli.py` | Запуск планирования из файла или stdin без HTTP. |
| `backend/web.py` | HTTP API диспетчера, выдача файлов `frontend/`, вызов солвера, проксирование геометрии и API карты. |
| `backend/routing_gateway.py` | Внутренний HTTP шлюз к OSRM Table/Route; проверка полноты матриц, перевод матриц в `travel.legs`. |
| `backend/map_manager.py` | SQLite-хранилище версий, заданий и расписания; запуск/отмена фоновой сборки. |
| `backend/audit_data.py` | Проверка исходных CSV; не участвует в обслуживании запросов. |
| `backend/travel_matrix.py` | Экспериментальный расчёт contextual-матрицы; не используется в штатном `/api/plan`. |
| `frontend/` | Статический интерфейс на HTML/CSS/JavaScript и MapLibre GL JS. Отдельного Node-сервиса или сборки нет. |
| `contracts/` | JSON Schema для версий `plan-input 1.0` и `plan-output 1.0`. |
| `config/work-normatives.json`, `data/` | Нормативы и исходные данные для подготовки/аудита, не база данных заявок работающего API. |
| `examples/` | Ручной пример и воспроизводимые синтетические входы; их координаты не являются реальной дорожной географией. |
| `scripts/build_moscow_osrm.py` | Загрузка/приём PBF, извлечение Москвы, сборка MLD-графа, активация версии и проверка маршрутизации. |
| `scripts/generate_run_inputs.py`, `scripts/build_travel_matrix.py` | Генерация тестовых входов и экспериментальная contextual-сборка матрицы. |
| `deploy/routing/` | Compose, образы backend/gateway/Osmium, пример окружения и systemd timer. |
| `tests/` | Тесты контракта, планирования, HTTP, матрицы и обновления карты. |

## 🌐 Сервисы и порты

| Сервис Compose | Процесс | Роль и доступ |
| --- | --- | --- |
| `backend` | `python -m backend --host 0.0.0.0 --port 4173` | UI и публичное для локального сервера API. Порт хоста `127.0.0.1:${BACKEND_PORT:-4173}`. В Compose установлен `ROUTING_MATRIX_MODE=gateway`. |
| `routing-gateway` | `python -m backend.routing_gateway --host 0.0.0.0 --port 4180` | Матрица и геометрия через приватный OSRM. Порт хоста `127.0.0.1:${ROUTING_GATEWAY_PORT:-4180}`; backend обращается по `http://routing-gateway:4180`. |
| `osrm-car` | `osrm-routed --algorithm mld ... /data/car/moscow.osrm` | Автомобильный граф; порт 5000 доступен только сервисам Compose. |
| `osrm-foot` | `osrm-routed --algorithm mld ... /data/foot/moscow.osrm` | Пешеходный граф; запускается с профилем Compose `foot`. Для его использования также задать `ROUTING_WITH_FOOT=1` и `OSRM_FOOT_URL=http://osrm-foot:5000`. |
| builder | `scripts/build_moscow_osrm.py` | Запускается по заданию Map Manager или вручную, постоянного HTTP-сервиса нет. |
| scheduler | `beeline-map-scheduler` из systemd timer | Раз в час проверяет сохранённое расписание и при необходимости создаёт задание; это не сервис Compose. |

Образы `backend` и `routing-gateway` описаны в `deploy/routing/Dockerfile.backend` и `Dockerfile`; служебный образ `beeline-osmium:local` — в `Dockerfile.osmium`. Compose ждёт успешный `/health` gateway перед запуском backend. Оба опубликованных порта привязаны к loopback, поэтому удалённый доступ требует отдельной сетевой настройки.

## 📊 Поток данных при планировании

1. Клиент передаёт `POST /api/plan` со снимком `plan-input 1.0`: `plan_id`, `locations`, `requests`, `technicians`, `travel` и, при необходимости, `planning_reason`/`replan_context`. Тело ограничено 2 МБ. В CLI читается тот же формат из файла или stdin.
2. По умолчанию backend работает в режиме `ROUTING_MATRIX_MODE=gateway`: координаты обязательны у всех `locations`, backend запрашивает у gateway полную направленную Table-матрицу для каждого вида транспорта инженеров и заменяет присланный `travel`. Режим `input` оставлен для изолированной разработки и передаёт матрицу из запроса в `parse_problem` без обращения к gateway.
3. Gateway вызывает отдельный профиль OSRM для `car` и, если настроен, `pedestrian`. Неполная матрица, недоступный движок или неизвестный профиль дают ошибку; расчёт не подменяет дорожные дуги прямыми расстояниями. Времена округляются вверх до минут, расстояния — до километров с тремя знаками. Диагональ равна нулю.
4. `parse_problem` проверяет ссылки, времена, требования и матрицу. Если клиент передал `use_travel_history: true`, backend назначает каждому инженеру с сопоставимыми наблюдениями его медианный экспериментальный коэффициент времени; остальные получают коэффициент 1. Затем `planner_for()` выбирает `RoutingPlanner`. Штатный расчёт использует OR-Tools Routing с лимитом по умолчанию 5 секунд. Если импорт OR-Tools невозможен или решение не получено, используется greedy + local search, а причина записывается в `diagnostics.warnings`.
5. Маршрут открыт: поездка обратно в офис после последней заявки не добавляется. Проверяются навыки, транспорт, оборудование, ручная фиксация инженера, окно начала работ и конец смены. Приоритеты пропуска заявок: `urgent`, затем `high`, затем `normal`.
6. Результат содержит `routes` с временами остановок, `unassigned` с кодами причин, агрегированные `metrics`, `algorithm`, `diagnostics` и `travel_time_calculation` с выбранным режимом и применёнными коэффициентами. В режиме `gateway` backend дополнительно добавляет `routing` с версиями графа/профилей.

Схемы находятся в `contracts/`. В режиме `gateway` ответ содержит описанное в `plan-output.schema.json` верхнеуровневое поле `routing` с версиями графа и профилей.

### 🔁 Перепланирование

`planning_reason: "replan"` без `replan_context` пересчитывает переданный снимок целиком. Для события в течение смены клиент передаёт `replan_context.event_at`, `new_request_ids`, `previous_routes` и при необходимости `active_work`. Вход должен содержать только оставшиеся заявки; у инженеров обновляются стартовая точка и `available_from` с учётом текущей работы.

При обычной новой заявке `backend/math_core/replan.py` сохраняет прежние назначения и порядок, вставляя новые заявки в допустимые места. Если среди новых заявок есть `urgent`, оставшийся снимок пересчитывается через RoutingPlanner; реакция свыше 120 минут добавляет предупреждение, а не жёсткий запрет. UI строит такие сценарии из текущего плана и сравнивает результаты по `request_id`.

### 🗺️ Геометрия для карты

Браузер запрашивает `GET /api/route?points=lon,lat;lon,lat...` у backend. Backend проверяет 2–25 точек, кэширует ответ по активной версии графа и обращается к Route API через gateway. Геометрия нужна для отображения; план считает километры и минуты только по `travel`. Если маршрут для рисунка недоступен, UI может соединить точки прямой, не меняя чисел плана. Подложка MapLibre загружает тайлы OpenStreetMap.

## 🔌 HTTP API

| Метод и путь | Назначение |
| --- | --- |
| `GET /`, статические файлы | Интерфейс из `frontend/`. |
| `GET /api/health` | Проверка процесса backend. |
| `GET /api/demo` | Содержимое `examples/sample-input.json`. |
| `POST /api/plan` | Построение плана. Необязательный `use_travel_history: true` применяет персональные коэффициенты времени. Ошибка контракта или маршрутизации возвращается в `error`. |
| `GET /api/route?points=...&mode=...` | Геометрия маршрута OSRM. Ответ содержит `duration_seconds` и `duration_minutes`. Если gateway вернул только минуты, backend дописывает секунды как `duration_minutes × 60`. |
| `GET /api/travel-observations/status` | Сводка сохранённых фактических поездок и экспериментальных коэффициентов. |
| `POST /api/travel-observations/import` | Идемпотентный приём CSV или JSON с наблюдениями. Лимит — 5 МБ. |
| `GET /api/map/status`, `GET /api/map/jobs` | Активная карта, расписание и история заданий. |
| `POST /api/map/updates` | Запуск загрузки и сборки новой карты. |
| `POST /api/map/uploads` | Приём `.osm.pbf` как `application/octet-stream`, затем сборка. Лимит — `ROUTING_MAX_UPLOAD_BYTES`. |
| `PUT /api/map/schedule` | Сохранение расписания (`enabled`, `interval_days` 1–90, `local_time` HH:MM, время Москвы). |
| `DELETE /api/map/jobs/{id}` | Отмена активного задания. |
| `GET /health` gateway | Проверка автомобильной и настроенной пешеходной матриц на двух контрольных точках. |
| `POST /internal/routing/v1/matrix` gateway | Направленная матрица времени и расстояния для указанных координат и режима; лимит OSRM по умолчанию 100 точек. |
| `GET /internal/routing/v1/route` gateway | GeoJSON-геометрия маршрута OSRM. |

В `backend/web.py` нет слоя аутентификации для изменяющих `/api/map/*`. Compose даёт backend доступ к `/var/run/docker.sock` для обновления OSRM. При публикации UI за пределами доверенной сети нужно закрыть эти маршруты существующим механизмом доступа. `/api/health` проверяет сам backend, а `/health` gateway действительно обращается к OSRM.

## 🧭 Состояние карты и обновление

`MapStore` хранит `settings`, `map_versions` и `update_jobs` в SQLite (`ROUTING_STATE_DB`). `TravelHistoryStore` в той же базе хранит проверенные фактические переезды инженеров из окна «Калибровка времени»; пакетная загрузка CSV/JSON доступна через `POST /api/travel-observations/import`. Отдельную базу можно задать через `TRAVEL_HISTORY_DB`. Наблюдения сами план не меняют. Переключатель «Без калибровки» / «С калибровкой» только выбирает, передаст ли следующий `POST /api/plan` поле `use_travel_history`. При `true` каждому инженеру с сопоставимыми наблюдениями назначается `travel_time_factor`, и `calibrated_travel_minutes` умножает минуты дуги на этот коэффициент с округлением вверх; нулевая дуга остаётся нулевой. Ответ дополняется `travel_time_calculation`. Статические ответы и JSON API отдают `Cache-Control: no-store`. `MapManager` разрешает одно активное задание и запускает заранее заданную `ROUTING_UPDATE_COMMAND`; интерфейс не формирует shell-команду. По умолчанию без этой переменной задание завершится ошибкой.

Builder использует загруженный PBF или скачивает Central Federal District PBF Geofabrik в `ROUTING_DATA_ROOT/sources`. Для выреза Москвы берётся `config/moscow.geojson` внутри корня данных; если его нет, извлекается административная граница OSM relation `102269`. Затем Osmium создаёт московский PBF, OSRM собирает car и опционально foot через `extract → partition → customize`, после чего запускается пробный `osrm-routed`.

Артефакты и SHA-256 источника/выреза/полигона записываются в `versions/<version>/manifest.json`. `active` — символьная ссылка на версию, `previous` — ссылка на прошлую. После переключения builder пересоздаёт контейнеры OSRM и gateway, проверяет матрицу на координатах из `examples/` и расстояние привязки к графу (`ROUTING_MAX_SNAP_METERS`, по умолчанию 1000 м). При ошибке проверки он возвращает ссылку `active` к предыдущей версии и пересоздаёт сервисы; успешную активацию пишет в SQLite. В каталоге данных также используются `state/` и `quarantine/` для базы и загрузок.

Systemd timer `deploy/routing/beeline-map-scheduler.timer` срабатывает ежечасно. Сервис читает сохранённое расписание в часовом поясе `Europe/Moscow`, проверяет интервал и запускает тот же builder. Таймер и unit-файл приведены рядом с Compose.

## 💻 Запуск локально: солвер и прототип

Из корня репозитория:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m backend.math_core examples/sample-input.json > plan.json
python -m backend --host 127.0.0.1 --port 4173
```

Открыть `http://127.0.0.1:4173`. По умолчанию backend использует режим
`ROUTING_MATRIX_MODE=gateway` и ожидает доступный Routing Gateway. Для запуска
без OSRM нужно явно задать `ROUTING_MATRIX_MODE=input`; тогда расчёт использует
матрицу из входного JSON. Команды после установки пакета: `beeline-planner`,
`beeline-web`, `beeline-audit`, `beeline-routing-gateway`,
`beeline-map-scheduler`. Проверка исходных CSV: `python -m backend.audit_data`.
Тесты: `python -m unittest discover -s tests -v`.

Для собственного JSON: `python -m backend.math_core input.json --reason overnight` или `--reason replan`. Флаг переопределяет `planning_reason` во входе. Локальный прототип может показать пример без OSRM; дорожная геометрия при этом зависит от доступности gateway. Синтетические входы можно пересобрать командой `python scripts/generate_run_inputs.py`.

## 🐳 Запуск с дорожным графом и Compose

Нужны Docker с Compose plugin, место для PBF/OSRM-графов и права на каталог данных и Docker socket. Подробная операционная инструкция: `deploy/routing/README.md`.

1. Создать каталог данных с подкаталогами `config`, `sources`, `quarantine`, `versions`, `state`, `logs`. При желании поместить утверждённый полигон в `config/moscow.geojson`.
2. Скопировать `deploy/routing/routing.env.example` в свой env-файл и установить абсолютный `ROUTING_DATA_ROOT`. Для пешеходных заявок оставить `ROUTING_WITH_FOOT=1` и `OSRM_FOOT_URL=http://osrm-foot:5000`. Этот файл содержит пример одновременно для хостового builder и Compose; Compose задаёт часть переменных сервисам отдельно.
3. Собрать служебный образ: `docker build -f deploy/routing/Dockerfile.osmium -t beeline-osmium:local .`.
4. Загрузить переменные env в shell, инициализировать таблицы SQLite и выполнить первый builder из корня проекта. Он сам поднимает OSRM/gateway и проверяет их. Для запуска в контейнере backend Map Manager уже передаёт builder абсолютный `--compose-file`.

```bash
set -a
. /путь/к/routing.env
set +a
python -c 'from backend.map_manager import manager_from_env; manager_from_env()'
python scripts/build_moscow_osrm.py --job-id initial
```

5. Поднять остальную связку из корня проекта:

```bash
docker compose --env-file /путь/к/routing.env \
  -f deploy/routing/compose.yml --profile foot up -d --build --wait
curl http://127.0.0.1:4180/health
curl http://127.0.0.1:4173/api/health
```

Если пешеходный профиль не нужен, убрать `--profile foot` и выставить `ROUTING_WITH_FOOT=0`, `OSRM_FOOT_URL=`. До первого запуска Compose в `ROUTING_DATA_ROOT/active` должен находиться готовый граф с `manifest.json`; файл монтируется gateway. Для серверного расписания установить файлы `beeline-map-scheduler.service` и `.timer` в `/etc/systemd/system/`, затем выполнить `systemctl daemon-reload` и `systemctl enable --now beeline-map-scheduler.timer`.

### ⚙️ Важные переменные

| Переменная | Использование |
| --- | --- |
| `ROUTING_MATRIX_MODE` | `gateway` по умолчанию; `input` включает использование матрицы из входного JSON. |
| `ROUTING_GATEWAY_URL` | Адрес gateway для backend и проверки builder. |
| `ROUTING_DATA_ROOT` | Корень артефактов карты; в Compose по умолчанию `/srv/beeline-routing`. |
| `ROUTING_STATE_DB`, `ROUTING_UPLOAD_DIR` | SQLite и приём PBF. |
| `TRAVEL_HISTORY_DB` | Необязательная отдельная SQLite для фактических поездок; по умолчанию используется `ROUTING_STATE_DB`. |
| `ROUTING_UPDATE_COMMAND` | Фиксированная команда запуска builder из Map Manager. |
| `ROUTING_WITH_FOOT`, `OSRM_FOOT_URL` | Сборка и использование отдельного пешеходного графа. |
| `OSRM_CAR_URL`, `OSRM_MAX_TABLE_SIZE` | Адрес автомобильного OSRM и лимит размера Table API у gateway. |
| `OSRM_IMAGE`, `OSMIUM_IMAGE`, `OSRM_RUNTIME_MEMORY` | Образы и лимит памяти OSRM. |
| `ROUTING_MAX_UPLOAD_BYTES`, `ROUTING_MAX_SNAP_METERS` | Лимит загрузки PBF и допустимая привязка контрольных точек к графу. |
| `BACKEND_PORT`, `ROUTING_GATEWAY_PORT` | Опубликованные loopback-порты Compose. |

## ⚠️ Границы текущей реализации

- Входные CSV не содержат координат и дорожной матрицы. UI может отдельно
  запросить координаты у настроенного геокодера, но планировщик не выводит их из
  адреса: для режима `gateway` координаты должны быть подготовлены до вызова API.
- OSRM-граф статичен и сам по себе не учитывает пробки в реальном времени. Экспериментальный `backend/travel_matrix.py` не подключён к штатному планированию.
- Матрица строится одним запросом Table API на профиль и ограничена числом точек, настроенным в gateway/OSRM (по умолчанию 100). При недоступной паре построение плана прекращается.
- Состояние Map Manager локально для одного узла. В HTTP-прототипе нет хранилища планов/заявок и механизма авторизации; клиент передаёт полный снимок при каждом расчёте.

Дополнительные детали: `docs/accepted-model.md` (матмодель),
`docs/experiment-results.md` (проверенные гипотезы), `docs/travel-matrix.md` и
`docs/distance-definition.md` (матрица и единицы измерения),
`deploy/routing/README.md` (операции с OSRM).
