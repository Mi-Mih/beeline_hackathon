# Развёртывание OSRM для Москвы

Компоненты рассчитаны на один Linux-сервер с Docker Compose и Python 3.12.
`osmium-tool` запускается в локально собранном контейнере. OSRM бесплатен по BSD-2-Clause; источник данных Geofabrik/OSM —
по ODbL.

## Подготовка

1. Установить Docker с Compose plugin и Python 3.12.
2. Разместить проект в `/opt/beeline`, создать virtualenv и установить проект
   для первого запуска builder на хосте.
3. Создать `/srv/beeline-routing/{config,sources,quarantine,versions,state,logs}`
   и выдать сервисному пользователю права на запись.
4. Скопировать `routing.env.example` в `/etc/beeline/routing.env` и проверить
   пути и лимиты. Для пешеходного профиля оставить `ROUTING_WITH_FOOT=1` и
   `OSRM_FOOT_URL=http://osrm-foot:5000`.
5. При наличии утверждённого полигона с дорожным буфером положить его в
   `/srv/beeline-routing/config/moscow.geojson`. Без файла builder получает
   актуальную административную границу relation 102269 из исходного PBF; это
   не добавляет буфер за границей Москвы.
6. Собрать закреплённый служебный образ Osmium:

```bash
docker build -f deploy/routing/Dockerfile.osmium -t beeline-osmium:local .
```

Не использовать тег Docker-образа `latest`. Перед production-развёртыванием
заменить tag в env на проверенный immutable digest.

## Первый запуск

Инициализировать SQLite можно запуском backend или командой:

```bash
ROUTING_STATE_DB=/srv/beeline-routing/state/routing.sqlite3 \
python -c 'from backend.map_manager import manager_from_env; manager_from_env()'
```

Затем запустить обновление из UI «Карта региона» или вручную:

```bash
set -a
. /etc/beeline/routing.env
set +a
python scripts/build_moscow_osrm.py --job-id initial
```

Builder скачивает Central Federal District PBF, проверяет его, извлекает
Москву, строит MLD-граф, выполняет `osrm-routed --trial`, создаёт immutable
manifest и только затем переключает `active`. Для обычного запуска всей
связки после этого используется одна команда:

```bash
docker compose --env-file /etc/beeline/routing.env \
  -f deploy/routing/compose.yml --profile foot up -d --build --wait
```

Compose поднимает OSRM, затем готовый Routing Gateway и backend. Backend
доступен на `http://127.0.0.1:4173`; gateway — на `http://127.0.0.1:4180`.
Панель обновления карты использует Docker socket, подключённый к backend:
доступ к `/api/map/*` следует ограничить доверенными пользователями.

Проверка gateway:

```bash
curl http://127.0.0.1:4180/health
curl -X POST http://127.0.0.1:4180/internal/routing/v1/matrix \
  -H 'Content-Type: application/json' \
  -d '{"mode":"car","coordinates":[{"id":"a","longitude":37.6176,"latitude":55.7558},{"id":"b","longitude":37.6428,"latitude":55.7642}]}'
```

## Backend и расписание

Backend запускается с тем же EnvironmentFile. При
`ROUTING_MATRIX_MODE=gateway` переданная пользователем `travel` заменяется
полной направленной матрицей локального gateway. При любой ошибке OSRM план не
строится; геометрического или скоростного fallback нет.

Установить `beeline-map-scheduler.service` и `.timer` в `/etc/systemd/system/`,
затем:

```bash
systemctl daemon-reload
systemctl enable --now beeline-map-scheduler.timer
```

Timer только проверяет сохранённое в UI расписание. Сборку запускает тот же
Map Manager pipeline, что и ручная кнопка.

## Перед production

- утвердить полигон и буфер по реальным точкам заявок;
- замерить RAM/disk/build time и выставить лимиты;
- провести matrix/solver regression и нагрузочный тест;
- проверить восстановление после reboot и rollback;
- подключить существующую аутентификацию к изменяющим `/api/map/*` endpoint;
- сохранить `© OpenStreetMap contributors` в UI.
