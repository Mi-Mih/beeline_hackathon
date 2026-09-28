# Дорожный движок как отдельный сервис: итоговый ресерч и план

Дата актуализации: 21 сентября 2026 года.

Статус: проектный план. Реализация не начиналась и не разрешена до отдельной
команды заказчика. Замечания технического ревью встроены в план, отклонённые
перечислены в разделе 14.

## 1. Подтверждённые требования

- Целевая среда — один Linux-сервер.
- Обслуживается одна карта региона.
- Одна карта может покрывать несколько зон планирования. `Region` движка — это
  OSM-покрытие, а не зона солвера «Восток», «Юго-восток» или «Югоцентр».
- Поддерживаются все виды транспорта, которые фактически присутствуют в
  согласованном production-входе. Список нельзя выводить из демонстрационного
  файла: в generated-inputs сейчас только `car`, а `pedestrian` встречается в
  sample-input.
- Пользователь может либо загрузить локальный `.osm.pbf`, либо выбрать
  настроенный источник региона для скачивания сервером.
- Пользователь может запустить обновление кнопкой или включить расписание.
- Обновление никогда не запускается автоматически перед планированием и не
  блокирует расчёт на активном графе.
- Приоритет — быстрый пакетный Matrix/Table API. Сравнение нескольких
  альтернатив для каждой пары не является обязательным production-правилом.
- SLA матрицы пока не определён: сначала нужны измерения на фактическом регионе.
- Решение о нескольких временных поясах пока не принято.

«Бесплатный» означает отсутствие платы за лицензию движка и внешний routing
API. Сервер, диски, трафик и эксплуатация остаются платными ресурсами.

## 2. Границы функциональности

Сервис отвечает за:

- хранение активной и предыдущих версий дорожного графа;
- ручное и плановое обновление OSM PBF;
- построение и проверку графа;
- быстрые матрицы времени и расстояния;
- геометрию маршрута для UI, чтобы production не зависел от публичного
  `router.project-osrm.org`;
- версионирование, активацию и rollback.

Сервис **не является геокодером**. CSV задачи содержит адреса, а routing engine
принимает координаты. Геокодирование, проверка адреса и сохранение координат —
отдельный обязательный трек данных до вызова Matrix API.

Live-пробки не входят в подтверждённое требование. Известные плановые события и
исторические скорости можно добавить через отдельную версию весов, не связывая
их обновление с запуском плана.

## 3. Сравнение движков

### 3.1. OSRM

OSRM — высокопроизводительный C++-движок под BSD-2-Clause. Table API возвращает
матрицы duration и distance для выбранных движком маршрутов.

Плюсы:

- специализированный пакетный Table API;
- расстояния и длительности в одном запросе;
- permissive-лицензия;
- официальные Docker-образы;
- MLD поддерживает повторный `osrm-customize` для новых скоростей/штрафов;
- `osrm-datastore` допускает reload подготовленного набора через shared memory;
- дефолтный `--max-table-size` равен 100 координатам, поэтому известный снимок
  из 84 точек помещается в один запрос без изменения лимита.

Ограничения:

- изменение топологии требует нового `extract`;
- транспортный профиль применяется на этапе подготовки;
- для `car` и `foot` нужны отдельные графы/экземпляры за общим gateway;
- Table возвращает один выбранный путь, но не альтернативы и не прежний
  `Score(r)` адаптера;
- статический Table сам по себе не делает время зависимым от часа выезда.

MLD — основной кандидат. CH также измеряется в POC, потому что официальный OSRM
указывает CH как возможный выигрыш для очень больших матриц. Выбор делается по
нашим измерениям, а не по умолчанию движка.

Источники:

- [OSRM README](https://github.com/Project-OSRM/osrm-backend);
- [Table API](https://github.com/Project-OSRM/osrm-backend/blob/master/docs/http.md#table-service);
- [инструменты OSRM](https://github.com/Project-OSRM/osrm-backend/blob/master/docs/tools.md);
- [профили OSRM](https://github.com/Project-OSRM/osrm-backend/blob/master/docs/profiles.md);
- [лицензия OSRM](https://github.com/Project-OSRM/osrm-backend/blob/master/LICENSE.TXT).

### 3.2. Valhalla

Valhalla — C++-движок под MIT с динамической стоимостью маршрута,
несколькими режимами движения и Matrix API.

Плюсы:

- один тайловый граф поддерживает несколько costing-моделей;
- стоимость настраивается во время запроса;
- Matrix API возвращает time, distance и cost;
- поддерживаются `date_time` и time-dependent алгоритмы;
- permissive-лицензия.

Ограничения:

- time-dependent расчёт не создаёт исторические скорости из OSM: для него
  нужен отдельный источник predicted/historical traffic или собственная модель;
- exact `timedistancematrix` медленнее быстрого `costmatrix`;
- API имеет ограничения на некоторые сочетания числа sources/targets и типа
  departure/arrival time;
- сборка и эксплуатация сложнее минимального OSRM;
- официального подтверждения безопасного инкрементального обновления топологии
  только изменившихся routing tiles недостаточно, поэтому в плане нельзя
  считать partial rebuild гарантированной возможностью.

Valhalla остаётся сравнительным кандидатом POC, но не считается автоматически
лучше при выборе времязависимости.

Источники:

- [Valhalla и лицензия](https://github.com/valhalla/valhalla);
- [Matrix API](https://valhalla.github.io/valhalla/api/matrix/);
- [построение tiles](https://valhalla.github.io/valhalla/start/building/);
- [Mjolnir](https://valhalla.github.io/valhalla/contributing/architecture/mjolnir/).

### 3.3. GraphHopper

Open-source ядро GraphHopper лицензировано под Apache 2.0, но официальный
готовый Matrix API относится к коммерческому Directions API. Повторять матрицу
через одиночные route-вызовы не отвечает требованию быстрого бесплатного
сервиса.

Вывод: не включать GraphHopper core в первый POC.

Источник: [GraphHopper](https://github.com/graphhopper/graphhopper).

### 3.4. openrouteservice

openrouteservice — self-hosted надстройка над GraphHopper, которая добавляет
собственный Matrix API и несколько профилей. Это объясняет, почему ORS можно
рассматривать, а GraphHopper core без Matrix API — нет.

Плюсы:

- готовые route/matrix endpoints;
- несколько профилей;
- health/status API;
- официально описанная двухинстансная схема обновления.

Ограничения:

- JVM и более высокая потребность в RAM;
- стандартный `maximum_routes=2500` считает элементы матрицы. Полный запрос
  84×84 содержит 7056 элементов, хотя солверу нужны только 6972 недиагональные
  дуги;
- функционально избыточен, если нужны только route/table;
- комплект лицензий зависимостей требует отдельной проверки.

ORS остаётся резервным вариантом, но не получает POC-слот раньше OSRM и
Valhalla.

Источники:

- [self-hosting ORS](https://giscience.github.io/openrouteservice/run-instance/);
- [Matrix endpoint](https://giscience.github.io/openrouteservice/run-instance/configuration/endpoints/matrix);
- [system requirements](https://giscience.github.io/openrouteservice/run-instance/system-requirements);
- [license notice](https://github.com/GIScience/openrouteservice/blob/main/NOTICE.md).

### 3.5. Предварительная рекомендация

Основной production-кандидат — **OSRM Table**. POC сравнивает:

1. OSRM MLD;
2. OSRM CH;
3. Valhalla как альтернативный engine-neutral runtime.

GraphHopper исключён, ORS отложен. Финальный выбор фиксируется ADR после
измерений скорости, ресурсов, качества матрицы и качества построенного плана.

## 4. Что меняется относительно текущего кода

`backend/travel_matrix.py` сейчас делает отдельный Route-запрос для каждой
направленной пары и получает до трёх альтернатив в ответе. Для 84 точек это
`84 × 83 = 6972` HTTP-запроса на один mode.

OSRM Route принимает много координат как последовательные waypoints одного
маршрута. Он не превращает их в пакет независимых origin-destination пар с
отдельными альтернативами. Поэтому уменьшить 6972 независимых запроса до
примерно 280 без смены семантики нельзя.

Production MatrixProvider должен использовать Table API. Это не просто
оптимизация транспорта данных: Table меняет определение маршрута.

Текущий `contextual-multiroute-v1`:

```text
до трёх альтернатив
→ эвристическая классификация сегментов по name/ref
→ E(r), U(r), D(r)
→ argmin Score(r)
```

OSRM Table:

```text
веса подготовленного графа
→ один оптимальный путь движка
→ duration + distance
```

Перенос коэффициентов в Lua или `segment-speed-file` создаёт новую модель, а не
точную копию текущей: Lua использует OSM-теги, а адаптер эвристически разбирает
названия улиц; `traffic_light_count` текущий OSRM provider фактически не
заполняет.

Заказчик подтвердил приоритет скорости, поэтому Table является целевой
production-моделью. До реализации необходимо:

- сравнить Table с текущим адаптером в POC;
- зафиксировать допустимое изменение качества;
- после ADR обновить `docs/distance-definition.md`, честно заменив старое
  определение, а не называя Table ускорением прежней формулы.

Гибридный режим «Table для полной матрицы + alternatives для дуг готового
плана» не включается по умолчанию: повторный выбор дуг может изменить окна и
потребует итерационного перепланирования. Он остаётся отдельным будущим
экспериментом.

## 5. Три разных уровня времязависимости

Нельзя смешивать три решения:

1. **Одна статическая матрица на сутки.** Одна дуга имеет одно значение.
2. **Пояса в адаптере.** Уже реализованные `morning_peak`, `day`,
   `evening_peak`, `off_peak` пересчитывают время по эвристическим
   коэффициентам, но `plan-input 1.0` всё равно хранит один снимок.
3. **Нативная time-dependent матрица движка.** Путь/время зависят от
   `departure_at` и исторических скоростей на рёбрах.

OSM PBF не содержит исторических скоростей. Нативная времязависимость требует
отдельного источника данных; без него Valhalla и OSRM будут использовать
статические или вручную заданные веса.

Для первой версии решение пока открыто между:

- одной матрицей на момент начала смены;
- несколькими подготовленными матрицами по поясам или итерационным пересчётом.

Live-пробки сюда не относятся.

## 6. Упрощённая архитектура для одного Linux-сервера

Комментарии об избыточности первоначальной схемы приняты. В первой версии не
нужны Kubernetes, внешний message broker и distributed lock.

```text
пользовательский UI / существующий backend
       │ кнопка, upload, расписание, статус
       ▼
локальный Map Update API ── durable state
       │                       (SQLite или эквивалент на одном узле)
       ▼
один build worker под local lock
       │ download/upload → validate → build → test
       ▼
immutable versions: active / candidate / previous
       │
       ▼
локальный Routing Gateway
       ├─ mode=car  → active car runtime
       └─ mode=...  → active runtime согласованных mode
       │
расчётный сервис и UI route geometry
```

Обязательные компоненты:

1. Runtime выбранного движка.
2. Routing Gateway со стабильным внутренним контрактом.
3. Один build worker.
4. Локальное долговечное состояние версий и jobs.
5. Immutable-каталоги графов.
6. Системный timer/cron, вызывающий ту же команду, что и кнопка.

Не нужны в первой версии:

- Redis/RabbitMQ как очередь;
- Redis/etcd как distributed lock;
- отдельный scheduler-service;
- Kubernetes;
- абстракции для нескольких регионов.

Локальный lock реализуется файловой блокировкой или уникальным ограничением в
локальном хранилище. Конкретный способ запуска — Docker Compose или systemd —
остаётся открытым до ответа заказчика.

Build worker должен иметь CPU/RAM/I/O ограничения, чтобы `extract` и
`partition` не вытесняли активный runtime. Потребность в отдельном диске и
возможность одновременно держать active и candidate runtime определяются POC.

OSRM runtime может читать файлы напрямую либо использовать `osrm-datastore`.
План не смешивает эти способы: выбор делается после измерения RAM, времени
переключения и поведения при перезагрузке Linux. Если выбран datastore, каждому
mode нужен отдельный dataset namespace и проверенный startup/reload сценарий.

## 7. Модель состояния

### Coverage

```text
id
name
expected_polygon
representative_points
managed_source_url          nullable
enabled_modes
active_version_id
update_enabled
update_interval
schedule_timezone
retention_count
```

Используется `Coverage`, а не зона планирования. Bbox хранится как производная
для быстрой грубой проверки, но не заменяет polygon: административная граница
может иметь сложную форму, анклавы и отверстия.

### MapVersion

```text
id
coverage_id
source_kind                 managed_download | upload
source_osm_timestamp        timestamp внутри/источника PBF
downloaded_at
source_sha256
source_size
engine
engine_version
profile_hashes
weights_version
artifact_path
status                      acquiring | validating | building | testing |
                            ready | activating | active | superseded |
                            failed | cancelled
created_at
activated_at
failure_code
failure_details
```

Свежесть карты считается по `source_osm_timestamp`, а не по моменту скачивания.
Rollback проверяет совместимость пары `graph_version + engine_version`.

### UpdateJob

```text
id
coverage_id
trigger                     manual_download | manual_upload | schedule
requested_by
status
progress_stage
cancel_requested
started_at
finished_at
log_location
```

Для одного покрытия допускается только одно незавершённое update job. Повторное
нажатие возвращает существующий `job_id`.

## 8. Пользовательские сценарии

### 8.1. Обновить сейчас

```text
POST /coverage/{id}/updates
→ 202 Accepted
→ {job_id, status: "queued"}
```

HTTP-запрос только создаёт job. Активный граф продолжает обслуживать матрицы.

### 8.2. Загрузить PBF

```text
POST /coverage/{id}/uploads
Content-Type: multipart/form-data
file: *.osm.pbf
```

Файл загружается потоково в карантин. Пользовательское имя не используется как
путь. Upload доступен только разрешённой роли.

### 8.3. Скачать выбранный регион

Для одного покрытия не нужен универсальный каталог всех регионов. Нужен
allowlist поддерживаемых источников, построенный, например, из индекса
Geofabrik. Если источник отдаёт более широкую территорию, pipeline должен
явно выполнить `osmium extract` по согласованному `.poly`, а не считать bbox
эквивалентом региона.

Автообновление возможно только для покрытия с `managed_source_url`. Произвольно
загруженный PBF нельзя обновлять автоматически, пока источник не задан.

### 8.4. Расписание

UI предлагает согласованный набор интервалов и локальное время запуска;
внутренне systemd timer или cron вызывает тот же update endpoint/command.
Обновление не связано с `/plan`.

### 8.5. Отмена

```text
DELETE /coverage/{id}/updates/{job_id}
```

Отмена выставляет `cancel_requested`. Worker завершает дочерний build-процесс,
освобождает local lock и очищает неполный candidate в `finally`. Во время
атомарной стадии activation отмена не принимается; после неё используется
rollback.

### 8.6. Rollback

```text
POST /coverage/{id}/versions/{version_id}/activate
```

Активировать можно только успешно проверенный artifact, совместимый с текущим
engine runtime.

## 9. Pipeline обновления

### Шаг 1. Захват job

- получить local lock;
- проверить отсутствие другого незавершённого job;
- проверить квоты RAM/disk и зарезервировать место;
- создать immutable version id;
- не менять active pointer.

### Шаг 2. Получение PBF

Managed download:

- только allowlisted HTTPS-домен и URL;
- защита от SSRF и redirect на внутренние адреса;
- conditional request, если источник поддерживает;
- `no_change`, если PBF не изменился;
- SHA-256, размер, source timestamp и время скачивания.

Upload:

- потоковый лимит размера;
- карантин;
- проверка формата по содержимому, а не расширению;
- запрет архивов и исполняемых файлов.

### Шаг 3. Подготовка покрытия

- если исходник шире покрытия, выполнить воспроизводимый clip по versioned
  polygon;
- сохранить SHA исходного и итогового PBF;
- проверить representative points и ожидаемое покрытие;
- bbox использовать только как coarse sanity check.

### Шаг 4. Валидация PBF

- PBF читается OSM-инструментом без ошибок;
- присутствуют nodes и ways;
- source timestamp не старее active без явного rollback-сценария;
- размер и прогноз build укладываются в квоту;
- источник, лицензия и attribution сохранены.

OpenStreetMap распространяется по ODbL; при публичном использовании требуется
attribution: [OSMF Licence](https://osmfoundation.org/wiki/Licence) и
[Attribution Guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines).

### Шаг 5. Построение OSRM-кандидата

Для каждого фактически необходимого mode:

```text
osrm-extract --profile <profile.lua> --data_version <version> coverage.osm.pbf
osrm-partition coverage.osrm
osrm-customize coverage.osrm [--segment-speed-file ...]
osrm-routed --algorithm mld --trial coverage.osrm
```

Для CH POC вместо `partition + customize` используется `osrm-contract`.
Engine image, profile, polygon и weights входят в build manifest и хэш версии.

### Шаг 6. Проверка кандидата

- artifact загружается тем же engine version, что будет его обслуживать;
- `/nearest`, `/route` и `/table` проходят canary;
- измеряется snap distance контрольных и фактических точек;
- отслеживаются p50/p95 snap distance и доля точек дальше согласованного
  радиуса;
- `road_km / straight_km` используется как сигнал аномалии, но не как жёсткий
  универсальный порог;
- нет неожиданных `null` в обязательной матрице;
- новая и активная версии сравниваются по покрытию, времени и расстоянию;
- запускается solver regression на репрезентативном снимке;
- измеряются latency, RAM, disk и wall-clock всего matrix adapter;
- пороги определяются по baseline и утверждаются после POC.

### Шаг 7. Активация

- опубликовать immutable artifact;
- поднять candidate runtime или подготовить dataset выбранным способом;
- дождаться readiness;
- прекратить выдачу новых запросов старому runtime;
- дождаться drain уже идущих запросов в пределах таймаута;
- атомарно переключить active pointer;
- записать active version;
- сохранить предыдущую версию для rollback.

Один matrix request обслуживается одной версией. Retry не может незаметно уйти
на новый graph version. Для сборки `plan-input` расчётный сервис пинит
`graph_version` на весь снимок.

### Шаг 8. Cleanup

При success, failure, cancel, OOM или прерывании:

- освободить local lock;
- остановить полуготовый candidate runtime/dataset;
- удалить незавершённые build-файлы из candidate;
- сохранить диагностический лог в пределах retention;
- никогда не удалять active и последнюю rollback-версию;
- при старте после reboot пометить незавершённый job как interrupted и выполнить
  recovery/cleanup до следующего build.

При любой ошибке active pointer не меняется.

## 10. Контракт с расчётным сервисом

### 10.1. Engine-neutral запрос

```text
POST /internal/routing/v1/coverage/{coverage_id}/matrix
{
  "mode": "car",
  "coordinates": [[lon, lat], ...],
  "sources": "all",
  "destinations": "all",
  "departure_at": "2026-09-21T08:00:00+03:00",
  "graph_version": "optional-pinned-version"
}
```

Для replan допускаются разные `sources` и `destinations`, чтобы не считать
лишний полный квадрат, если контракту солвера достаточно подматрицы.

### 10.2. Успешный ответ

```text
graph_version
engine_version
profile_version
weights_version
departure_at
durations_seconds[][]
distances_meters[][]
```

Gateway работает в единицах движка. Только адаптер `plan-input` один раз
выполняет:

```text
minutes = 0 для совпадающей точки, иначе max(1, ceil(seconds / 60))
distance_km = meters / 1000
```

### 10.3. Полнота и ошибки

Частичная матрица не передаётся солверу. Если обязательная пара недоступна,
gateway возвращает ошибку уровня 422 с диагностикой, а сборка `plan-input`
завершается. `fallback_speed`, гаверсинус и подмена mode запрещены.

Политика использования старого кэша при полном отказе runtime остаётся открыта.
Технически кэш допустим только когда:

- множество новых координат является подмножеством закэшированного;
- координаты и mode совпадают;
- `graph_version`, `profile_version`, `weights_version` и контекст совпадают.

Для новой аварийной точки такой фолбэк неприменим.

### 10.4. Кэш

Ключ:

```text
coverage_id
+ graph_version
+ engine_version
+ profile_version
+ weights_version
+ mode
+ departure_at или model/time-band version
+ hash ordered coordinates/sources/destinations
```

### 10.5. Backpressure

Gateway ограничивает число одновременных matrix-запросов и имеет отдельную
короткую очередь расчётов, не связанную с update jobs. Политика приоритета
overnight/replan и числовые лимиты определяются после load test. Retry разрешён
только для целого идемпотентного запроса и учитывает pinned graph version.

## 11. Наблюдаемость и безопасность

Метрики:

- source age по `source_osm_timestamp`;
- active graph/engine/profile/weights versions;
- стадии и длительность update job;
- success/failed/cancelled/no_change;
- размер PBF и артефактов, свободный disk;
- CPU/RAM/I/O build worker и runtime;
- p50/p95 matrix latency и wall-clock адаптера;
- очередь matrix requests и update status;
- доля unreachable pairs;
- p50/p95 snap distance и доля превышений;
- rollback count.

Alerts:

- карта старше согласованного SLA;
- последовательные ошибки update;
- нет ready active runtime;
- выросли unreachable или snap anomalies;
- заканчивается disk;
- schedule не запускался;
- cleanup не освободил candidate/lock.

Безопасность:

- update/upload/activate/rollback только для разрешённой роли;
- allowlist источников и защита от SSRF;
- квоты размера, CPU, RAM, I/O и длительности;
- пользовательские значения не вставляются в shell-строку;
- worker изолирован от production secrets;
- runtime читает только immutable artifacts;
- audit log действий пользователя;
- active/candidate/rollback artifacts защищены от обычного cleanup.

## 12. План выполнения после отдельного разрешения

### Этап 0. Закрыть решения

- получить ответы раздела 13;
- определить production modes;
- определить coverage polygon и managed source;
- определить тестовые адреса и получить проверенные координаты;
- выбрать Docker Compose или systemd;
- определить baseline и процедуру ADR.

### Этап 1. POC движка и матрицы

На одном versioned PBF, одном наборе координат и одном `departure_at` сравнить:

1. текущий `contextual-multiroute-v1`;
2. OSRM MLD Table с базовым профилем;
3. OSRM CH Table с тем же профилем;
4. OSRM Table с экспериментальными весами;
5. Valhalla Matrix как резервный runtime.

Для каждого варианта измерить:

- build time, cold/warm start, RAM, disk;
- p50/p95 engine latency;
- wall-clock всей сборки матрицы;
- unreachable и snap statistics;
- распределение отклонений distance/time относительно baseline;
- долю дуг с изменением выше порога, который утверждается после первого
  прогона;
- solver lex-score, число назначенных заявок, число инженеров,
  `travel_minutes` и runtime солвера;
- overnight и replan отдельно;
- каждый реально присутствующий mode отдельно.

POC также проверяет:

- file-based runtime против `osrm-datastore`;
- возможность держать active/candidate на одном сервере;
- влияние build worker под ресурсными лимитами на active p95;
- reload, reboot и rollback;
- полную пересборку Valhalla. Partial topology rebuild не считается доступным,
  пока POC и официальная поддержка не докажут обратное.

Результат: ADR о движке, алгоритме, runtime loading и новой семантике расстояния.

### Этап 2. Быстрый MatrixProvider

- engine-neutral contract;
- OSRM Table adapter;
- строгая полнота без fallback;
- единое округление в `plan-input` adapter;
- pinned graph version;
- cache и bounded concurrency;
- contract/load tests;
- обновление `docs/distance-definition.md` по принятому ADR.

### Этап 3. Минимальный update pipeline

- pinned engine/build images или binaries;
- allowlisted download и upload quarantine;
- clip/validation;
- active/candidate/previous artifacts;
- local lock;
- resource limits;
- canary, solver regression, activation, drain, rollback;
- cancel, cleanup и reboot recovery.

### Этап 4. Управление и расписание

- durable local state;
- update/upload/status/cancel/activate endpoints;
- system timer/cron;
- RBAC и audit;
- метрики и alerts.

### Этап 5. UI

- active version и source age;
- «Обновить сейчас»;
- upload PBF;
- выбор разрешённого источника;
- расписание;
- progress/history/error;
- cancel и rollback.

### Этап 6. Приёмка

- update не вызывается из `/plan`;
- planning доступен во время resource-limited build;
- invalid graph не активируется;
- одна матрица не смешивает версии;
- restart/reboot восстанавливает active runtime;
- OOM, kill, network failure и invalid PBF освобождают lock/candidate;
- повторный upload идемпотентен по SHA-256;
- rollback восстанавливает совместимую пару graph/engine;
- нагрузка соответствует согласованному SLA;
- ODbL attribution присутствует.

## 13. Открытые вопросы заказчику

Подтверждённые ответы находятся в разделе 1. До реализации остаётся решить:

1. Допустим ли Docker Compose или нужен systemd/другой способ запуска?
2. Какой именно географический охват означает «один регион», и каков
   максимальный PBF?
3. Какой источник использовать для управляемого скачивания и нужен ли clip по
   собственной границе?
4. Какой SLA принять после POC: p95 и число параллельных расчётов?
5. Для первой версии достаточно одной матрицы на начало смены или нужны
   несколько временных поясов/итерационный пересчёт? Это исторические
   коэффициенты, не live-пробки.
6. При полном отказе runtime запрещать новый план или разрешать строго
   совместимую последнюю закэшированную матрицу?
7. Кто может upload/activate/rollback?
8. Сколько предыдущих версий хранить и допустимо ли короткое окно drain/reload?

Командные решения, фиксируемые ADR после POC:

- OSRM MLD или CH;
- file runtime или datastore;
- новая семантика Table weights;
- production modes;
- единое правило контекста и округления;
- ресурсные лимиты;
- пороги качества и автоактивации.

## 14. Решения по замечаниям технического ревью

### Принято и встроено

1. Развести одну статическую матрицу, пояса адаптера и нативную
   time-dependent модель.
2. Уточнить, что одна карта региона может обслуживать несколько зон плана, а
   геокодирование находится вне routing service.
3. Перевести UI route geometry с публичного demo на внутренний gateway для
   production.
4. Сравнить OSRM MLD и CH; корректно трактовать лимит OSRM как число координат.
5. Учитывать отсутствие historical traffic в OSM.
6. Признать Table сменой определения расстояния и обновить соответствующий
   документ только после ADR.
7. Упростить одноузловую архитектуру: без брокера, distributed lock,
   Kubernetes и отдельного scheduler-service.
8. Ограничить CPU/RAM/I/O build worker.
9. Использовать coverage polygon, representative points и отдельный source
   timestamp вместо одной bbox/даты скачивания.
10. Добавить cancel, гарантированный cleanup и восстановление после reboot.
11. Проверять snap distance, совместимость graph/engine и solver regression.
12. Пинить graph version на весь снимок и drain старый runtime.
13. Сохранить политику «полная матрица или ошибка», единое округление и запрет
    fallback.
14. Ограничить concurrency matrix-запросов и отдельно измерять replan.
15. Сравнивать влияние движка на план, а не только HTTP latency.

### Принято с оговорками

1. `road_km / straight_km` полезно как сигнал, но диапазон 1.0–2.5 нельзя
   использовать как универсальный hard-fail.
2. `osrm-datastore` может быть полезен, но не выбирается заранее: сравнивается
   с file runtime, отдельные dataset namespaces обязательны.
3. ORS остаётся бесплатным self-hosted вариантом, но из-за ресурсов и
   избыточности отложен, а не объявлен технически непригодным.
4. Valhalla остаётся POC-кандидатом, но без источника исторических скоростей её
   time-dependent возможности не дают требуемые данные автоматически.
5. Физически разделять ресерч и план сейчас не нужно: один итоговый документ
   содержит статус и журнал решений. После ADR устаревшее сравнение можно
   вынести в отдельный research archive.

### Отклонено

1. **«OSRM Route можно сократить до ~280 запросов, передав до 25 координат».**
   Route API строит один маршрут через последовательные waypoints; он не
   вычисляет независимые пары с отдельными альтернативами. Семантика текущего
   адаптера так не сохраняется.
2. **«Time-dependent Valhalla работает только 1×N или N×1».** Официальный
   Matrix API поддерживает общую и квадратную матрицу. Ограничения зависят от
   соотношения sources/targets, типа arrival/departure и выбранного алгоритма;
   тезис в абсолютной форме неверен. Реальная производительность проверяется
   POC.
3. **«Valhalla гарантированно пересобирает только изменившиеся routing tiles».**
   Тайловая структура сама по себе не доказывает поддержанный incremental
   topology rebuild. До официального подтверждения и POC планирует полную
   пересборку.
4. **«OSRM Table на 84 точках занимает десятки секунд на одном ядре».** Без
   измерения на нашем PBF это неподтверждённая оценка; backpressure нужен, но
   числовое утверждение в план не включается.

## 15. Состояние после ревью

Документ готов как план исследования и последующей реализации. Следующее
допустимое действие — получить ответы раздела 13. Реализация, развёртывание
движка и изменение production-кода до отдельной команды не выполняются.
