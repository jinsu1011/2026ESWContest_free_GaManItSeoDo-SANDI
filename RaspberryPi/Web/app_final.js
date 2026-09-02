"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  publication: null,
  socket: null,
  pollTimer: null,
  reconnectTimer: null,
  auxiliaryAt: 0,
  previousLevel: null,
  dangerTransitionId: null,
  busyActions: new Set(),
  transportWarning: false,
  connectionMode: "connecting",
  smsCooldownUntil: 0,
  cooldownTimer: null,
  voiceEnabled: readStorage("safenest.voiceEnabled") !== "false",
  lastAnnouncedTransitionId: readSessionStorage("safenest.lastDangerTransition") || null,
};

const reasonLabels = {
  EMERGENCY_HUMAN_FALL: "Thermal AI가 높은 신뢰도로 낙상을 감지했습니다.",
  EMERGENCY_VERIFIED_APNEA: "검증된 무호흡 신호가 감지되었습니다.",
  APNEA_UNVERIFIED_NO_OVERRIDE: "무호흡 후보가 있으나 검증되지 않아 긴급 승격하지 않았습니다.",
  ABNORMAL_RESPIRATION_RPM: "호흡수가 정상 범위(10–24 rpm)를 벗어났습니다.",
  CO2_RELATIVE_RISE: "로컬라이징 이후 CO₂가 기준값보다 약 500 ppm 올랐습니다.",
  VERY_FAST_CO2_RISE: "CO₂ 기울기가 주의 산식 기준(50 ppm/min) 이상입니다.",
  FAST_CO2_RISE: "CO₂ 농도가 빠르게 상승하고 있습니다.",
  LONG_NO_MOTION: "사람이 확인된 상태에서 3분 이상 움직임이 없습니다.",
  NO_MOTION_DETECTED: "사람이 확인되었지만 현재 움직임이 없습니다.",
  PRESENCE_NOT_CONFIRMED: "사람 존재가 확인되지 않아 무움직임 위험을 누적하지 않습니다.",
  PRESENCE_UNCONFIRMED: "mmWave presence 입력을 사용할 수 없습니다.",
  PRESENCE_FROM_THERMAL: "Thermal AI로 사람 존재를 교차 확인했습니다.",
  PRESENCE_FROM_MMWAVE: "mmWave로 사람 존재를 확인했습니다.",
  MMWAVE_THERMAL_MISMATCH: "mmWave와 Thermal의 사람 존재 판정이 일치하지 않습니다.",
  THERMAL_FALL_PROXY_LIMITED_RISK_NO_EMERGENCY: "Thermal AI가 누운 자세 proxy를 감지해 위험도에 제한 반영했습니다. 실제 낙상 긴급 판정은 아닙니다.",
  ALL_RISK_COMPONENTS_UNAVAILABLE: "사용 가능한 위험도 입력이 없습니다.",
  ALL_SENSORS_FAULT_OR_MISSING: "모든 센서가 결측 또는 고장 상태입니다.",
  ABNORMAL_RESPIRATION_RPM_SUSTAINED: "호흡수 이상이 연속으로 확인되었습니다.",
  RESPIRATION_NORMAL: "호흡수가 정상 범위입니다.",
  RESPIRATION_ABNORMAL: "호흡수가 정상 범위를 벗어났습니다.",
  RESPIRATION_INPUT_UNAVAILABLE: "호흡 입력을 사용할 수 없습니다.",
  APNEA_PROXY_AWAITING_PERSISTENCE: "무호흡 proxy가 지속 조건을 아직 채우지 못했습니다.",
  APNEA_PROXY_UNVERIFIED_NO_EMERGENCY: "무호흡 proxy는 하드웨어 검증 전이라 긴급으로 승격하지 않습니다.",
  CO2_IMMEDIATE_DANGER: "CO₂가 절대 비상 기준(5,000 ppm) 이상입니다.",
  CO2_RELATIVE_WARNING: "CO₂가 기준값 대비 +500 ppm 이상 상승했습니다.",
  CO2_BASELINE_UNLOCKED: "CO₂ 기준값 로컬라이징이 아직 끝나지 않았습니다.",
  CO2_LOCALIZING: "CO₂ 기준값을 측정하는 중입니다.",
  CO2_NORMAL: "CO₂가 정상 범위입니다.",
  CO2_INPUT_UNAVAILABLE: "CO₂ 입력을 사용할 수 없습니다.",
  NO_MOTION_RISING: "무움직임 시간이 누적되고 있습니다.",
  PIR_INPUT_UNAVAILABLE: "PIR 입력을 사용할 수 없습니다.",
  PIR_INVALID_TIMESTAMP: "PIR 타임스탬프가 유효하지 않습니다.",
  PIR_PRESENCE_UNCONFIRMED: "사람 존재가 확인되지 않아 PIR 판단을 보류합니다.",
  THERMAL_FALL_MEDIUM_CONFIDENCE: "Thermal 자세 proxy가 중간 신뢰도로 눕기를 시사합니다.",
  THERMAL_FALL_LOW_CONFIDENCE: "Thermal 자세 proxy가 낮은 신뢰도로 눕기를 시사합니다.",
  INSUFFICIENT_EVIDENCE_FOR_NORMAL: "정상을 선언할 근거가 부족해 판단을 보류합니다.",
  VACANT_DANGER_SUPPRESSED: "사람이 확인되지 않아 위험 단계를 올리지 않았습니다.",
  VACANT_SCORE_DANGER_SUPPRESSED: "사람이 확인되지 않아 점수 기반 위험 단계를 올리지 않았습니다.",
  VACANT_CO2_EMERGENCY_DEMOTED: "사람이 확인되지 않아 CO₂ 비상을 주의로 강등했습니다.",
  FLOOR_CO2_IMMEDIATE_DANGER: "CO₂ 절대 비상 기준으로 위험 단계가 고정되었습니다.",
  FLOOR_CO2_RELATIVE_WARNING: "CO₂ 상대 상승으로 주의 단계가 고정되었습니다.",
  FLOOR_CO2_FAST_RISE: "CO₂ 급상승으로 주의 단계가 고정되었습니다.",
  FLOOR_THERMAL_FALL_CONFIDENT: "Thermal 낙상 신뢰도로 위험 단계가 고정되었습니다.",
  FLOOR_MMWAVE_APNEA_HARDWARE_VERIFIED: "검증된 무호흡으로 위험 단계가 고정되었습니다.",
  FLOOR_MMWAVE_APNEA_PROXY_SUSTAINED: "무호흡 proxy 지속으로 위험 단계가 고정되었습니다.",
  FLOOR_PIR_LONG_NO_MOTION: "장시간 무움직임으로 위험 단계가 고정되었습니다.",
};

const eventLabels = {
  SNAPSHOT_INITIALIZED: "관제 상태 초기화",
  RISK_LEVEL_CHANGED: "위험 단계 변경",
  WARNING_ENTERED: "주의 단계 진입",
  DANGER_ENTERED: "DANGER 진입",
  DANGER_CLEARED: "DANGER 해제",
  NORMAL_RESTORED: "정상 상태 복구",
  SYSTEM_HEALTH_CHANGED: "시스템 건강도 변경",
  EMERGENCY_STARTED: "긴급 경보 시작",
  EMERGENCY_CLEARED: "긴급 경보 해제",
  SENSOR_STATUS_CHANGED: "센서 상태 변경",
  SENSOR_OFFLINE: "센서 오프라인",
  SENSOR_RECOVERED: "센서 복구",
  GATEWAY_OFFLINE: "Gateway 오프라인",
  GATEWAY_ONLINE: "Gateway 온라인",
  GATEWAY_DEGRADED: "Gateway 부분 연결",
  WEBSOCKET_OFFLINE: "WebSocket 오프라인",
  WEBSOCKET_ONLINE: "WebSocket 온라인",
  BUZZER_ACTIVATED: "부저 활성화",
  BUZZER_UNAVAILABLE: "부저 미사용",
  ALARM_ACKNOWLEDGED: "사용자 경고 확인",
  MANAGER_SMS_REQUESTED: "담당자 SMS 요청",
  MANAGER_SMS_SUCCEEDED: "담당자 SMS 전송 성공",
  MANAGER_SMS_FAILED: "담당자 SMS 전송 실패",
  MANAGER_SMS_COOLDOWN_REJECTED: "SMS 재전송 대기",
  RUNTIME_ERROR: "Runtime 오류",
};

const audioFiles = {
  DANGER: "danger.mp3",
  WARNING: "warning.mp3",
  SMS_SUCCESS: "sms_sent.mp3",
  SMS_FAILURE: "sms_failed.mp3",
  SENSOR_OFFLINE: "sensor_offline.mp3",
};

function number(value, digits = 1) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function percent(value) {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
}

function setText(id, value) {
  const element = $(id);
  if (element) element.textContent = value ?? "—";
}

function setStatus(id, status) {
  const value = status || "NO DATA";
  const element = $(id);
  if (!element) return;
  element.textContent = value.replaceAll("_", " ");
  element.dataset.status = value;
}

function valueAt(sensor, key) { return sensor?.state?.values?.[key]; }
function aiAt(sensor) { return sensor?.ai || {}; }
function componentAt(sensor) { return sensor?.risk_component || {}; }

function riskComponentText(sensor) {
  const component = componentAt(sensor);
  if (!component.available) return "UNAVAILABLE";
  return `${number(component.score, 2)} · ${(component.source || "UNKNOWN").toUpperCase()}`;
}

function effectiveLevel(payload) {
  const riskLevel = payload?.risk?.risk_level;
  if (riskLevel) return riskLevel;
  return payload?.emergency?.active ? "DANGER" : "UNKNOWN";
}

function renderOverview(payload) {
  const risk = payload.risk || {};
  const level = effectiveLevel(payload);
  const score = typeof risk.risk_score === "number" ? risk.risk_score : null;
  document.body.dataset.level = level;
  setText("riskLevel", level === "UNKNOWN" ? "NO DATA" : level);
  setText("riskKicker", risk.is_emergency || payload.emergency?.active
    ? "즉시 현장 확인 필요"
    : level === "UNKNOWN" ? "데이터 수신 대기" : "실시간 융합 위험도");
  const summary = {
    NORMAL: "현재 사용 가능한 센서 기준으로 즉시 대응이 필요한 위험은 없습니다.",
    WARNING: "주의 신호가 감지되었습니다. 원인 센서와 현장 상태를 확인하세요.",
    DANGER: payload.offline
      ? "마지막 위험 판정이 유지되고 있습니다. 센서 연결이 복구될 때까지 현장 대응을 계속하세요."
      : "위험 신호가 확인되었습니다. 즉시 현장 대응 절차를 시작하세요.",
    UNKNOWN: "유효한 센서 입력이 부족해 위험도를 계산할 수 없습니다.",
  }[level];
  setText("riskSummary", summary);
  setText("riskScore", score === null ? "—" : Math.round(score));
  $("scoreGauge")?.style.setProperty("--score", score === null ? 0 : Math.max(0, Math.min(100, score)));
  $("scoreGauge")?.setAttribute("aria-label", score === null ? "위험 점수 없음" : `위험 점수 ${score.toFixed(1)}점`);
  setText("gatewayState", payload.system || "OFFLINE");
  setText("stateRevision", payload.revision ?? "—");
  setText("lastUpdate", formatTime(payload.timestamp));
  const health = payload.system_health || "FAILED";
  setText("healthBadge", health);
  $("healthBadge").dataset.health = health;
  renderReasons(risk.reasons || []);
}

function renderReasons(reasons) {
  const list = $("reasonList");
  if (!list) return;
  list.replaceChildren();
  const unique = [...new Set(reasons)];
  setText("reasonCount", unique.length);
  if (!unique.length) {
    const item = document.createElement("li");
    item.className = "empty-state";
    item.textContent = "현재 수집된 판단 근거가 없습니다.";
    list.append(item);
    return;
  }
  unique.slice(0, 8).forEach((reason) => {
    const item = document.createElement("li");
    item.textContent = reasonLabels[reason] || String(reason).replaceAll("_", " ");
    if (String(reason).startsWith("EMERGENCY") || String(reason).includes("DANGER")) item.className = "critical";
    list.append(item);
  });
}

function renderSensors(payload) {
  const mmwave = payload.mmwave || {};
  setStatus("mmwaveStatus", mmwave.state?.status);
  setText("respirationValue", number(valueAt(mmwave, "respiration_rate_bpm"), 1));
  setText("heartValue", number(valueAt(mmwave, "heart_rate_bpm"), 1));
  const presenceAvailable = valueAt(mmwave, "presence_available") === true;
  const fusedPresence = payload.risk?.presence_detected;
  const fusedSource = payload.risk?.presence_source;
  setText("presenceValue", presenceAvailable
    ? (valueAt(mmwave, "presence") ? "감지 · mmWave" : "미감지 · mmWave")
    : typeof fusedPresence === "boolean" && fusedSource !== "UNCONFIRMED"
      ? `${fusedPresence ? "감지" : "미감지"} · ${fusedSource}`
      : "미제공");
  setText("mmwaveAi", aiAt(mmwave).available ? aiAt(mmwave).state : (aiAt(mmwave).error || "INPUT UNAVAILABLE"));
  setText("mmwaveRisk", riskComponentText(mmwave));

  const thermal = payload.thermal || {};
  const thermalAi = aiAt(thermal);
  setStatus("thermalStatus", thermal.state?.status);
  setText("thermalAi", thermalAi.available ? thermalAi.state : (thermalAi.error || "UNAVAILABLE"));
  const probabilities = thermalAi.metadata?.probabilities;
  const human = Array.isArray(probabilities) && probabilities.length === 3 ? probabilities[1] + probabilities[2] : null;
  setText("humanProbability", percent(human));
  const rawMin = thermalAi.metadata?.raw_minimum ?? valueAt(thermal, "minimum_raw");
  const rawMax = thermalAi.metadata?.raw_maximum ?? valueAt(thermal, "maximum_raw");
  setText("thermalRawRange", typeof rawMin === "number" && typeof rawMax === "number" ? `${rawMin} – ${rawMax}` : "—");
  setText("thermalRisk", riskComponentText(thermal));
  renderHeatmap(thermalAi.metadata?.heatmap_preview);

  const co2 = payload.co2 || {};
  const ppm = valueAt(co2, "ppm");
  setStatus("co2Status", co2.state?.status);
  setText("co2Value", number(ppm, 0));
  const co2Component = componentAt(co2);
  setText("co2State", co2Component.state || "—");
  setText("co2Risk", riskComponentText(co2));
  const co2Progress = typeof ppm === "number" ? Math.max(0, Math.min(100, ppm / 3000 * 100)) : 0;
  $("co2Track").style.width = `${co2Progress}%`;
  $("co2Track").style.background = ppm >= 5000 ? "var(--red)" : ppm >= 1000 ? "var(--yellow)" : "var(--cyan)";

  const pir = payload.pir || {};
  const motion = valueAt(pir, "motion");
  const pirComponent = componentAt(pir);
  setStatus("pirStatus", pir.state?.status);
  $("motionVisual").dataset.motion = typeof motion === "boolean" ? String(motion) : "unknown";
  setText("motionValue", typeof motion === "boolean" ? (motion ? "움직임 감지" : "움직임 없음") : "확인 불가");
  setText("pirRule", pirComponent.state || aiAt(pir).state || "—");
  const noMotion = pirComponent.metadata?.no_motion_seconds;
  setText("noMotionTime", typeof noMotion === "number" ? `${noMotion.toFixed(1)}초` : "—");
  setText("pirRisk", riskComponentText(pir));
}

function renderEmergency(payload) {
  const level = effectiveLevel(payload);
  const emergency = payload.emergency || {};
  const active = level === "DANGER" || emergency.active === true;
  const overlay = $("emergencyOverlay");
  if (!overlay) return;
  overlay.hidden = !active;
  if (!active) return;

  const risk = payload.risk || {};
  const mmwave = payload.mmwave || {};
  const thermal = payload.thermal || {};
  const pir = payload.pir || {};
  const reasons = [...new Set(risk.reasons || [])];
  setText("emergencyRiskLevel", "DANGER");
  setText("emergencyPill", payload.offline ? "DANGER · DATA OFFLINE" : "DANGER");
  setText("emergencyRiskScore", typeof risk.risk_score === "number" ? Math.round(risk.risk_score) : "—");
  setText("emergencyEnteredAt", emergency.entered_at ? `진입 시각 ${formatTime(emergency.entered_at)}` : "진입 시각 —");
  setText("emergencyRespiration", `${number(valueAt(mmwave, "respiration_rate_bpm"), 1)} rpm`);
  setText("emergencyCo2", `${number(valueAt(payload.co2 || {}, "ppm"), 0)} ppm`);
  const noMotion = componentAt(pir).metadata?.no_motion_seconds;
  setText("emergencyNoMotion", typeof noMotion === "number" ? `${noMotion.toFixed(1)}초` : "—");
  setText("emergencyThermal", aiAt(thermal).state || componentAt(thermal).state || "—");

  const reasonList = $("emergencyReasonList");
  reasonList.replaceChildren();
  (reasons.length ? reasons : ["DANGER 상태가 Risk Engine에서 전달되었습니다."]).slice(0, 6).forEach((reason) => {
    const item = document.createElement("li");
    item.textContent = reasonLabels[reason] || String(reason).replaceAll("_", " ");
    reasonList.append(item);
  });

  const buzzer = emergency.buzzer || {};
  setText("emergencyBuzzerMode", buzzer.mode ? `부저 ${buzzer.mode}` : "No Data");
  const acknowledged = emergency.acknowledged === true;
  setText("emergencyAlarmState", acknowledged ? "사용자 경고 확인됨 · DANGER 유지" : (emergency.buzzer_active ? "부저 작동 중" : "부저 확인 필요"));
  const ackButton = $("acknowledgeButton");
  ackButton.disabled = acknowledged || isBusy("acknowledge");
  ackButton.textContent = acknowledged ? "✓ 경고 확인됨" : "✓ 경고 확인";
  $("emergencyOfflineNote").hidden = !(payload.offline || state.transportWarning || emergency.latched_while_offline);
  updateVoiceButton();
  updateSmsButton();
}

function renderOffline(payload) {
  const offline = payload.offline === true || payload.system !== "ONLINE" || state.connectionMode === "offline";
  const banner = $("offlineBanner");
  if (!banner) return;
  banner.hidden = !offline && !state.transportWarning;
  if (!banner.hidden) {
    const message = state.connectionMode === "offline"
      ? "WebSocket와 polling 모두 연결되지 않았습니다. 마지막 값은 현재값으로 간주하지 마십시오."
      : state.transportWarning
        ? "WebSocket이 끊겨 polling으로 전환했습니다. 센서 데이터 freshness를 확인하십시오."
        : "현재 센서 데이터를 수신하지 못하고 있습니다. 센서 및 Gateway 연결 상태를 확인하십시오.";
    setText("offlineMessage", message);
  }
}

function render(payload) {
  const level = effectiveLevel(payload);
  const transitionId = payload.emergency?.transition_id || `publication-${payload.publication_revision || payload.revision || "unknown"}`;
  const enteredDanger = level === "DANGER" && (state.previousLevel !== "DANGER" || state.dangerTransitionId !== transitionId);
  state.publication = payload;
  if (enteredDanger) onDangerEntered(payload, transitionId);
  state.previousLevel = level;
  if (level === "DANGER") state.dangerTransitionId = transitionId;
  renderOverview(payload);
  renderSensors(payload);
  renderEmergency(payload);
  renderOffline(payload);
  const now = Date.now();
  if (now - state.auxiliaryAt > 4500) {
    state.auxiliaryAt = now;
    refreshAuxiliary();
  }
}

function onDangerEntered(payload, transitionId) {
  state.dangerTransitionId = transitionId;
  setActionStatus("긴급 상황입니다. 위험 원인을 확인하고 대응 버튼을 선택하십시오.", "critical");
  if (state.lastAnnouncedTransitionId !== transitionId) {
    state.lastAnnouncedTransitionId = transitionId;
    writeSessionStorage("safenest.lastDangerTransition", transitionId);
    playVoice("DANGER");
    recordVoice("DANGER");
  }
  if (payload.emergency?.acknowledged) setActionStatus("이전 경고 확인 상태를 복원했습니다. DANGER는 유지됩니다.", "warning");
}

function renderHeatmap(preview) {
  const canvas = $("thermalCanvas");
  if (!canvas) return;
  const context = canvas.getContext("2d");
  context.fillStyle = "#071018";
  context.fillRect(0, 0, canvas.width, canvas.height);
  const valid = preview && Number.isInteger(preview.width) && Number.isInteger(preview.height) &&
    Array.isArray(preview.values) && preview.values.length === preview.width * preview.height;
  if (!valid) {
    context.strokeStyle = "#162b37";
    for (let x = 0; x < canvas.width; x += 25) { context.beginPath(); context.moveTo(x, 0); context.lineTo(x, canvas.height); context.stroke(); }
    for (let y = 0; y < canvas.height; y += 25) { context.beginPath(); context.moveTo(0, y); context.lineTo(canvas.width, y); context.stroke(); }
    setText("heatmapNote", "FRAME UNAVAILABLE");
    canvas.setAttribute("aria-label", "Thermal frame을 사용할 수 없습니다");
    return;
  }
  const cellWidth = canvas.width / preview.width;
  const cellHeight = canvas.height / preview.height;
  preview.values.forEach((rawValue, index) => {
    const value = Math.max(0, Math.min(1, Number(rawValue) || 0));
    const hue = 245 - value * 245;
    const lightness = 24 + value * 34;
    context.fillStyle = `hsl(${hue} 88% ${lightness}%)`;
    context.fillRect((index % preview.width) * cellWidth, Math.floor(index / preview.width) * cellHeight, cellWidth + .5, cellHeight + .5);
  });
  setText("heatmapNote", `${preview.source_width}×${preview.source_height} RAW · NORMALIZED PREVIEW`);
  canvas.setAttribute("aria-label", `${preview.source_width} 곱하기 ${preview.source_height} Thermal frame 정규화 미리보기`);
}

async function refreshAuxiliary() {
  await Promise.allSettled([loadEvents(), loadHistory()]);
}

async function loadEvents() {
  const response = await fetch("/api/events?limit=12", { cache: "no-store" });
  if (!response.ok) throw new Error("events unavailable");
  const payload = await response.json();
  setText("storageBadge", (payload.persistence || "memory").toUpperCase());
  const list = $("eventList");
  list.replaceChildren();
  const events = (payload.events || []).filter(event => !String(event.event_type || "").includes("SIMULATION"));
  if (!events.length) {
    const item = document.createElement("li"); item.className = "empty-state"; item.textContent = "기록된 이벤트가 없습니다."; list.append(item); return;
  }
  events.forEach((event) => {
    const item = document.createElement("li");
    const time = document.createElement("time"); time.textContent = formatTime(event.timestamp); time.dateTime = new Date(event.timestamp * 1000).toISOString();
    const content = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = eventLabels[event.event_type] || event.event_type;
    const detail = document.createElement("span"); detail.textContent = eventDetail(event);
    content.append(title, detail); item.append(time, content); list.append(item);
  });
}

function eventDetail(event) {
  const details = event.details || {};
  if (details.sensor_id) return `${details.sensor_id} · ${details.from ?? "—"} → ${details.to ?? "—"}`;
  if ("from" in details || "to" in details) return `${details.from ?? "—"} → ${details.to ?? "—"}`;
  if (details.source) return `${details.source} · ${details.detail || details.message || "상태 변경"}`;
  if (details.manager_phone_masked) return `담당자 ${details.manager_phone_masked}`;
  return Object.entries(details).map(([key, value]) => `${key}: ${value}`).join(" · ") || "상세 정보 없음";
}

async function loadHistory() {
  const response = await fetch("/api/history?limit=60", { cache: "no-store" });
  if (!response.ok) throw new Error("history unavailable");
  const payload = await response.json();
  drawTrend([...(payload.history || [])].reverse());
}

function drawTrend(history) {
  const canvas = $("trendCanvas");
  if (!canvas) return;
  const context = canvas.getContext("2d");
  const width = canvas.width, height = canvas.height, pad = 34;
  context.clearRect(0, 0, width, height);
  context.strokeStyle = "#203746"; context.lineWidth = 1;
  for (let row = 0; row <= 4; row++) {
    const y = pad + (height - pad * 2) * row / 4;
    context.beginPath(); context.moveTo(pad, y); context.lineTo(width - pad, y); context.stroke();
  }
  const usable = history.filter((item) => typeof item.timestamp === "number");
  $("trendEmpty").hidden = usable.length > 1;
  if (usable.length <= 1) { canvas.setAttribute("aria-label", "최근 기록이 부족합니다"); return; }
  const plot = (getter, max, color) => {
    context.beginPath(); context.strokeStyle = color; context.lineWidth = 3; context.lineJoin = "round";
    let started = false;
    usable.forEach((item, index) => {
      const value = getter(item);
      if (typeof value !== "number" || !Number.isFinite(value)) { started = false; return; }
      const x = pad + (width - pad * 2) * index / Math.max(1, usable.length - 1);
      const y = height - pad - Math.max(0, Math.min(1, value / max)) * (height - pad * 2);
      if (!started) { context.moveTo(x, y); started = true; } else context.lineTo(x, y);
    });
    context.stroke();
  };
  plot((item) => item.risk_score ?? item.risk?.risk_score, 100, "#ff5d6c");
  plot((item) => item.co2_ppm ?? item.state?.sensors?.co2?.values?.ppm, 3000, "#38d5e6");
  canvas.setAttribute("aria-label", `최근 ${usable.length}개 기록의 위험도와 CO₂ 추이`);
}

async function postJson(path, body = {}) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
  } catch (_error) {
    return { ok: false, status: 0, message: "백엔드와 연결되지 않았습니다. 잠시 후 다시 시도하십시오." };
  }
  let payload = {};
  try { payload = await response.json(); } catch (_error) { payload = {}; }
  return { ok: response.ok, status: response.status, ...payload };
}

function isBusy(action) { return state.busyActions.has(action); }

function setBusy(action, busy, buttonId) {
  if (busy) state.busyActions.add(action); else state.busyActions.delete(action);
  const button = $(buttonId);
  if (button) button.disabled = busy;
}

function setActionStatus(message, tone = "") {
  const status = $("emergencyActionStatus");
  if (!status) return;
  status.dataset.tone = tone;
  setText("smsStatus", message);
}

function updateVoiceButton() {
  const button = $("voiceToggleButton");
  if (!button) return;
  button.textContent = state.voiceEnabled ? "🔊 음성 ON" : "🔇 음성 OFF";
  button.setAttribute("aria-pressed", String(state.voiceEnabled));
  button.dataset.enabled = String(state.voiceEnabled);
}

function updateSmsButton() {
  const button = $("contactManagerButton");
  if (!button) return;
  const remaining = Math.max(0, state.smsCooldownUntil - Date.now());
  if (remaining > 0) {
    button.disabled = true;
    button.textContent = `📱 담당자 연락 (${Math.ceil(remaining / 1000)}초 후)`;
  } else if (!isBusy("sms")) {
    button.disabled = false;
    button.textContent = "📱 담당자 연락";
  }
}

async function contactManager() {
  if (isBusy("sms") || Date.now() < state.smsCooldownUntil) return;
  setBusy("sms", true, "contactManagerButton");
  setText("smsStatus", "담당자에게 긴급 알림 전송 중...");
  const response = await postJson("/api/emergency/contact", {
    idempotency_key: `manager-${state.dangerTransitionId || "danger"}-${Date.now()}`,
  });
  if (response.ok) {
    state.smsCooldownUntil = Date.now() + Number(response.cooldown_seconds || 60) * 1000;
    setText("smsStatus", `✓ ${response.manager?.name || "안전 담당자"}에게 긴급 알림을 전송했습니다. ${response.manager?.phone_masked || ""}`);
    setActionStatus("안전 담당자에게 긴급 알림을 전송했습니다.", "success");
    playVoice("SMS_SUCCESS");
    recordVoice("SMS_SUCCESS");
  } else {
    const retry = response.retry_after_seconds ? ` ${Math.ceil(response.retry_after_seconds)}초 후 재시도하십시오.` : "";
    setText("smsStatus", `⚠ 담당자 긴급 알림 전송에 실패했습니다.${retry}`);
    setActionStatus(response.message || "네트워크 또는 SMS 서비스 상태를 확인하십시오.", "error");
    playVoice("SMS_FAILURE");
    recordVoice("SMS_FAILURE");
    if (response.retry_after_seconds) state.smsCooldownUntil = Date.now() + Number(response.retry_after_seconds) * 1000;
  }
  setBusy("sms", false, "contactManagerButton");
  updateSmsButton();
  refreshAuxiliary();
}

async function acknowledgeAlarm() {
  if (isBusy("acknowledge") || $("acknowledgeButton").disabled) return;
  setBusy("acknowledge", true, "acknowledgeButton");
  const response = await postJson("/api/emergency/acknowledge");
  if (response.ok) {
    if (state.publication) state.publication.emergency = response.emergency;
    setActionStatus("경고를 확인했습니다. 위험 단계는 Risk Engine이 낮출 때까지 DANGER로 유지됩니다.", "warning");
    renderEmergency(state.publication || {});
    refreshAuxiliary();
  } else {
    setActionStatus(response.message || "경고 확인에 실패했습니다.", "error");
  }
  setBusy("acknowledge", false, "acknowledgeButton");
}

function toggleVoice() {
  state.voiceEnabled = !state.voiceEnabled;
  writeStorage("safenest.voiceEnabled", String(state.voiceEnabled));
  updateVoiceButton();
  recordVoice(state.voiceEnabled ? "UNMUTED" : "MUTED");
  setActionStatus(state.voiceEnabled ? "음성 안내를 켰습니다." : "음성 안내를 껐습니다. 위험 상태에는 영향을 주지 않습니다.", "warning");
}

function playVoice(action) {
  if (!state.voiceEnabled) return;
  const filename = audioFiles[action];
  if (!filename) return;
  const audio = new Audio(`/dashboard/assets/audio/${filename}`);
  audio.volume = .95;
  const result = audio.play();
  if (result && typeof result.catch === "function") {
    result.catch(() => setActionStatus("음성 파일이 없거나 브라우저 autoplay가 차단되었습니다. 화면 조작은 계속 가능합니다.", "warning"));
  }
}

function recordVoice(action) {
  postJson("/api/emergency/voice", { action }).catch(() => {});
}

function setConnection(mode, label) {
  state.connectionMode = mode;
  $("connectionBadge").dataset.state = mode;
  $("connectionBadge").querySelector("span").textContent = label;
  renderOffline(state.publication || { system: mode === "offline" ? "OFFLINE" : "ONLINE" });
}

function notifyConnection(source, status) {
  postJson("/api/client-connection", { source, status }).catch(() => {});
}

function connect() {
  clearTimeout(state.reconnectTimer);
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws`);
  state.socket = socket;
  setConnection("connecting", "연결 중");
  socket.addEventListener("open", () => {
    stopPolling();
    state.transportWarning = false;
    setConnection("live", "실시간 연결");
    notifyConnection("websocket", "online");
  });
  socket.addEventListener("message", (event) => {
    try { render(JSON.parse(event.data)); } catch (_error) { setConnection("polling", "데이터 오류"); }
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    state.transportWarning = true;
    setConnection("polling", "Polling 전환");
    notifyConnection("websocket", "offline");
    startPolling();
    state.reconnectTimer = setTimeout(connect, 2500);
  });
  socket.addEventListener("error", () => socket.close());
}

function startPolling() {
  if (state.pollTimer) return;
  poll(); state.pollTimer = setInterval(poll, 2000);
}

function stopPolling() { clearInterval(state.pollTimer); state.pollTimer = null; }

async function poll() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("status unavailable");
    render(await response.json());
    state.transportWarning = false;
    setConnection("polling", "Polling 연결");
    notifyConnection("polling", "online");
  } catch (_error) {
    state.transportWarning = true;
    render({ system: "OFFLINE", risk: { risk_level: "UNKNOWN", reasons: [] } });
    setConnection("offline", "연결 끊김");
    notifyConnection("polling", "offline");
  }
}

function formatTime(unixSeconds) {
  if (typeof unixSeconds !== "number" || !Number.isFinite(unixSeconds)) return "—";
  return new Intl.DateTimeFormat("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(unixSeconds * 1000));
}

function readStorage(key) { try { return localStorage.getItem(key); } catch (_error) { return null; } }
function writeStorage(key, value) { try { localStorage.setItem(key, value); } catch (_error) {} }
function readSessionStorage(key) { try { return sessionStorage.getItem(key); } catch (_error) { return null; } }
function writeSessionStorage(key, value) { try { sessionStorage.setItem(key, value); } catch (_error) {} }

function tickClock() {
  const now = new Date();
  $("currentTime").dateTime = now.toISOString();
  $("currentTime").textContent = now.toLocaleTimeString("ko-KR", { hour12: false });
  updateSmsButton();
}

$("contactManagerButton").addEventListener("click", contactManager);
$("acknowledgeButton").addEventListener("click", acknowledgeAlarm);
$("voiceToggleButton").addEventListener("click", toggleVoice);

updateVoiceButton();
state.cooldownTimer = setInterval(updateSmsButton, 1000);
tickClock();
setInterval(tickClock, 1000);
renderHeatmap(null);
startPolling();
connect();
