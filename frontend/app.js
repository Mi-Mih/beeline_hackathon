const ROUTE_COLORS = ["#536b45", "#835f8a", "#a8642f", "#486b70"];
const SKILL_LABELS = { connection: "Подключение", emergency: "Аварийные работы", satellite: "Спутниковое ТВ" };
const WORK_TYPE_LABELS = { connection: "Подключение", emergency: "Авария", equipment_order: "Дозаказ", local_repair: "Локальный ремонт" };

const state = {
  input: null,
  originalInput: null,
  plan: null,
  baseline: null,
  filter: "all",
  query: "",
  requestPage: 0,
  technicianQuery: "",
  technicianFilter: "all",
  technicianPage: 0,
  selectedRequestId: null,
  selectedTechnicianId: null,
  routeThroughRequestId: null,
  changes: [],
  busy: false,
  mapManagement: null,
  travelCalibrationStatus: null,
  useTravelHistory: false,
  demoSource: null,
  timelineZoom: 1,
  pendingRequests: [],
  planningConfig: null,
  lastEventAt: null,
  comparisonSummary: "",
  seenRequestIds: [],
};

let mapInstance;
let initialMapReady;
let mapLayers;
let mapRenderToken = 0;
let routeLayerSerial = 0;
let mapStatusTimer;
let mapRouteController;
const REQUEST_PAGE_SIZE = 50;
const TECHNICIAN_PAGE_SIZE = 20;
const routeGeometryCache = new Map();
const routeGeometryFailures = new Map();

const byId = (id) => document.getElementById(id);
const deepClone = (value) => JSON.parse(JSON.stringify(value));
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
const timeLabel = (value) => new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
const dateLabel = (value) => new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date(value));
const skillLabel = (skill) => SKILL_LABELS[skill] || skill;
const workTypeLabel = (request) => WORK_TYPE_LABELS[request.work_type] || skillLabel(request.required_skills[0]);

function indexPlan(plan = state.plan) {
  const assignments = new Map();
  for (const [routeIndex, route] of (plan?.routes || []).entries()) {
    for (const stop of route.stops) assignments.set(stop.request_id, { route, stop, routeIndex });
  }
  const unassigned = new Map((plan?.unassigned || []).map((item) => [item.request_id, item]));
  return { assignments, unassigned };
}

async function requestPlan(input, { keepBaseline = false, reason = "overnight", demoSource, fullReplan = false } = {}) {
  if (state.busy) throw new Error("Дождитесь завершения текущего расчёта");
  let succeeded = false;
  setBusy(true, reason === "replan" ? "Перепланируем маршруты…" : "Считаем маршруты на смену…");
  try {
    const body = { ...input, planning_reason: reason, use_travel_history: state.useTravelHistory };
    // Explicit cancellation/unavailability can redistribute all future work;
    // frozen current jobs remain protected by each technician's available_from.
    if (fullReplan) delete body.replan_context;
    const response = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Не удалось построить план");
    state.input = input;
    if (demoSource !== undefined) state.demoSource = demoSource;
    state.plan = payload;
    state.routeThroughRequestId = null;
    state.seenRequestIds = [...new Set([...(keepBaseline ? state.seenRequestIds : []), ...input.requests.map((request) => request.id)])];
    if (!keepBaseline) {
      state.pendingRequests = [];
      state.lastEventAt = null;
      state.changes = [];
      state.comparisonSummary = "";
      const firstShift = input.technicians.map((tech) => tech.available_from).sort()[0];
      if (firstShift) byId("event-at").value = moscowLocalValue(firstShift);
    }
    if (!keepBaseline || !state.baseline) state.baseline = deepClone(payload);
    if (state.selectedRequestId && !input.requests.some((item) => item.id === state.selectedRequestId)) state.selectedRequestId = null;
    if (state.selectedTechnicianId && !input.technicians.some((item) => item.id === state.selectedTechnicianId)) state.selectedTechnicianId = null;
    if (!state.selectedRequestId && !state.selectedTechnicianId && input.requests.length) state.selectedRequestId = input.requests[0].id;
    render();
    succeeded = true;
    return payload;
  } catch (error) {
    showToast(error.message, true);
    throw error;
  } finally {
    setBusy(false, succeeded ? "План рассчитан" : "Ошибка расчёта");
  }
}

async function loadDemo() {
  const response = await fetch("/api/demo");
  if (!response.ok) throw new Error("Демо-данные недоступны");
  const input = await response.json();
  await requestPlan(input, { demoSource: "sample" });
  state.originalInput = deepClone(input);
}

function setBusy(isBusy, label) {
  state.busy = isBusy;
  byId("plan-status").textContent = label;
  byId("plan-status").parentElement.classList.toggle("busy", isBusy);
  document.querySelectorAll(".header-actions button:not(#map-button), #reset-button, #apply-scenario, #apply-assignment, #assignment-technician, [data-travel-time-mode]").forEach((button) => {
    button.disabled = isBusy || (!state.plan && button.id !== "load-button") || (state.pendingRequests.length > 0 && ["load-button", "stress-demo-button", "reset-button", "apply-scenario", "apply-assignment", "assignment-technician"].includes(button.id));
  });
  renderPendingStatus();
}

async function apiJson(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Ошибка сервера");
  return payload;
}

function mapDate(value) {
  return value ? new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";
}

function renderMapManagement(status, jobs = []) {
  const previousVersion = state.mapManagement?.active_version;
  state.mapManagement = status;
  if (previousVersion && previousVersion !== status.active_version) routeGeometryCache.clear();
  byId("map-coverage").textContent = status.coverage || "Москва";
  byId("map-version").textContent = status.active_version || "—";
  byId("map-activated").textContent = mapDate(status.activated_at);
  byId("map-modes").textContent = (status.modes || []).join(", ") || "—";
  byId("map-freshness").textContent = status.source_timestamp ? `Данные OSM от ${mapDate(status.source_timestamp)}` : "Карта ещё не инициализирована";
  const labels = { ready: "Готова", updating: "Обновляется", uninitialized: "Не настроена" };
  byId("map-engine-status").textContent = labels[status.state] || status.state;
  byId("map-engine-status").className = `status-pill ${status.state}`;
  byId("map-button-dot").className = `map-status-dot ${status.state}`;
  const schedule = status.schedule || {};
  byId("map-schedule-enabled").checked = Boolean(schedule.enabled);
  byId("map-schedule-days").value = schedule.interval_days || 7;
  byId("map-schedule-time").value = schedule.local_time || "02:00";
  const job = status.current_job;
  byId("map-update-button").disabled = Boolean(job);
  byId("map-file-input").disabled = Boolean(job);
  byId("map-upload-button").disabled = Boolean(job);
  const jobBox = byId("map-job");
  jobBox.hidden = !job;
  if (job) jobBox.innerHTML = `<strong>Обновление выполняется</strong><span>Стадия: ${escapeHtml(job.stage)} · job ${escapeHtml(job.id.slice(0, 8))}</span><button id="map-cancel-job" class="text-button" type="button">Отменить</button>`;
  byId("map-history").innerHTML = jobs.length ? jobs.map((item) => `<div class="map-history-row"><span><strong>${escapeHtml(item.trigger)}</strong><small>${mapDate(item.created_at)}</small></span><span class="job-result ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>${item.error ? `<p>${escapeHtml(item.error)}</p>` : ""}</div>`).join("") : "<p>Обновлений ещё не было.</p>";
  byId("map-cancel-job")?.addEventListener("click", () => cancelMapJob(job.id));
}

async function loadMapManagement() {
  clearTimeout(mapStatusTimer);
  try {
    const [status, history] = await Promise.all([apiJson("/api/map/status"), apiJson("/api/map/jobs")]);
    renderMapManagement(status, history.jobs);
    if (status.current_job && byId("map-dialog").open) mapStatusTimer = setTimeout(loadMapManagement, 2000);
  } catch (error) { showToast(error.message, true); }
}

async function startMapUpdate() {
  try { await apiJson("/api/map/updates", { method: "POST" }); await loadMapManagement(); }
  catch (error) { showToast(error.message, true); }
}

async function uploadMap(file) {
  try {
    await apiJson("/api/map/uploads", { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name }, body: file });
    await loadMapManagement();
  } catch (error) { showToast(error.message, true); }
}

function datetimeLocalValue(value) {
  const date = value ? new Date(value) : new Date();
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function renderCalibrationHistory() {
  const technicianId = byId("calibration-technician").value;
  const item = state.travelCalibrationStatus?.technicians?.find((row) => row.technician_id === technicianId);
  const box = byId("calibration-history");
  if (!item?.observation_count) {
    box.innerHTML = calibrationNote("Первое наблюдение", "После сохранения появится экспериментальное сравнение с OSRM.");
    return;
  }
  const factor = item.experimental_factor ? ` · коэффициент ×${item.experimental_factor.toFixed(2)}` : "";
  const readiness = item.calibration_ready ? "Данных достаточно для отдельной проверки модели." : `Для устойчивой оценки желательно минимум ${state.travelCalibrationStatus.calibration_min_observations} сопоставимых поездок.`;
  box.innerHTML = calibrationNote(`Наблюдений: ${item.observation_count}${factor}`, readiness);
}

function calibrationNote(title, text) {
  return `<i class="info-mark" aria-hidden="true">i</i><p><strong>${escapeHtml(title)}</strong> <span>${escapeHtml(text)}</span></p>`;
}

function calibrationPointOptions() {
  return state.input.locations.map((location) => `<option value="${escapeHtml(location.id)}">${escapeHtml(location.address || location.id)}</option>`).join("");
}

async function openTravelCalibration() {
  const dialog = byId("travel-calibration-dialog");
  const technicianSelect = byId("calibration-technician");
  technicianSelect.innerHTML = state.input.technicians.map((technician) => `<option value="${escapeHtml(technician.id)}">${escapeHtml(technician.name)}</option>`).join("");
  const pointOptions = calibrationPointOptions();
  byId("calibration-origin").innerHTML = pointOptions;
  byId("calibration-destination").innerHTML = pointOptions;
  const firstTechnician = state.input.technicians[0];
  if (firstTechnician) byId("calibration-origin").value = firstTechnician.start_location_id;
  const firstDestination = state.input.locations.find((location) => location.id !== firstTechnician?.start_location_id);
  if (firstDestination) byId("calibration-destination").value = firstDestination.id;
  const firstStart = state.input.requests[0]?.window?.start || new Date().toISOString();
  const departed = datetimeLocalValue(firstStart);
  byId("calibration-departed").value = departed;
  byId("calibration-arrived").value = datetimeLocalValue(new Date(new Date(departed).getTime() + 30 * 60000));
  byId("calibration-result").hidden = true;
  dialog.showModal();
  try {
    state.travelCalibrationStatus = await apiJson("/api/travel-observations/status");
    renderCalibrationHistory();
  } catch (error) {
    showToast(error.message, true);
  }
}

function updateCalibrationOrigin() {
  const technician = state.input.technicians.find((item) => item.id === byId("calibration-technician").value);
  if (technician) byId("calibration-origin").value = technician.start_location_id;
  renderCalibrationHistory();
}

function planTravelForecast(originId, destinationId, mode) {
  const travel = state.input?.travel;
  const legs = travel?.legs || [];
  const directed = (from, to) => legs.find((leg) => leg.from === from && leg.to === to && (leg.mode || null) === mode)
    || legs.find((leg) => leg.from === from && leg.to === to && !leg.mode);
  const leg = directed(originId, destinationId) || (travel?.symmetric ? directed(destinationId, originId) : null);
  if (!leg || !Number.isFinite(Number(leg.minutes))) return null;
  return { duration_seconds: Number(leg.minutes) * 60, distance_km: Number(leg.distance_km) || 0, source: "plan" };
}

async function saveTravelCalibration(event) {
  event.preventDefault();
  const technician = state.input.technicians.find((item) => item.id === byId("calibration-technician").value);
  const origin = state.input.locations.find((item) => item.id === byId("calibration-origin").value);
  const destination = state.input.locations.find((item) => item.id === byId("calibration-destination").value);
  if (!technician || !origin || !destination) return;
  if (origin.id === destination.id) {
    showToast("Точки А и Б должны отличаться", true);
    return;
  }
  const departed = new Date(byId("calibration-departed").value);
  const arrived = new Date(byId("calibration-arrived").value);
  const actualSeconds = Math.round((arrived - departed) / 1000);
  if (!Number.isFinite(actualSeconds) || actualSeconds <= 0) {
    showToast("Прибытие должно быть позже убытия", true);
    return;
  }
  if (![origin.latitude, origin.longitude, destination.latitude, destination.longitude].every(Number.isFinite)) {
    showToast("Для выбранных точек не заданы координаты", true);
    return;
  }
  const saveButton = byId("travel-calibration-save");
  saveButton.disabled = true;
  saveButton.textContent = "Сравниваем…";
  try {
    const points = `${origin.longitude},${origin.latitude};${destination.longitude},${destination.latitude}`;
    let route;
    try {
      route = await apiJson(`/api/route?mode=${encodeURIComponent(technician.vehicle)}&points=${encodeURIComponent(points)}`);
      if (!Number.isFinite(Number(route.duration_seconds)) && Number.isFinite(Number(route.duration_minutes))) {
        route.duration_seconds = Number(route.duration_minutes) * 60;
      }
    } catch (error) {
      route = planTravelForecast(origin.id, destination.id, technician.vehicle);
      if (!route) throw error;
    }
    const observation = {
      technician_id: technician.id,
      plan_id: state.plan?.plan_id || state.input.plan_id,
      origin_location_id: origin.id,
      destination_location_id: destination.id,
      departed_at: departed.toISOString(),
      arrived_at: arrived.toISOString(),
      vehicle_mode: technician.vehicle,
      osrm_seconds: route.duration_seconds,
      osrm_distance_meters: route.distance_km * 1000,
      origin_latitude: origin.latitude,
      origin_longitude: origin.longitude,
      destination_latitude: destination.latitude,
      destination_longitude: destination.longitude,
      ...(route.graph_version ? { graph_version: route.graph_version } : {}),
    };
    const imported = await apiJson("/api/travel-observations/import?filename=calibration-form.json", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify([observation]),
    });
    state.travelCalibrationStatus = imported.status;
    renderCalibrationHistory();
    const stats = imported.status.technicians.find((item) => item.technician_id === technician.id);
    const differenceMinutes = (actualSeconds - route.duration_seconds) / 60;
    const sign = differenceMinutes >= 0 ? "+" : "−";
    const result = byId("calibration-result");
    result.hidden = false;
    const factorText = stats.experimental_factor == null
      ? "Наблюдение сохранено, но отклонение слишком велико для предварительного коэффициента."
      : `Персональный коэффициент по ${stats.preview_observation_count} наблюд.: <strong>×${stats.experimental_factor.toFixed(2)}</strong>.`;
    const forecastLabel = route.source === "plan" ? "Прогноз" : "OSRM";
    const sourceNote = route.source === "plan" ? " Прогноз взят из матрицы текущего плана: сервис маршрутов сейчас недоступен." : "";
    result.innerHTML = `<h3>Результат эксперимента</h3><div class="calibration-comparison"><span><small>${forecastLabel}</small><strong>${(route.duration_seconds / 60).toFixed(1)} мин</strong></span><i>→</i><span><small>Факт</small><strong>${(actualSeconds / 60).toFixed(1)} мин</strong></span><span><small>Разница</small><strong>${sign}${Math.abs(differenceMinutes).toFixed(1)} мин</strong></span></div><p>${factorText}${sourceNote} Включите «С калибровкой» в этом окне, чтобы проверить влияние коэффициента.</p>`;
    showToast(imported.inserted ? "Наблюдение сохранено" : "Такое наблюдение уже было сохранено");
  } catch (error) {
    const offline = /routing provider is unavailable|Connection refused|Failed to fetch/i.test(error.message);
    showToast(offline ? "Сервис маршрутов недоступен, и в плане нет времени между этими точками." : error.message, true);
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = "Сравнить и сохранить";
  }
}

async function cancelMapJob(jobId) {
  try { await apiJson(`/api/map/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }); await loadMapManagement(); }
  catch (error) { showToast(error.message, true); }
}

async function saveMapSchedule() {
  const payload = { enabled: byId("map-schedule-enabled").checked, interval_days: Number(byId("map-schedule-days").value), local_time: byId("map-schedule-time").value };
  try {
    await apiJson("/api/map/schedule", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    showToast("Расписание сохранено");
    await loadMapManagement();
  } catch (error) { showToast(error.message, true); }
}

function render() {
  renderPendingStatus();
  renderMetrics();
  renderTravelTimeMode();
  renderRequests();
  renderMap();
  renderTimeline();
  renderTechnicians();
  renderDecision();
  renderChanges();
  const firstWindow = state.input?.requests?.[0]?.window?.start;
  if (firstWindow) byId("plan-date").textContent = dateLabel(firstWindow);
}

function renderTravelTimeMode() {
  document.querySelectorAll("[data-travel-time-mode]").forEach((button) => {
    const active = state.useTravelHistory
      ? button.dataset.travelTimeMode === "history"
      : button.dataset.travelTimeMode === "osrm";
    button.setAttribute("aria-pressed", String(active));
  });
  byId("travel-time-mode-note").textContent = state.useTravelHistory
    ? "Время из OSRM калибруется по наблюдениям инженера"
    : "Время из OSRM без калибровки";
}

function setTravelTimeMode(useHistory) {
  if (state.useTravelHistory === useHistory) return;
  state.useTravelHistory = useHistory;
  renderTravelTimeMode();
}

function delta(current, baseline, inverse = false, unit = "", digits = null) {
  let difference = current - baseline;
  if (digits != null) difference = Number(difference.toFixed(digits));
  if (!difference) return { text: "", className: "" };
  const shown = digits == null ? String(difference) : difference.toFixed(digits);
  const isGood = inverse ? difference < 0 : difference > 0;
  return { text: `${difference > 0 ? "+" : ""}${shown}${unit}`, className: isGood ? "good" : "bad" };
}

function setDelta(id, value) {
  const element = byId(id);
  element.textContent = value.text;
  element.className = value.className;
}

function renderMetrics() {
  const metrics = state.plan?.metrics;
  if (!metrics) return;
  byId("dataset-note").hidden = !state.demoSource;
  byId("dataset-note").textContent = state.demoSource ? "Демо · тестовые данные и время поездок" : "";
  const base = state.baseline?.metrics || metrics;
  byId("metric-assigned").textContent = `${metrics.assigned_count} / ${metrics.request_count}`;
  byId("metric-unassigned").textContent = metrics.unassigned_count;
  byId("metric-techs").textContent = metrics.used_technicians;
  byId("metric-distance").textContent = `${metrics.distance_km.toFixed(1)} км`;
  byId("algorithm-label").textContent = `${state.plan.algorithm.id} v${state.plan.algorithm.version}`;
  byId("runtime-label").textContent = `${state.plan.diagnostics.runtime_ms.toFixed(1)} мс`;
  setDelta("delta-assigned", delta(metrics.assigned_count, base.assigned_count));
  setDelta("delta-unassigned", delta(metrics.unassigned_count, base.unassigned_count, true));
  setDelta("delta-techs", delta(metrics.used_technicians, base.used_technicians, true));
  setDelta("delta-distance", delta(metrics.distance_km, base.distance_km, true, " км", 1));
}

function requestMeta(request) {
  const { assignments, unassigned } = indexPlan();
  return { assignment: assignments.get(request.id), unassigned: unassigned.get(request.id) };
}

function renderRequests() {
  const input = workingInput();
  const list = byId("request-list");
  const query = state.query.trim().toLowerCase();
  const locations = new Map(input.locations.map((item) => [item.id, item]));
  const indexed = indexPlan();
  const requests = input.requests.filter((request) => {
    const meta = { unassigned: indexed.unassigned.has(request.id) || isPending(request.id) };
    const matchesFilter = state.filter === "all" || (state.filter === "priority" && request.priority !== "normal") || (state.filter === "unassigned" && meta.unassigned);
    const haystack = `${request.id} ${locations.get(request.location_id)?.address || request.location_id}`.toLowerCase();
    return matchesFilter && (!query || haystack.includes(query));
  });
  byId("request-count").textContent = `${requests.length} из ${input.requests.length}`;
  const visible = paginate(requests, "requestPage", REQUEST_PAGE_SIZE, "requests");
  if (!requests.length) {
    list.innerHTML = '<div class="empty-state">По этому фильтру заявок нет.</div>';
    return;
  }
  list.innerHTML = visible.map((request) => {
    const assignment = indexed.assignments.get(request.id);
    const unassigned = indexed.unassigned.has(request.id);
    const location = locations.get(request.location_id);
    const assignee = isPending(request.id) ? "Ожидает расчёта" : assignment ? assignment.route.technician_name : "Не назначена";
    const start = request.window.start;
    return `<button class="request-item ${state.selectedRequestId === request.id ? "selected" : ""}" data-request-id="${escapeHtml(request.id)}" type="button">
      <i class="request-priority ${request.priority}"></i>
      <span>
        <span class="request-top"><strong>${escapeHtml(request.id)}</strong><time>${timeLabel(start)}–${timeLabel(request.window.end)}</time></span>
        <span class="request-address">${escapeHtml(location?.address || request.location_id)}</span>
        <span class="request-bottom"><span>${escapeHtml(workTypeLabel(request))} · ${request.duration_minutes} мин</span><span class="assignment ${unassigned ? "unassigned" : ""}">${escapeHtml(assignee)}</span></span>
      </span>
    </button>`;
  }).join("");
  bindRequestSelection(list);
}

function paginate(items, stateKey, size, prefix) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  state[stateKey] = Math.min(Math.max(0, state[stateKey]), pages - 1);
  byId(`${prefix}-page`).textContent = `${state[stateKey] + 1} / ${pages}`;
  byId(`${prefix}-prev`).disabled = state[stateKey] === 0;
  byId(`${prefix}-next`).disabled = state[stateKey] >= pages - 1;
  return items.slice(state[stateKey] * size, (state[stateKey] + 1) * size);
}

function locationPoint(location) {
  return Number.isFinite(location?.latitude) && Number.isFinite(location?.longitude)
    ? [location.latitude, location.longitude]
    : null;
}

function ensureMap() {
  if (mapInstance) return true;
  if (!window.maplibregl) {
    byId("route-map").innerHTML = '<div class="empty-state">Карта не загрузилась. Проверьте доступ к сети.</div>';
    byId("map-caption").textContent = "Картографический слой недоступен";
    return false;
  }
  mapInstance = new maplibregl.Map({
    container: "route-map",
    center: [37.6176, 55.7558],
    zoom: 10,
    maxZoom: 19,
    maplibreLogo: false,
    attributionControl: false,
    style: {
      version: 8,
      sources: {
        osm: {
          type: "raster",
          tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
          tileSize: 256,
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        },
      },
      layers: [{ id: "osm", type: "raster", source: "osm" }],
    },
  });
  mapInstance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  mapInstance.addControl(new maplibregl.AttributionControl({
    compact: false,
    customAttribution: '<a href="https://project-osrm.org/">маршруты OSRM</a>',
  }), "bottom-right");
  mapLayers = { markers: [], routes: [] };
  return true;
}

function mapReady() {
  // loaded() can become false again while sources/tiles update. The initial
  // load event only happens once, so all subsequent renders share its promise.
  if (!initialMapReady) initialMapReady = mapInstance.loaded() ? Promise.resolve() : new Promise((resolve) => mapInstance.once("load", resolve));
  return initialMapReady;
}

function clearMapLayers() {
  for (const marker of mapLayers.markers) marker.remove();
  for (const route of mapLayers.routes) route.remove();
  mapLayers.markers = [];
  mapLayers.routes = [];
}

function addMapMarker(point, className, html, popupHtml, onClick) {
  const element = document.createElement("div");
  element.className = className;
  element.innerHTML = html;
  if (onClick) element.addEventListener("click", onClick);
  const marker = new maplibregl.Marker({ element, anchor: "center" })
    .setLngLat([point[1], point[0]])
    .setPopup(new maplibregl.Popup({ offset: 16 }).setHTML(popupHtml))
    .addTo(mapInstance);
  mapLayers.markers.push(marker);
  return marker;
}

function createRouteLine(points, style, tooltipContent, onClick) {
  const serial = routeLayerSerial++;
  const sourceId = `route-source-${serial}`;
  const layerId = `route-layer-${serial}`;
  const data = (rows) => ({
    type: "Feature",
    properties: {},
    geometry: { type: "LineString", coordinates: rows.map(([latitude, longitude]) => [longitude, latitude]) },
  });
  mapInstance.addSource(sourceId, { type: "geojson", data: data(points) });
  mapInstance.addLayer({
    id: layerId,
    type: "line",
    source: sourceId,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": style.color,
      "line-width": style.weight,
      "line-opacity": style.opacity,
      "line-dasharray": style.dashArray ? [2, 3] : [1, 0],
    },
  });
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
  let tooltip = tooltipContent;
  const showTooltip = (event) => popup.setLngLat(event.lngLat).setHTML(tooltip).addTo(mapInstance);
  const hideTooltip = () => popup.remove();
  mapInstance.on("mouseenter", layerId, showTooltip);
  mapInstance.on("mouseleave", layerId, hideTooltip);
  mapInstance.on("click", layerId, onClick);
  const line = {
    points,
    style: { ...style },
    setLatLngs(rows) {
      this.points = rows;
      mapInstance.getSource(sourceId)?.setData(data(rows));
      return this;
    },
    setStyle(next) {
      Object.assign(this.style, next);
      if (next.opacity !== undefined) mapInstance.setPaintProperty(layerId, "line-opacity", next.opacity);
      if (next.weight !== undefined) mapInstance.setPaintProperty(layerId, "line-width", next.weight);
      if (next.dashArray !== undefined) mapInstance.setPaintProperty(layerId, "line-dasharray", next.dashArray ? [2, 3] : [1, 0]);
      return this;
    },
    setTooltipContent(content) { tooltip = content; return this; },
    remove() {
      popup.remove();
      mapInstance.off("mouseenter", layerId, showTooltip);
      mapInstance.off("mouseleave", layerId, hideTooltip);
      mapInstance.off("click", layerId, onClick);
      if (mapInstance.getLayer(layerId)) mapInstance.removeLayer(layerId);
      if (mapInstance.getSource(sourceId)) mapInstance.removeSource(sourceId);
    },
  };
  mapLayers.routes.push(line);
  return line;
}

async function roadGeometry(points, mode, signal) {
  const coordinates = points.map(([latitude, longitude]) => `${longitude},${latitude}`).join(";");
  const key = `${state.demoSource || "local"}:${mode}:${coordinates}`;
  if (routeGeometryCache.has(key)) return routeGeometryCache.get(key);
  if (Date.now() - (routeGeometryFailures.get(key) || 0) < 30000) throw new Error("Сервис маршрутов недоступен");
  const source = state.demoSource ? `&demo=${encodeURIComponent(state.demoSource)}` : "";
  const response = await fetch(`/api/route?mode=${encodeURIComponent(mode)}&points=${encodeURIComponent(coordinates)}${source}`, { signal });
  const payload = await response.json();
  if (!response.ok) { routeGeometryFailures.set(key, Date.now()); throw new Error(payload.error || "Маршрут по дорогам недоступен"); }
  if (!Array.isArray(payload.geometry?.coordinates) || payload.geometry.coordinates.length < 2) throw new Error("Пустая геометрия маршрута");
  const geometry = payload.geometry.coordinates.map(([longitude, latitude]) => [latitude, longitude]);
  routeGeometryCache.set(key, geometry);
  if (routeGeometryCache.size > 200) routeGeometryCache.delete(routeGeometryCache.keys().next().value);
  return geometry;
}

async function renderMap() {
  if (!ensureMap()) return;
  const token = ++mapRenderToken;
  mapRouteController?.abort();
  const controller = new AbortController();
  mapRouteController = controller;
  await mapReady();
  if (token !== mapRenderToken) return;
  clearMapLayers();
  const input = workingInput();
  const locations = new Map(input.locations.map((item) => [item.id, item]));
  const { assignments, unassigned } = indexPlan();
  const selectedRoute = selectedMapRoute();
  const selectedIds = new Set(selectedRoute?.stops.map((stop) => stop.request_id) || []);
  byId("map-retry").hidden = true;
  const allPoints = [];
  const officeIds = new Set(selectedRoute ? [selectedRoute.start_location_id] : state.input.technicians.map((item) => item.start_location_id));
  for (const locationId of officeIds) {
    const location = locations.get(locationId);
    const point = locationPoint(location);
    if (!point) continue;
    allPoints.push(point);
    addMapMarker(
      point,
      "office-map-icon",
      "<span>ОФИС</span>",
      `<strong>Точка старта</strong>${escapeHtml(location.address || location.id)}`,
    );
  }
  const locationUse = new Map();
  for (const request of input.requests) {
    if (selectedRoute && !selectedIds.has(request.id)) continue;
    const location = locations.get(request.location_id);
    const basePoint = locationPoint(location);
    if (!location || !basePoint) continue;
    const useIndex = locationUse.get(location.id) || 0;
    locationUse.set(location.id, useIndex + 1);
    const point = [basePoint[0] + useIndex * 0.00012, basePoint[1] + useIndex * 0.00012];
    allPoints.push(point);
    const assignment = assignments.get(request.id);
    const routeIndex = assignment?.routeIndex ?? 0;
    const color = isPending(request.id) ? "#b95f27" : unassigned.has(request.id) ? "#a23f35" : ROUTE_COLORS[routeIndex % ROUTE_COLORS.length];
    const status = isPending(request.id) ? "Ожидает расчёта" : assignment ? `Инженер: ${escapeHtml(assignment.route.technician_name)}` : "Не назначена";
    const compact = !selectedRoute && state.input.requests.length > 50;
    addMapMarker(
      point,
      compact ? "request-map-dot" : `request-map-icon ${unassigned.has(request.id) ? "unassigned" : ""} ${state.selectedRequestId === request.id ? "selected" : ""}`,
      compact ? `<span style="background:${color}"></span>` : `<span style="background:${color}">${assignment?.stop.sequence || "!"}</span>`,
      `<strong>${escapeHtml(request.id)} · ${timeLabel(request.window.start)}–${timeLabel(request.window.end)}</strong>${escapeHtml(location.address || location.id)}<br>${status}`,
      () => selectRequest(request.id),
    );
  }

  if (allPoints.length) {
    const bounds = new maplibregl.LngLatBounds();
    for (const [latitude, longitude] of allPoints) bounds.extend([longitude, latitude]);
    mapInstance.fitBounds(bounds, { padding: 40, duration: 0, maxZoom: 15 });
  }
  if (!selectedRoute && state.selectedTechnicianId) {
    byId("map-caption").textContent = "У этой бригады нет назначений";
    return;
  }
  await drawRoadRoutes(selectedRoute ? [selectedRoute] : state.plan.routes, locations, controller, token, Boolean(selectedRoute));
}

async function completeRoadGeometry(points, mode, signal) {
  const geometry = [];
  // Adjacent chunks share a stop. Never join across missing coordinates.
  for (let offset = 0; offset < points.length - 1; offset += 24) {
    if (signal.aborted) throw new Error("Загрузка отменена");
    const segment = await roadGeometry(points.slice(offset, offset + 25), mode, signal);
    geometry.push(...(offset ? segment.slice(1) : segment));
  }
  return geometry;
}

async function drawRoadRoutes(routes, locations, controller, token, focused) {
  // Draw every schematic immediately, then replace one at a time with roads.
  // Sequential loading respects the public demo's rate limit and remains cancellable.
  const entries = routes.filter((route) => route.stops.length).map((route) => {
    const index = state.plan.routes.findIndex((item) => item.technician_id === route.technician_id);
    const points = [route.start_location_id, ...route.stops.map((stop) => stop.location_id)]
      .map((id) => locationPoint(locations.get(id)));
    if (points.some((point) => !point) || points.length < 2) return { route, line: null };
    const line = createRouteLine(
      points,
      { color: ROUTE_COLORS[index % ROUTE_COLORS.length], weight: focused ? 4 : 2, opacity: focused ? .7 : .45, dashArray: "7 9" },
      `${escapeHtml(route.technician_name)} · схема порядка выездов`,
      () => selectTechnician(route.technician_id),
    );
    return { route, points, line };
  });
  let loaded = 0, failed = 0;
  const source = state.demoSource ? "публичный OSRM, демо" : "локальный OSRM";
  const updateCaption = (pending) => {
    const cutoff = focused && state.routeThroughRequestId === state.selectedRequestId ? routes[0].stops.at(-1) : null;
    const name = focused ? `${routes[0].technician_name} · ${cutoff ? `до ${cutoff.request_id} включительно (${timeLabel(cutoff.service_start_at)}) · ` : ""}` : "";
    byId("map-caption").textContent = entries.length
      ? `${name}По дорогам: ${loaded}/${entries.length} · ${source}${pending ? " · загрузка…" : ""}${loaded < entries.length ? " · пунктир — схема" : ""}${failed ? ` · недоступно: ${failed}` : ""}`
      : "Нет назначенных маршрутов";
    byId("map-retry").hidden = !failed;
  };
  updateCaption(entries.length > 0);
  for (const { route, points, line } of entries) {
    if (controller.signal.aborted || token !== mapRenderToken) return;
    if (!line) { failed++; updateCaption(true); continue; }
    const routeController = new AbortController();
    const cancel = () => routeController.abort();
    controller.signal.addEventListener("abort", cancel, { once: true });
    const timeout = setTimeout(cancel, 12000);
    try {
      const mode = state.input.technicians.find((tech) => tech.id === route.technician_id)?.vehicle;
      const geometry = await completeRoadGeometry(points, mode, routeController.signal);
      if (token !== mapRenderToken) return;
      line.setLatLngs(geometry).setStyle({ dashArray: null, opacity: focused ? .9 : .65, weight: focused ? 5 : 3 });
      line.setTooltipContent(`${escapeHtml(route.technician_name)} · дорожный маршрут`);
      loaded++;
    } catch {
      if (token !== mapRenderToken) return;
      failed++;
    } finally {
      clearTimeout(timeout);
      controller.signal.removeEventListener("abort", cancel);
    }
    if (controller.signal.aborted || token !== mapRenderToken) return;
    updateCaption(loaded + failed < entries.length);
  }
  updateCaption(false);
}

function selectedMapRoute() {
  const route = state.plan.routes.find((item) => item.technician_id === state.selectedTechnicianId)
    || indexPlan().assignments.get(state.selectedRequestId)?.route;
  if (!route || state.routeThroughRequestId !== state.selectedRequestId || !state.routeThroughRequestId) return route;
  const index = route.stops.findIndex((stop) => stop.request_id === state.routeThroughRequestId);
  return index < 0 ? route : { ...route, stops: route.stops.slice(0, index + 1) };
}

function selectRequest(requestId, routePrefix = false) {
  state.selectedRequestId = requestId;
  state.selectedTechnicianId = null;
  state.routeThroughRequestId = routePrefix ? requestId : null;
  renderRequests();
  renderMap();
  renderTimeline(); renderTechnicians(); renderDecision();
}

function bindRequestSelection(container, routePrefix = false) {
  container.querySelectorAll("[data-request-id]").forEach((element) => element.addEventListener("click", () => {
    selectRequest(element.dataset.requestId, routePrefix);
  }));
}

function renderTimeline() {
  const stops = state.plan.routes.flatMap((route) => route.stops);
  const starts = [...state.input.technicians.map((tech) => tech.available_from), ...stops.map((stop) => stop.service_start_at)].map((value) => new Date(value).getTime());
  const ends = [...state.input.technicians.map((tech) => tech.shift_end), ...stops.map((stop) => stop.service_end_at)].map((value) => new Date(value).getTime());
  const start = starts.length ? Math.min(...starts) : new Date().setHours(9, 0, 0, 0);
  const end = ends.length ? Math.max(...ends) : start + 9 * 3600000;
  const duration = Math.max(60000, end - start);
  const hours = duration / 3600000;
  const zoom = state.timelineZoom;
  byId("timeline-canvas").style.minWidth = zoom ? `${145 + hours * 240 * zoom}px` : "100%";
  byId("timeline-zoom-label").textContent = zoom ? `${Math.round(zoom * 100)}%` : "Вся смена";
  byId("timeline-zoom-out").disabled = zoom === 0;
  byId("timeline-zoom-in").disabled = zoom >= 4;
  const ticks = zoom ? Math.max(1, Math.ceil(hours * (zoom >= 2 ? 2 : 1))) : 3;
  document.querySelector(".timeline-scale").innerHTML = Array.from({ length: ticks + 1 }, (_, index) => `<span style="left:${index / ticks * 100}%">${timeLabel(start + duration * index / ticks)}</span>`).join("");
  byId("timeline-canvas").style.setProperty("--timeline-grid", `${100 / ticks}%`);
  const routeByTech = new Map(state.plan.routes.map((route, index) => [route.technician_id, { route, index }]));
  const technicians = state.selectedTechnicianId ? state.input.technicians.filter((tech) => tech.id === state.selectedTechnicianId) : state.input.technicians;
  byId("timeline").innerHTML = technicians.map((tech) => {
    const item = routeByTech.get(tech.id);
    const blocks = (item?.route.stops || []).map((stop) => {
      const left = Math.max(0, (new Date(stop.service_start_at).getTime() - start) / duration * 100);
      const width = Math.min(100 - left, (new Date(stop.service_end_at) - new Date(stop.service_start_at)) / duration * 100);
      const color = ROUTE_COLORS[(item?.index || 0) % ROUTE_COLORS.length];
      return `<button class="timeline-block ${state.selectedRequestId === stop.request_id ? "selected" : ""}" data-request-id="${escapeHtml(stop.request_id)}" style="left:${left}%;width:${width}%;background:${color}" aria-label="${escapeHtml(stop.request_id)}: ${timeLabel(stop.service_start_at)}–${timeLabel(stop.service_end_at)}" title="${escapeHtml(stop.request_id)}: ${timeLabel(stop.service_start_at)}–${timeLabel(stop.service_end_at)}"><strong>${escapeHtml(stop.request_id)}</strong><span>${timeLabel(stop.service_start_at)}–${timeLabel(stop.service_end_at)}</span></button>`;
    }).join("");
    return `<div class="timeline-row"><div class="timeline-tech"><strong>${escapeHtml(tech.name)}</strong><span>${item ? `${item.route.metrics.request_count} заявок · ${item.route.metrics.distance_km.toFixed(1)} км` : "Свободен"}</span></div><div class="timeline-track">${blocks}</div></div>`;
  }).join("");
  bindRequestSelection(byId("timeline"), true);
}

function changeTimelineZoom(direction) {
  const levels = [0, .5, 1, 2, 4];
  const index = levels.indexOf(state.timelineZoom);
  const viewport = byId("timeline-scroll");
  const fraction = viewport.scrollLeft / Math.max(1, viewport.scrollWidth - viewport.clientWidth);
  state.timelineZoom = direction === 0 ? 0 : levels[Math.max(0, Math.min(levels.length - 1, index + direction))];
  renderTimeline();
  viewport.scrollLeft = state.timelineZoom === 0 ? 0 : fraction * Math.max(0, viewport.scrollWidth - viewport.clientWidth);
}

function selectTechnician(technicianId) {
  state.routeThroughRequestId = null;
  state.selectedTechnicianId = technicianId;
  state.selectedRequestId = null;
  renderRequests(); renderTimeline(); renderTechnicians(); renderDecision(); renderMap();
}

function renderTechnicians() {
  const routeByTech = new Map(state.plan.routes.map((route, index) => [route.technician_id, { route, index }]));
  const query = state.technicianQuery.trim().toLowerCase();
  const matching = state.input.technicians.filter((tech) => {
    const assigned = Boolean(routeByTech.get(tech.id)?.route.stops.length);
    return `${tech.name} ${tech.id}`.toLowerCase().includes(query)
      && (state.technicianFilter === "all" || (state.technicianFilter === "assigned" ? assigned : !assigned));
  });
  byId("tech-count").textContent = `${matching.length} / ${state.input.technicians.length}`;
  const visible = paginate(matching, "technicianPage", TECHNICIAN_PAGE_SIZE, "technicians");
  byId("technician-list").innerHTML = visible.map((tech) => {
    const item = routeByTech.get(tech.id);
    const service = item?.route.metrics.service_minutes || 0;
    const travel = item?.route.metrics.travel_minutes || 0;
    const shift = (new Date(tech.shift_end) - new Date(tech.available_from)) / 60000;
    const load = Math.min(100, Math.round((service + travel) / shift * 100));
    const color = item ? ROUTE_COLORS[item.index % ROUTE_COLORS.length] : "#9b9e95";
    return `<button class="technician ${state.selectedTechnicianId === tech.id ? "selected" : ""}" data-technician-id="${escapeHtml(tech.id)}" type="button" aria-pressed="${state.selectedTechnicianId === tech.id}"><span class="technician-head"><span class="tech-avatar" style="background:${color}">${escapeHtml(tech.name.slice(0, 2).toUpperCase())}</span><span class="technician-name"><strong>${escapeHtml(tech.name)}</strong><span>${tech.skills.map(skillLabel).map(escapeHtml).join(", ")}</span></span><span class="tech-count">${item?.route.metrics.request_count || 0}</span></span><span class="load-bar"><i style="width:${load}%;background:${color}"></i></span><span class="tech-meta"><span>Загрузка ${load}%</span><span>${item ? `${item.route.metrics.distance_km.toFixed(1)} км` : "Без маршрута"}</span></span></button>`;
  }).join("") || '<p class="empty-state">Бригады не найдены. Измените поиск или фильтр.</p>';
  byId("technician-list").querySelectorAll("[data-technician-id]").forEach((button) => button.addEventListener("click", () => {
    selectTechnician(button.dataset.technicianId);
  }));
}

function renderDecision() {
  const technician = state.input.technicians.find((item) => item.id === state.selectedTechnicianId);
  if (technician) {
    const route = state.plan.routes.find((item) => item.technician_id === technician.id);
    const shiftMinutes = (new Date(technician.shift_end) - new Date(technician.available_from)) / 60000;
    const occupied = (route?.metrics.service_minutes || 0) + (route?.metrics.travel_minutes || 0);
    const load = Math.min(100, Math.round(occupied / shiftMinutes * 100));
    const vehicle = technician.vehicle === "car" ? "Автомобиль" : technician.vehicle === "pedestrian" ? "Пешком" : technician.vehicle;
    const stops = (route?.stops || []).map((stop) => `<button type="button" data-request-id="${escapeHtml(stop.request_id)}"><span>${stop.sequence}. ${escapeHtml(stop.request_id)}</span><time>${timeLabel(stop.service_start_at)}</time></button>`).join("");
    byId("decision-title").textContent = "Инженер и маршрут";
    byId("decision-content").innerHTML = `<div class="decision-card"><div class="decision-title"><strong>${escapeHtml(technician.name)}</strong><span class="assignment">${route ? "На маршруте" : "Свободен"}</span></div><p>${escapeHtml(technician.skills.map(skillLabel).join(", "))}</p><dl><dt>Смена</dt><dd>${timeLabel(technician.available_from)}–${timeLabel(technician.shift_end)}</dd><dt>Транспорт</dt><dd>${escapeHtml(vehicle)}</dd><dt>Загрузка</dt><dd>${load}%</dd><dt>Заявок</dt><dd>${route?.metrics.request_count || 0}</dd><dt>Пробег</dt><dd>${route ? `${route.metrics.distance_km.toFixed(1)} км` : "0 км"}</dd><dt>В дороге</dt><dd>${route?.metrics.travel_minutes || 0} мин</dd><dt>Работы</dt><dd>${route?.metrics.service_minutes || 0} мин</dd><dt>Завершение</dt><dd>${route ? timeLabel(route.finish_at) : "—"}</dd></dl>${stops ? `<div class="engineer-route">${stops}</div>` : ""}</div>`;
    bindRequestSelection(byId("decision-content"));
    return;
  }
  byId("decision-title").textContent = "Заявка и назначение";
  const input = workingInput();
  const request = input.requests.find((item) => item.id === state.selectedRequestId);
  if (!request) {
    byId("decision-content").innerHTML = "<p>Выберите заявку на карте или в списке.</p>";
    return;
  }
  const locations = new Map(input.locations.map((item) => [item.id, item]));
  if (isPending(request.id)) {
    byId("decision-content").innerHTML = `<div class="decision-card"><div class="decision-title"><strong>${escapeHtml(request.id)}</strong><span>Ожидает расчёта</span></div><p>Заявка сохранена в текущей сессии. Действующий план пока не изменён.</p><dl><dt>Адрес</dt><dd>${escapeHtml(locations.get(request.location_id)?.address)}</dd><dt>Работы</dt><dd>${escapeHtml(workTypeLabel(request))} · ${request.duration_minutes} мин</dd><dt>Приоритет</dt><dd>${escapeHtml({urgent:"Срочный", high:"Высокий", normal:"Обычный"}[request.priority])}</dd><dt>Окно начала</dt><dd>${timeLabel(request.window.start)}–${timeLabel(request.window.end)}</dd></dl><p>Нажмите «Пересчитать» над рабочей областью.</p><button type="button" class="text-button" id="remove-pending">Удалить черновик</button></div>`;
    byId("remove-pending").disabled = state.busy;
    byId("remove-pending").addEventListener("click", () => {
      if (state.busy) return;
      state.pendingRequests = state.pendingRequests.filter((item) => item.request.id !== request.id);
      state.selectedRequestId = null; render(); setBusy(false, state.pendingRequests.length ? "Есть новые заявки" : "План рассчитан");
    });
    return;
  }
  const { assignment, unassigned } = requestMeta(request);
  const equipment = request.required_equipment?.length ? request.required_equipment.join(", ") : "не требуется";
  const assignmentControl = `<div class="manual-assignment"><label for="assignment-technician">Назначить вручную</label><div><select id="assignment-technician"><option value="">Автораспределение</option>${state.input.technicians.map((tech) => `<option value="${escapeHtml(tech.id)}" ${request.locked_technician_id === tech.id ? "selected" : ""}>${escapeHtml(tech.name)}</option>`).join("")}</select><button class="button compact" id="apply-assignment" type="button">Применить</button></div></div>`;
  if (assignment) {
    const stop = assignment.stop;
    byId("decision-content").innerHTML = `<div class="decision-card"><div class="decision-title"><strong>${escapeHtml(request.id)}</strong><span class="assignment">Назначена</span></div><p>${escapeHtml(stop.explanation.message)}</p><dl><dt>Вид работ</dt><dd>${escapeHtml(workTypeLabel(request))} · ${request.duration_minutes} мин</dd><dt>Инженер</dt><dd>${escapeHtml(assignment.route.technician_name)}</dd><dt>Адрес</dt><dd>${escapeHtml(locations.get(request.location_id)?.address || request.location_id)}</dd><dt>Окно начала</dt><dd>${timeLabel(request.window.start)}–${timeLabel(request.window.end)}</dd><dt>Плановое начало</dt><dd>${timeLabel(stop.service_start_at)}</dd><dt>Дорога</dt><dd>${stop.travel_minutes} мин · ${stop.distance_km.toFixed(1)} км</dd><dt>Оборудование</dt><dd>${escapeHtml(equipment)}</dd></dl>${assignmentControl}<div class="reason-code">${escapeHtml(stop.explanation.code)}</div></div>`;
  } else if (unassigned) {
    byId("decision-content").innerHTML = `<div class="decision-card danger"><div class="decision-title"><strong>${escapeHtml(request.id)}</strong><span class="assignment unassigned">Не назначена</span></div><p>${escapeHtml(unassigned.reason.message)}</p><dl><dt>Вид работ</dt><dd>${escapeHtml(workTypeLabel(request))} · ${request.duration_minutes} мин</dd><dt>Навыки</dt><dd>${request.required_skills.map(skillLabel).map(escapeHtml).join(", ")}</dd><dt>Транспорт</dt><dd>${escapeHtml(request.required_vehicle || "не важен")}</dd><dt>Оборудование</dt><dd>${escapeHtml(equipment)}</dd><dt>Окно начала</dt><dd>${timeLabel(request.window.start)}–${timeLabel(request.window.end)}</dd></dl>${assignmentControl}<div class="reason-code">${escapeHtml(unassigned.reason.code)}</div></div>`;
  }
  byId("apply-assignment")?.addEventListener("click", () => {
    applyManualAssignment().catch((error) => showToast(error.message, true));
  });
  byId("apply-assignment").disabled = state.busy || state.pendingRequests.length > 0;
  byId("assignment-technician").disabled = state.busy || state.pendingRequests.length > 0;
}

async function applyManualAssignment() {
  const requestId = state.selectedRequestId;
  const technicianId = byId("assignment-technician").value;
  const input = deepClone(state.input);
  const eventAt = validatedEventAt();
  assertRequestNotStarted(requestId, eventAt);
  const request = input.requests.find((item) => item.id === requestId);
  if (!request) return;
  if (technicianId) request.locked_technician_id = technicianId;
  else delete request.locked_technician_id;
  input.plan_id = `${state.originalInput.plan_id}-manual`;
  const before = state.plan;
  prepareEventSnapshot(input, before, [], eventAt);
  const next = await requestPlan(input, { keepBaseline: true, reason: "replan", fullReplan: true });
  finishComparison(before, next, input);
  renderChanges();
  const selected = input.technicians.find((item) => item.id === technicianId);
  const rejected = next.unassigned.find((item) => item.request_id === requestId);
  showToast(rejected ? `${requestId}: ${rejected.reason.message}` : selected ? `${requestId}: выбран ${selected.name}` : `${requestId}: вернута в автораспределение`, Boolean(rejected));
}

function planAssignments(plan) {
  const result = new Map();
  for (const route of plan.routes) for (const stop of route.stops) result.set(stop.request_id, route.technician_name);
  for (const item of plan.unassigned) result.set(item.request_id, null);
  return result;
}

function comparePlans(previous, current, eventAt = null) {
  const before = planAssignments(previous);
  const after = planAssignments(current);
  const oldStops = new Map(previous.routes.flatMap((route) => route.stops.map((stop) => [stop.request_id, stop])));
  const newStops = new Map(current.routes.flatMap((route) => route.stops.map((stop) => [stop.request_id, stop])));
  const ids = new Set([...before.keys(), ...after.keys()]);
  return [...ids].sort().flatMap((id) => {
    const oldValue = before.get(id); const newValue = after.get(id);
    const oldStop = oldStops.get(id), newStop = newStops.get(id);
    if (oldValue === newValue) {
      if (oldStop && newStop && (new Date(oldStop.service_start_at).getTime() !== new Date(newStop.service_start_at).getTime() || oldStop.sequence !== newStop.sequence)) {
        return [{ requestId: id, type: "neutral", title: `${id}: время / порядок`, text: `${newValue}: ${timeLabel(oldStop.service_start_at)} → ${timeLabel(newStop.service_start_at)} · позиция ${oldStop.sequence} → ${newStop.sequence}` }];
      }
      return [];
    }
    if (!before.has(id)) return [{ type: newValue ? "positive" : "negative", title: `${id} добавлена`, text: newValue ? `Назначена: ${newValue}${newStop ? ` · ${timeLabel(newStop.service_start_at)}` : ""}` : "Не удалось назначить: причина в карточке заявки" }];
    if (!after.has(id)) {
      if (eventAt && oldStop && new Date(oldStop.arrival_at || oldStop.service_start_at) <= new Date(eventAt)) {
        const finished = new Date(oldStop.service_end_at) <= new Date(eventAt);
        return [{ type: "neutral", title: `${id}: ${finished ? "завершена" : "на месте / в работе"}`, text: finished ? "Не участвует в расчёте оставшейся смены" : `Закреплена за ${oldValue} до ${timeLabel(oldStop.service_end_at)}` }];
      }
      return [{ type: "neutral", title: `${id} отменена`, text: "Удалена из плана" }];
    }
    if (!newValue) return [{ type: "negative", title: `${id} без назначения`, text: `Была у инженера ${oldValue}` }];
    return [{ type: "positive", title: `${id} переназначена`, text: `${oldValue || "без назначения"}${oldStop ? ` ${timeLabel(oldStop.service_start_at)}` : ""} → ${newValue}${newStop ? ` ${timeLabel(newStop.service_start_at)}` : ""}` }];
  });
}

function renderChanges() {
  const section = byId("changes-section");
  section.hidden = !state.changes.length && !state.comparisonSummary;
  byId("changes-summary").textContent = state.comparisonSummary;
  byId("changes-list").innerHTML = state.changes.map((item) => `<div class="change-item ${item.type}"><strong>${escapeHtml(item.title)}</strong>${escapeHtml(item.text)}</div>`).join("") || '<p class="form-note">Назначения, порядок и время выездов не изменились.</p>';
}

function buildScenario(kind) {
  const input = deepClone(state.input);
  input.plan_id = `${input.plan_id}-${kind}`;
  if (kind === "urgent" || kind === "ordinary") {
    const source = input.requests.find((item) => item.work_type === (kind === "urgent" ? "emergency" : "connection")) || input.requests[0];
    const targetLocation = input.locations.find((item) => item.id !== input.technicians[0].start_location_id && !input.requests.some((request) => request.location_id === item.id)) || input.locations.at(-1);
    const used = new Set(input.requests.map((item) => item.id));
    let number = input.requests.length + 1;
    while (used.has(`REQ-${number}`)) number += 1;
    input.requests.push({ ...deepClone(source), id: `REQ-${number}`, location_id: targetLocation.id, work_type: kind === "urgent" ? "emergency" : "connection", duration_minutes: kind === "urgent" ? 80 : 70, required_skills: [kind === "urgent" ? "emergency" : "connection"], required_vehicle: null, required_equipment: [], priority: kind === "urgent" ? "urgent" : "high", window: { start: source.window.start.replace(/T\d\d:\d\d/, "T12:30"), end: source.window.end.replace(/T\d\d:\d\d/, "T15:30") } });
  } else if (kind === "cancel") {
    delete input.replan_context;
    const assignedIds = new Set(state.plan.routes.flatMap((route) => route.stops.map((stop) => stop.request_id)));
    const target = [...input.requests].reverse().find((item) => assignedIds.has(item.id));
    if (target) input.requests = input.requests.filter((item) => item.id !== target.id);
  } else if (kind === "unavailable") {
    delete input.replan_context;
    if (input.technicians.length < 2) throw new Error("Для этого сценария нужен минимум второй инженер");
    const busyId = state.plan.routes.at(-1)?.technician_id || input.technicians.at(-1).id;
    input.technicians = input.technicians.filter((item) => item.id !== busyId);
  }
  return input;
}

function prepareEventSnapshot(input, previousPlan, newRequestId, eventAt) {
  const eventTime = new Date(eventAt).getTime();
  const pastIds = new Set();
  const previousRoutes = [];
  const activeWork = (input.replan_context?.active_work || []).filter((work) => new Date(work.finish_at).getTime() > eventTime);
  const routesByTech = new Map(previousPlan.routes.map((route) => [route.technician_id, route]));

  for (const technician of input.technicians) {
    const stops = routesByTech.get(technician.id)?.stops || [];
    const pending = [];
    let location = technician.start_location_id;
    let available = new Date(technician.available_from).getTime();
    for (const stop of stops) {
      const ends = new Date(stop.service_end_at).getTime();
      if (ends <= eventTime) {
        pastIds.add(stop.request_id);
        location = stop.location_id;
        available = ends;
      } else if (new Date(stop.arrival_at || stop.service_start_at).getTime() <= eventTime) {
        pastIds.add(stop.request_id);
        location = stop.location_id;
        available = ends;
        if (!activeWork.some((work) => work.request_id === stop.request_id)) activeWork.push({ technician_id: technician.id, request_id: stop.request_id, location_id: location, finish_at: stop.service_end_at });
      } else {
        if (input.requests.some((request) => request.id === stop.request_id)) pending.push(stop.request_id);
      }
    }
    technician.start_location_id = location;
    technician.available_from = new Date(Math.max(eventTime, available)).toISOString();
    previousRoutes.push({ technician_id: technician.id, request_ids: pending });
  }
  input.requests = input.requests.filter((request) => !pastIds.has(request.id));
  input.replan_context = { event_at: eventAt, new_request_ids: Array.isArray(newRequestId) ? newRequestId : [newRequestId], previous_routes: previousRoutes, active_work: activeWork };
}

async function applyScenario(kind) {
  if (state.pendingRequests.length) throw new Error("Сначала рассчитайте или удалите новые заявки");
  const eventAt = validatedEventAt();
  const before = state.plan;
  const input = buildScenario(kind);
  let newIds = [];
  if (kind === "urgent" || kind === "ordinary") {
    const oldIds = new Set(state.input.requests.map((request) => request.id));
    const added = input.requests.find((request) => !oldIds.has(request.id));
    newIds = [added.id];
  } else {
    for (const work of state.input.replan_context?.active_work || []) {
      if (new Date(work.finish_at) > new Date(eventAt) && !input.technicians.some((tech) => tech.id === work.technician_id)) throw new Error(`${work.request_id}: инженер занят текущей работой до ${timeLabel(work.finish_at)}`);
    }
    for (const route of before.routes) {
      for (const stop of route.stops) {
        if (!input.requests.some((request) => request.id === stop.request_id) || !input.technicians.some((tech) => tech.id === route.technician_id)) assertRequestNotStarted(stop.request_id, eventAt);
      }
    }
  }
  prepareEventSnapshot(input, before, newIds, eventAt);
  const next = await requestPlan(input, { keepBaseline: true, reason: "replan", fullReplan: !newIds.length });
  finishComparison(before, next, input);
  renderChanges();
  showToast(`План перестроен: ${next.metrics.assigned_count} заявок назначено`);
}

function showToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.style.background = isError ? "#8d3029" : "#252722";
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3200);
}

function exportPlan() {
  const blob = new Blob([JSON.stringify(state.plan, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${state.plan.plan_id}-output.json`;
  link.click();
  URL.revokeObjectURL(link.href);
  showToast("План экспортирован в JSON");
}

function registerWebMcpTools() {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  const tools = [
    {
      name: "read_plan_summary",
      title: "Прочитать сводку плана",
      description: "Возвращает текущие показатели и список неназначенных заявок без изменения плана.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: false },
      execute: () => ({ planId: state.plan?.plan_id, metrics: state.plan?.metrics, unassigned: state.plan?.unassigned || [] }),
    },
    {
      name: "apply_replanning_scenario",
      title: "Перестроить план",
      description: "Применяет один демонстрационный сценарий и обновляет видимый план после расчёта.",
      inputSchema: { type: "object", properties: { scenario: { type: "string", enum: ["urgent", "ordinary", "cancel", "unavailable"] } }, required: ["scenario"], additionalProperties: false },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      async execute(input) {
        if (!["urgent", "ordinary", "cancel", "unavailable"].includes(input?.scenario)) throw new Error("Unknown scenario");
        await applyScenario(input.scenario);
        return { planId: state.plan.plan_id, metrics: state.plan.metrics, changes: state.changes };
      },
    },
  ];
  const lifecycle = new AbortController();
  for (const tool of tools) Promise.resolve(context.registerTool(tool, { signal: lifecycle.signal })).catch(() => {});
}

function moscowLocalValue(value) {
  return new Date(new Date(value).getTime() + 3 * 3600000).toISOString().slice(0, 16);
}

function parseMoscow(value) {
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(value)) throw new Error("Укажите дату и время");
  const parsed = new Date(`${value}:00+03:00`);
  if (!Number.isFinite(parsed.getTime()) || moscowLocalValue(parsed) !== value) throw new Error("Некорректная дата или время");
  return parsed.toISOString();
}

function isPending(id) { return state.pendingRequests.some((item) => item.request.id === id); }

function workingInput() {
  const input = deepClone(state.input);
  for (const { request, location } of state.pendingRequests) {
    if (!input.requests.some((item) => item.id === request.id)) input.requests.push(deepClone(request));
    if (location && !input.locations.some((item) => item.id === location.id)) input.locations.push(deepClone(location));
  }
  return input;
}

function renderPendingStatus() {
  const count = state.pendingRequests.length;
  byId("pending-label").textContent = count ? `Ожидают расчёта: ${count}` : "Нет новых заявок";
  byId("live-note").textContent = count ? "Сохранён прежний план. Новые заявки ещё не распределены." : state.lastEventAt ? `План оставшейся смены с ${timeLabel(state.lastEventAt)} · сравнение с предыдущим расчётом` : "Создайте заявку, затем пересчитайте план.";
  byId("recalculate-button").disabled = state.busy || !state.plan;
  byId("event-at").disabled = state.busy || !state.plan;
  byId("save-request").disabled = state.busy || !state.plan;
}

function applyWorkDefaults() {
  const type = byId("new-work-type").value;
  const normative = state.planningConfig?.normatives?.[type];
  byId("new-duration").value = normative?.duration_minutes || {connection:70, emergency:80, equipment_order:20, local_repair:30}[type];
  byId("new-skill").value = type === "emergency" ? "emergency" : "connection";
  byId("new-priority").value = type === "emergency" ? "urgent" : type === "connection" ? "high" : "normal";
  byId("new-equipment").value = type === "connection" ? "router" : type === "emergency" ? "repair-kit" : "";
}

let addressSearchController;
let addressSearchVersion = 0;

function cancelAddressSearch() {
  addressSearchVersion++;
  addressSearchController?.abort();
  byId("find-address").disabled = false;
  byId("find-address").textContent = "Найти";
}

function chooseAddress(point) {
  byId("new-location").value = point.id || "__new__";
  byId("new-address").value = point.address;
  byId("new-latitude").value = point.latitude;
  byId("new-longitude").value = point.longitude;
  byId("address-results").hidden = true;
  byId("new-location-note").textContent = `Точка выбрана: ${point.latitude}, ${point.longitude}. Проверьте адрес и номер дома.`;
}

function updateAddressInput() {
  cancelAddressSearch();
  byId("new-location").value = "__new__";
  byId("new-latitude").value = "";
  byId("new-longitude").value = "";
  byId("address-results").hidden = true;
  byId("new-location-note").textContent = "Введите полный адрес и нажмите «Найти» или укажите координаты вручную.";
  const query = byId("new-address").value.trim().toLocaleLowerCase("ru");
  const matches = workingInput().locations.filter((point) => point.address?.trim().toLocaleLowerCase("ru") === query);
  if (matches.length === 1) chooseAddress(matches[0]);
}

async function findAddress() {
  cancelAddressSearch();
  const version = addressSearchVersion;
  const query = byId("new-address").value.trim();
  if (query.length < 3) { byId("new-location-note").textContent = "Введите город, улицу и дом (не менее 3 символов)."; return; }
  byId("address-results").hidden = true;
  byId("new-location").value = "__new__";
  byId("new-latitude").value = ""; byId("new-longitude").value = "";
  const local = workingInput().locations.filter((point) => point.address?.toLocaleLowerCase("ru").includes(query.toLocaleLowerCase("ru"))).slice(0, 8);
  const controller = new AbortController();
  addressSearchController = controller;
  const timeout = setTimeout(() => controller.abort(), 10000);
  byId("find-address").disabled = true;
  byId("find-address").textContent = "Поиск…";
  byId("new-location-note").textContent = local.length ? "Ищем в текущем наборе…" : state.planningConfig?.geocoding_mode === "public_demo" ? "Ищем адрес в бесплатном API…" : "Ищем адрес локально…";
  try {
    if (!local.length && !state.planningConfig) throw new Error("Не удалось определить сервис поиска. Закройте и откройте форму, чтобы загрузить настройки.");
    const response = local.length ? {results:local,source:"dataset"} : await apiJson("/api/geocode", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({query}), signal:controller.signal});
    const results = response.results;
    if (version !== addressSearchVersion) return;
    byId("new-location-note").textContent = results.length ? "Выберите точный адрес из результатов. Точка не выбирается автоматически." : "Адрес не найден. Уточните город и дом или укажите координаты вручную.";
    byId("address-results").innerHTML = results.map((point, index) => `<button type="button" data-address-index="${index}">${escapeHtml(point.address)}<small>${escapeHtml(point.latitude)}, ${escapeHtml(point.longitude)} · ${response.source === "dataset" ? "из текущего набора" : response.source === "public_demo" ? "бесплатный API · OpenStreetMap" : "локальный геокодер"}</small></button>`).join("");
    byId("address-results").hidden = !results.length;
    byId("address-results").querySelectorAll("[data-address-index]").forEach((button) => button.addEventListener("click", () => chooseAddress(results[Number(button.dataset.addressIndex)])));
  } catch (error) {
    if (version !== addressSearchVersion) return;
    byId("new-location-note").textContent = controller.signal.aborted ? "Поиск не ответил за 10 секунд. Повторите или укажите координаты вручную." : error.message;
    byId("coordinate-details").open = true;
  } finally {
    clearTimeout(timeout);
    if (version === addressSearchVersion) { byId("find-address").disabled = false; byId("find-address").textContent = "Найти"; }
  }
}

async function openRequestForm() {
  if (state.busy || !state.plan) return;
  try { state.planningConfig = await apiJson("/api/planning-config"); }
  catch { state.planningConfig = null; }
  if (state.busy) return;
  const input = workingInput();
  byId("request-form").reset();
  byId("request-form-error").hidden = true;
  let number = 1;
  while (input.requests.some((request) => request.id === `NEW-${number}`) || state.seenRequestIds.includes(`NEW-${number}`)) number++;
  byId("new-request-id").value = `NEW-${number}`;
  byId("known-addresses").innerHTML = input.locations.map((point) => `<option value="${escapeHtml(point.address || point.id)}"></option>`).join("");
  byId("coordinate-details").open = false;
  const publicGeocoding = state.planningConfig?.geocoding_mode === "public_demo";
  byId("geocoding-privacy-note").textContent = publicGeocoding ? "Демо: по кнопке «Найти» адрес отправляется во внешний Nominatim. Только публичные тестовые адреса — не вводите данные клиентов. До 1 запроса в секунду на приложение, без автопоиска при вводе." : state.planningConfig ? "Поиск через локальный геокодер. Адрес не отправляется внешнему сервису." : "Настройки поиска недоступны. Можно выбрать известный адрес или указать координаты вручную.";
  byId("geocoding-attribution").hidden = !publicGeocoding;
  updateAddressInput();
  byId("new-routing-note").textContent = state.planningConfig?.matrix_mode === "gateway" ? "Время поездок к новой точке рассчитает локальный OSRM." : "Сейчас включена тестовая матрица. Новый адрес можно сохранить, но для пересчёта новых координат нужен локальный OSRM в режиме gateway.";
  const now = byId("event-at").value;
  const day = now.slice(0, 10);
  byId("new-window-start").value = now;
  byId("new-window-end").value = `${day}T18:00`;
  applyWorkDefaults();
  byId("request-dialog").showModal();
}

function makePendingRequest(values) {
  const input = workingInput();
  const id = values.id.trim();
  if (!id || id.length > 80) throw new Error("Введите номер заявки до 80 символов");
  if (input.requests.some((request) => request.id === id) || state.seenRequestIds.includes(id)) throw new Error("Заявка с таким номером уже существует");
  if (!Object.hasOwn(WORK_TYPE_LABELS, values.type)) throw new Error("Выберите вид работ");
  if (!["normal", "high", "urgent"].includes(values.priority)) throw new Error("Выберите приоритет");
  const duration = Number(values.duration);
  if (!Number.isInteger(duration) || duration < 1 || duration > 1440) throw new Error("Длительность: целое число от 1 до 1440 минут");
  const start = parseMoscow(values.start), end = parseMoscow(values.end);
  if (new Date(start) >= new Date(end)) throw new Error("Конец окна должен быть позже начала");
  const eventAt = parseMoscow(byId("event-at").value);
  if (new Date(end) < new Date(eventAt)) throw new Error("Окно заявки уже закончилось к моменту события");
  if (!Object.hasOwn(SKILL_LABELS, values.skill)) throw new Error("Выберите навык");
  if (!["", "car", "pedestrian"].includes(values.vehicle)) throw new Error("Выберите транспорт");
  let location = null, locationId = values.locationId;
  if (locationId === "__new__") {
    const latitude = Number(values.latitude), longitude = Number(values.longitude);
    if (!values.address.trim() || values.address.trim().length > 300 || !String(values.latitude).trim() || !String(values.longitude).trim() || !Number.isFinite(latitude) || !Number.isFinite(longitude) || Math.abs(latitude) > 90 || Math.abs(longitude) > 180) throw new Error("Укажите адрес и корректные координаты");
    locationId = `location-${id}`;
    while (input.locations.some((point) => point.id === locationId)) locationId += "-new";
    location = {id:locationId, address:values.address.trim(), latitude, longitude};
  } else if (!input.locations.some((point) => point.id === locationId)) throw new Error("Выберите адрес");
  return { location, request: {id, location_id:locationId, work_type:values.type, priority:values.priority, duration_minutes:duration, window:{start,end}, required_skills:[values.skill], required_vehicle:values.vehicle || null, required_equipment:[...new Set(values.equipment.split(",").map((item) => item.trim()).filter(Boolean))]} };
}

function savePendingRequest(event) {
  event.preventDefault();
  if (state.busy) return;
  try {
    const item = makePendingRequest({id:byId("new-request-id").value, locationId:byId("new-location").value, address:byId("new-address").value, latitude:byId("new-latitude").value, longitude:byId("new-longitude").value, type:byId("new-work-type").value, priority:byId("new-priority").value, duration:byId("new-duration").value, start:byId("new-window-start").value, end:byId("new-window-end").value, skill:byId("new-skill").value, vehicle:byId("new-vehicle").value, equipment:byId("new-equipment").value});
    state.pendingRequests.push(item);
    state.selectedRequestId = item.request.id; state.selectedTechnicianId = null;
    state.query = item.request.id; state.filter = "all"; state.requestPage = 0;
    byId("request-search").value = state.query;
    document.querySelectorAll(".tab").forEach((tab) => { const active = tab.dataset.filter === "all"; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", active); });
    byId("request-dialog").close();
    render(); setBusy(false, "Есть новые заявки");
    showToast(`${item.request.id} сохранена. Нажмите «Пересчитать».`);
  } catch (error) {
    byId("request-form-error").textContent = error.message; byId("request-form-error").hidden = false;
  }
}

function comparisonBaseline(before, input) {
  if (!input.replan_context) return deepClone(before);
  const eventTime = new Date(input.replan_context.event_at).getTime();
  const result = deepClone(before);
  result.routes = result.routes.map((route) => ({...route, stops:route.stops.filter((stop) => new Date(stop.arrival_at || stop.service_start_at).getTime() > eventTime)})).filter((route) => route.stops.length);
  const stops = result.routes.flatMap((route) => route.stops);
  result.metrics = {...result.metrics, request_count:stops.length + result.unassigned.length, assigned_count:stops.length, unassigned_count:result.unassigned.length, used_technicians:result.routes.length,
    distance_km:stops.reduce((sum,stop) => sum + stop.distance_km, 0), travel_minutes:stops.reduce((sum,stop) => sum + stop.travel_minutes, 0)};
  return result;
}

function finishComparison(before, next, input) {
  state.baseline = comparisonBaseline(before, input);
  state.lastEventAt = input.replan_context?.event_at || state.lastEventAt;
  state.changes = comparePlans(before, next, input.replan_context?.event_at);
  const old = state.baseline.metrics, current = next.metrics;
  state.comparisonSummary = `Оставшаяся смена${state.lastEventAt ? ` с ${timeLabel(state.lastEventAt)}` : ""}. Назначено: ${old.assigned_count} → ${current.assigned_count}. Пробег: ${old.distance_km.toFixed(1)} → ${current.distance_km.toFixed(1)} км. В пути: ${old.travel_minutes} → ${current.travel_minutes} мин.`;
  renderMetrics(); renderPendingStatus();
}

async function recalculatePending() {
  if (state.busy || !state.plan) return;
  const eventAt = validatedEventAt();
  const before = deepClone(state.plan);
  const input = workingInput();
  const ids = state.pendingRequests.map((item) => item.request.id);
  const newCoordinates = state.pendingRequests.some((item) => item.location);
  if (newCoordinates) {
    const config = await apiJson("/api/planning-config");
    if (config.matrix_mode !== "gateway") throw new Error("Заявки сохранены. Для новых координат включите локальный OSRM и ROUTING_MATRIX_MODE=gateway. Тестовая матрица не содержит пути к новому адресу.");
  }
  prepareEventSnapshot(input, before, ids, eventAt);
  input.plan_id = `${state.originalInput.plan_id}-live-${Date.now()}`;
  const next = await requestPlan(input, {keepBaseline:true, reason:"replan", demoSource:newCoordinates ? null : state.demoSource});
  state.pendingRequests = [];
  finishComparison(before, next, input);
  state.query = ""; byId("request-search").value = "";
  render(); setBusy(false, "План пересчитан");
  showToast(`План пересчитан. Новых заявок: ${ids.length}. Изменения показаны справа.`);
}

function assertRequestNotStarted(requestId, eventAt) {
  const stop = state.plan.routes.flatMap((route) => route.stops).find((stop) => stop.request_id === requestId);
  if (stop && new Date(stop.arrival_at || stop.service_start_at) <= new Date(eventAt)) throw new Error(`${requestId}: бригада уже на месте или работа завершена. Переназначение и отмена заблокированы.`);
}

function validatedEventAt() {
  const eventAt = parseMoscow(byId("event-at").value);
  if (state.lastEventAt && new Date(eventAt) < new Date(state.lastEventAt)) throw new Error("Время события нельзя сдвигать назад. Верните исходный план для новой демонстрации.");
  const ends = state.input.technicians.map((tech) => new Date(tech.shift_end).getTime());
  const starts = state.originalInput.technicians.map((tech) => new Date(tech.available_from).getTime());
  if (!starts.length || new Date(eventAt).getTime() < Math.min(...starts) || new Date(eventAt).getTime() >= Math.max(...ends)) throw new Error("Время события должно быть внутри текущей смены");
  return eventAt;
}

function openDemoEvents() {
  if (state.busy || !state.plan) return;
  const pending = state.pendingRequests.length;
  byId("demo-pending-note").hidden = !pending;
  byId("demo-pending-count").textContent = `Ожидают расчёта: ${pending}. Сначала пересчитайте сохранённые заявки. После этого можно применить демо-событие.`;
  byId("demo-scenarios").disabled = Boolean(pending);
  byId("apply-scenario").disabled = Boolean(pending);
  byId("demo-go-recalculate").hidden = !pending;
  byId("replan-dialog").showModal();
}

function bindEvents() {
  byId("create-request-button").addEventListener("click", openRequestForm);
  byId("request-form").addEventListener("submit", savePendingRequest);
  byId("request-close").addEventListener("click", () => byId("request-dialog").close());
  byId("request-cancel").addEventListener("click", () => byId("request-dialog").close());
  byId("new-work-type").addEventListener("change", applyWorkDefaults);
  byId("new-address").addEventListener("input", updateAddressInput);
  byId("find-address").addEventListener("click", findAddress);
  byId("new-address").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); findAddress(); } });
  byId("request-dialog").addEventListener("close", cancelAddressSearch);
  for (const id of ["new-latitude", "new-longitude"]) byId(id).addEventListener("input", () => {
    cancelAddressSearch(); byId("address-results").hidden = true; byId("new-location").value = "__new__";
    byId("new-location-note").textContent = "Координаты указаны вручную. Проверьте, что они соответствуют адресу.";
  });
  byId("recalculate-button").addEventListener("click", () => recalculatePending().catch((error) => showToast(error.message, true)));
  window.addEventListener("beforeunload", (event) => { if (state.pendingRequests.length) { event.preventDefault(); event.returnValue = ""; } });
  byId("stress-demo-button").addEventListener("click", async () => {
    try {
      const input = await apiJson("/api/demo?size=large");
      await requestPlan(input, { demoSource: "synthetic" });
      state.originalInput = deepClone(input);
      state.query = ""; state.filter = "all"; state.requestPage = 0;
      byId("request-search").value = "";
      document.querySelectorAll(".tab").forEach((tab) => { const active = tab.dataset.filter === "all"; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", active); });
      state.changes = []; renderRequests(); renderChanges();
      showToast("Синтетическое демо: 30 бригад, 300 заявок. Время поездок тестовое.");
    } catch (error) { showToast(error.message, true); }
  });
  byId("map-all").addEventListener("click", () => {
    state.routeThroughRequestId = null;
    state.selectedTechnicianId = null; state.selectedRequestId = null;
    renderRequests(); renderTechnicians(); renderTimeline(); renderDecision(); renderMap();
  });
  byId("map-retry").addEventListener("click", () => { routeGeometryFailures.clear(); renderMap(); });
  byId("timeline-zoom-in").addEventListener("click", () => changeTimelineZoom(1));
  byId("timeline-zoom-out").addEventListener("click", () => changeTimelineZoom(-1));
  byId("timeline-fit").addEventListener("click", () => changeTimelineZoom(0));
  byId("technician-search").addEventListener("input", (event) => { state.technicianQuery = event.target.value; state.technicianPage = 0; renderTechnicians(); });
  byId("technician-filter").addEventListener("change", (event) => { state.technicianFilter = event.target.value; state.technicianPage = 0; renderTechnicians(); });
  for (const [prefix, key, renderList] of [["requests", "requestPage", renderRequests], ["technicians", "technicianPage", renderTechnicians]]) {
    for (const [direction, delta] of [["prev", -1], ["next", 1]]) byId(`${prefix}-${direction}`).addEventListener("click", () => {
      state[key] += delta; renderList();
      byId(prefix === "requests" ? "request-list" : "technician-list").scrollTop = 0;
    });
  }
  byId("map-upload-button").addEventListener("click", () => byId("map-file-input").click());
  byId("map-button").addEventListener("click", async () => { byId("map-dialog").showModal(); await loadMapManagement(); });
  byId("map-dialog").addEventListener("close", () => { clearTimeout(mapStatusTimer); });
  byId("map-update-button").addEventListener("click", startMapUpdate);
  byId("map-file-input").addEventListener("change", (event) => { const [file] = event.target.files; if (file) uploadMap(file); event.target.value = ""; });
  byId("map-schedule-save").addEventListener("click", saveMapSchedule);
  byId("travel-calibration-button").addEventListener("click", openTravelCalibration);
  byId("travel-calibration-close").addEventListener("click", () => byId("travel-calibration-dialog").close());
  byId("travel-calibration-cancel").addEventListener("click", () => byId("travel-calibration-dialog").close());
  byId("travel-calibration-form").addEventListener("submit", saveTravelCalibration);
  byId("calibration-technician").addEventListener("change", updateCalibrationOrigin);
  document.querySelectorAll("[data-travel-time-mode]").forEach((button) => button.addEventListener("click", () => {
    setTravelTimeMode(button.dataset.travelTimeMode === "history");
  }));
  byId("load-button").addEventListener("click", () => byId("file-input").click());
  byId("file-input").addEventListener("change", async (event) => {
    try {
      const file = event.target.files[0];
      if (!file) return;
      const input = JSON.parse(await file.text());
      await requestPlan(input, { demoSource: null });
      state.originalInput = deepClone(input);
      state.changes = [];
      renderChanges();
      showToast(`Загружено: ${file.name}`);
    } catch (error) { showToast(error.message, true); }
    event.target.value = "";
  });
  byId("export-button").addEventListener("click", exportPlan);
  byId("replan-button").addEventListener("click", openDemoEvents);
  byId("demo-go-recalculate").addEventListener("click", () => { byId("replan-dialog").close(); byId("recalculate-button").focus(); });
  byId("apply-scenario").addEventListener("click", async (event) => {
    event.preventDefault();
    const scenario = new FormData(byId("replan-dialog").querySelector("form")).get("scenario");
    byId("replan-dialog").close();
    try { await applyScenario(scenario); } catch (error) { showToast(error.message, true); }
  });
  byId("reset-button").addEventListener("click", async () => {
    const previousMode = state.useTravelHistory;
    state.useTravelHistory = false;
    try {
      await requestPlan(deepClone(state.originalInput));
      state.changes = [];
      renderChanges();
      showToast("Исходный план восстановлен");
    } catch (error) {
      state.useTravelHistory = previousMode;
      showToast(error.message, true);
    }
  });
  byId("request-search").addEventListener("input", (event) => { state.query = event.target.value; state.requestPage = 0; renderRequests(); });
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    state.filter = tab.dataset.filter;
    state.requestPage = 0;
    document.querySelectorAll(".tab").forEach((item) => { const active = item === tab; item.classList.toggle("active", active); item.setAttribute("aria-selected", active); });
    renderRequests();
  }));
}

async function init() {
  bindEvents();
  setBusy(false, "Загрузка данных…");
  loadMapManagement();
  try { await loadDemo(); registerWebMcpTools(); }
  catch (error) { setBusy(false, "Ошибка загрузки"); showToast(error.message, true); }
}

init();
