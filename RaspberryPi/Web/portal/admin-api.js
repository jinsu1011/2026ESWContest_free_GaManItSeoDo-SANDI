(() => {
  "use strict";

  let token = sessionStorage.getItem("safenest-token") || "";
  let spaces = [];
  let events = [];
  let currentSpaceId = "";
  let socket = null;
  let chart = null;

  const $ = id => document.getElementById(id);
  const text = (id, value) => { const el = $(id); if (el) el.textContent = value ?? "No Data"; };
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
  const statusMeta = {
    "normal-empty": ["정상 · 미재실", "ok"],
    "normal-occupied": ["정상 · 재실", "ok"],
    warning: ["주의", "warn"], danger: ["위험", "danger"],
    emergency: ["긴급", "emergency"], offline: ["통신 오류", "offline"]
  };

  function showToast(message) {
    text("toast", message);
    $("toast").classList.add("show");
    setTimeout(() => $("toast").classList.remove("show"), 2400);
  }

  function setAuthenticated(ok) {
    document.body.classList.toggle("auth-pending", !ok);
    if (!ok) setTimeout(() => $("adminLoginId").focus(), 0);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      cache: "no-store",
      headers: {
        ...(token ? {Authorization: `Bearer ${token}`} : {}),
        ...(options.body ? {"Content-Type": "application/json"} : {}),
        ...options.headers
      }
    });
    const data = response.status === 204 ? null : await response.json();
    if (response.status === 401) {
      token = "";
      sessionStorage.removeItem("safenest-token");
      setAuthenticated(false);
    }
    if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
    return data;
  }

  function currentSpace() { return spaces.find(space => space.id === currentSpaceId) || spaces[0] || null; }
  function value(value, suffix = "", digits = 0) {
    return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(digits)}${suffix}` : "No Data";
  }
  function statusBadge(status) {
    const [label, cls] = statusMeta[status] || ["No Data", "offline"];
    return `<span class="badge ${cls}">${label}</span>`;
  }
  function eventBadge(level) {
    const labels = {normal:"정상", info:"정보", warning:"주의", danger:"위험", emergency:"긴급", offline:"통신 오류"};
    const cls = {normal:"ok", info:"ok", warning:"warn", danger:"danger", emergency:"emergency", offline:"offline"}[level] || "offline";
    return `<span class="badge ${cls}">${labels[level] || "No Data"}</span>`;
  }

  function switchView(view) {
    document.querySelectorAll("[data-view-panel]").forEach(panel => panel.classList.toggle("active", panel.dataset.viewPanel === view));
    document.querySelectorAll("nav [data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === view));
    const meta = {
      dashboard:["밀폐공간 통합 안전관제", "현재 공간의 센서 융합 상태를 실시간으로 확인합니다."],
      spaces:["공간 관리", "등록된 SafeNest 공간의 백엔드 수신 상태입니다."],
      events:["이벤트 로그", "백엔드에 기록된 실제 이벤트입니다."],
      settings:["설정", "Raspberry Pi 연결 및 공간 정보를 관리합니다."]
    }[view];
    text("pageTitle", meta[0]); text("pageDescription", meta[1]);
    if (view === "spaces") renderSpaces();
    if (view === "events") renderEvents();
    if (view === "settings") renderSettings();
  }

  function renderDashboard() {
    const space = currentSpace();
    document.body.dataset.spaceId = space?.id || "";
    document.querySelectorAll(".current-room-name").forEach(el => { el.textContent = space?.name || "No Data"; });
    text("globalConnection", space ? space.name : "Waiting for Data");
    const reading = space?.reading || {};
    const connected = space?.bridge?.fresh === true;
    const status = connected ? space.status : "offline";
    const [statusLabel] = statusMeta[status] || ["No Data"];
    text("presenceText", connected && typeof reading.occupied === "boolean" ? (reading.occupied ? "작업자 감지" : "인체 미감지") : "No Data");
    text("presenceSub", connected ? `Raspberry Pi · ${space.bridge?.deviceId || space.nodeId || "장치 ID 없음"}` : "Waiting for Data");
    text("riskScore", connected && typeof space?.risk === "number" ? `${space.risk}/100` : "No Data");
    text("riskBadge", statusLabel);
    text("breathValue", value(reading.breathRate, " rpm", 1));
    text("breathFoot", reading.breathRate == null ? "mmWave 데이터 없음" : "mmWave 실시간 수신");
    text("heartValue", value(reading.heartRate, " bpm", 1));
    text("heartFoot", reading.heartRate == null ? "mmWave 데이터 없음" : "mmWave 실시간 수신");
    const thermalMax = reading.thermal?.fresh ? reading.thermal.maxC : reading.bodyTemperature;
    text("tempLabel", "열화상 최고온도"); text("tempValue", value(thermalMax, "℃", 1));
    text("tempFoot", reading.thermal?.fresh ? "80×62 실시간 프레임" : "열화상 프레임 대기");
    text("co2Value", value(reading.co2, " ppm", 0));
    text("co2Foot", reading.co2 == null ? "CO₂ 데이터 없음" : "CO₂ 실시간 수신");
    text("motionValue", connected && typeof reading.motion === "boolean" ? (reading.motion ? "감지됨" : "미감지") : "No Data");
    text("motionFoot", connected && typeof reading.motion === "boolean" ? "PIR 실시간 수신" : "PIR 데이터 없음");
    text("fusionMmwave", reading.breathRate == null ? "No Data" : "호흡·심박 수신");
    text("fusionThermal", reading.thermal?.fresh ? "실시간 프레임" : "No Data");
    text("fusionMotion", connected && typeof reading.motion === "boolean" ? (reading.motion ? "최근 감지" : "미감지") : "No Data");
    text("fusionCo2", reading.co2 == null ? "No Data" : "실시간 수신");
    text("confidenceValue", connected ? "LIVE" : "No Data");
    text("confidenceText", connected ? "ESP32 → Raspberry Pi → SafeNest Web" : "Raspberry Pi 데이터 수신 대기");
    ["breathCard", "heartCard", "tempCard", "co2Card", "motionCard"].forEach(id => {
      const el = $(id); el.classList.remove("warning", "danger", "offline");
      if (!connected) el.classList.add("offline");
      else if (status === "warning") el.classList.add("warning");
      else if (["danger", "emergency"].includes(status)) el.classList.add("danger");
    });
    const banner = $("alertBanner");
    banner.className = "alert-banner";
    if (["warning", "danger", "emergency", "offline"].includes(status)) {
      banner.classList.add("show", status === "warning" ? "warning" : status === "offline" ? "offline" : "danger");
      text("alertTitle", status === "offline" ? "Connection Error" : `${statusLabel} 상태가 백엔드에서 수신되었습니다.`);
      text("alertDetail", status === "offline" ? "Raspberry Pi 및 센서 연결을 확인하세요." : "백엔드 위험 판단과 현장 상태를 확인하세요.");
    }
  }

  function renderRecentEvents() {
    $("recentEventTable").innerHTML = events.slice(0, 3).map(event => `<tr><td>${escapeHtml(event.time)}</td><td>${eventBadge(event.level)}</td><td>${escapeHtml(event.message)}</td></tr>`).join("");
    if (!events.length) $("recentEventTable").innerHTML = '<tr><td colspan="3">Waiting for Data</td></tr>';
  }

  function renderSpaces() {
    const counts = {
      total: spaces.length,
      normal: spaces.filter(s => String(s.status).startsWith("normal")).length,
      warning: spaces.filter(s => s.status === "warning").length,
      danger: spaces.filter(s => ["danger", "emergency"].includes(s.status)).length,
      offline: spaces.filter(s => s.status === "offline").length
    };
    $("spaceSummary").innerHTML = [["전체 공간",counts.total],["정상",counts.normal],["주의",counts.warning],["위험·긴급",counts.danger],["통신 오류",counts.offline]].map(([label,v]) => `<article class="card summary-card"><span>${label}</span><strong>${v}</strong></article>`).join("");
    $("spaceGrid").innerHTML = spaces.map(space => {
      const reading = space.reading || {};
      const live = space.bridge?.fresh === true;
      return `<article class="card space-card"><div class="space-card-head"><div><h3>${escapeHtml(space.name)}</h3><div class="node">${escapeHtml(space.nodeId)} · ${escapeHtml(space.host)}:${escapeHtml(space.port)}</div></div>${statusBadge(space.status)}</div><div class="space-score"><div class="space-score-row"><div><span>위험점수</span><strong>${live && typeof space.risk === "number" ? `${space.risk}/100` : "No Data"}</strong></div></div></div><div class="space-metrics"><div class="space-metric"><span>CO₂</span><strong>${live ? value(reading.co2," ppm") : "No Data"}</strong></div><div class="space-metric"><span>온도</span><strong>${live ? value(reading.thermal?.maxC ?? reading.bodyTemperature,"℃",1) : "No Data"}</strong></div><div class="space-metric"><span>움직임</span><strong>${live && typeof reading.motion === "boolean" ? (reading.motion ? "감지됨" : "미감지") : "No Data"}</strong></div></div><div class="space-foot"><span>${live && space.lastSeen ? `마지막 수신 ${escapeHtml(new Date(space.lastSeen).toLocaleTimeString("ko-KR"))}` : "Waiting for Data"}</span><button class="secondary-button" data-open-space="${escapeHtml(space.id)}" type="button">상세 보기</button></div></article>`;
    }).join("") || '<div class="empty-state">Waiting for Data</div>';
    document.querySelectorAll("[data-open-space]").forEach(button => button.onclick = () => { currentSpaceId = button.dataset.openSpace; renderDashboard(); switchView("dashboard"); });
  }

  function populateEventSpaces() {
    const select = $("eventSpaceFilter");
    const selected = select.value;
    select.innerHTML = '<option value="all">전체 공간</option>' + spaces.map(space => `<option value="${escapeHtml(space.id)}">${escapeHtml(space.name)}</option>`).join("");
    if ([...select.options].some(option => option.value === selected)) select.value = selected;
  }
  function renderEvents() {
    populateEventSpaces();
    const spaceId = $("eventSpaceFilter").value, level = $("eventLevelFilter").value, query = $("eventSearch").value.trim().toLowerCase();
    const filtered = events.filter(event => (spaceId === "all" || event.spaceId === spaceId) && (level === "all" || event.level === level) && (!query || `${event.message} ${event.detail}`.toLowerCase().includes(query)));
    text("eventCount", `조회 ${filtered.length}건 · 전체 ${events.length}건`);
    $("allEventTable").innerHTML = filtered.map(event => `<tr><td>${escapeHtml(event.time)}</td><td>${escapeHtml(spaces.find(s => s.id === event.spaceId)?.name || event.spaceId || "No Data")}</td><td>${eventBadge(event.level)}</td><td>${escapeHtml(event.message)}</td><td>${escapeHtml(event.detail)}</td></tr>`).join("");
    $("eventEmpty").hidden = filtered.length > 0;
  }

  function renderSettings() {
    $("renameSpaceSelect").innerHTML = spaces.map(space => `<option value="${escapeHtml(space.id)}">${escapeHtml(space.name)}</option>`).join("");
    $("renameSpaceSelect").value = currentSpaceId || spaces[0]?.id || "";
    syncRename();
    $("registeredSpaceTable").innerHTML = spaces.map(space => `<tr><td>${escapeHtml(space.name)}</td><td>${escapeHtml(space.nodeId)}</td><td>${escapeHtml(space.host)}:${escapeHtml(space.port)}</td><td>${statusBadge(space.status)}</td><td><a class="secondary-button" href="/api/qr/${encodeURIComponent(space.id)}.png" target="_blank" rel="noopener">QR 열기</a> <button class="danger-button" data-remove-space="${escapeHtml(space.id)}" type="button">삭제</button></td></tr>`).join("");
    document.querySelectorAll("[data-remove-space]").forEach(button => button.onclick = () => removeSpace(button.dataset.removeSpace));
  }
  function syncRename() { text("renameSpaceInput", ""); $("renameSpaceInput").value = spaces.find(s => s.id === $("renameSpaceSelect").value)?.name || ""; }

  function updateChart(history) {
    if (!window.Chart) return;
    const rows = [...history].reverse();
    const labels = rows.map(row => row.timestamp ? new Date(row.timestamp * 1000).toLocaleTimeString("ko-KR") : "No Data");
    const risk = rows.map(row => row.risk_score ?? row.risk?.risk_score ?? null);
    const co2 = rows.map(row => row.co2_ppm ?? row.state?.sensors?.co2?.values?.ppm ?? null);
    if (!chart) chart = new Chart($("trendChart"), {type:"line", data:{labels:[],datasets:[{label:"위험점수",data:[],tension:.35},{label:"CO₂(ppm)",data:[],tension:.35}]}, options:{responsive:true,maintainAspectRatio:false,interaction:{mode:"index",intersect:false},plugins:{legend:{position:"bottom"}}}});
    chart.data.labels = labels; chart.data.datasets[0].data = risk; chart.data.datasets[1].data = co2; chart.update();
    text("trendStatus", rows.length ? `백엔드 이력 ${rows.length}건` : "Waiting for Data");
  }

  async function reload() {
    const [remoteSpaces, remoteEvents, history] = await Promise.all([api("/api/spaces"), api("/api/portal/events"), fetch("/api/history?limit=60", {cache:"no-store"}).then(r => r.ok ? r.json() : {history:[]})]);
    spaces = Array.isArray(remoteSpaces) ? remoteSpaces : [];
    events = Array.isArray(remoteEvents) ? remoteEvents.filter(e => !String(e.message || "").includes("SIMULATION")).map(e => ({...e, time:e.time ? new Date(e.time).toLocaleString("sv-SE") : "No Data"})) : [];
    if (!spaces.some(space => space.id === currentSpaceId)) currentSpaceId = spaces[0]?.id || "";
    renderDashboard(); renderRecentEvents(); renderSpaces(); renderSettings(); updateChart(history.history || []);
  }

  function renderConnectionError() {
    spaces = spaces.map(space => ({...space, status:"offline", risk:null, reading:{}, bridge:{...(space.bridge || {}), fresh:false}, lastSeen:null}));
    renderDashboard(); renderSpaces();
    text("globalConnection", "Connection Error");
    text("trendStatus", "Connection Error");
  }

  function connectStream() {
    if (socket) socket.close();
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${location.host}/ws`);
    socket.onmessage = () => reload().catch(() => {});
    socket.onclose = () => { renderConnectionError(); if (token) setTimeout(connectStream, 2500); };
  }

  async function removeSpace(id) {
    const space = spaces.find(item => item.id === id);
    if (!space || !confirm(`${space.name} 연결 정보를 삭제할까요?`)) return;
    try { await api(`/api/spaces/${encodeURIComponent(id)}`, {method:"DELETE"}); await reload(); showToast("공간 연결 정보를 삭제했습니다."); }
    catch (error) { showToast(error.message); }
  }

  $("adminLoginForm").onsubmit = async event => {
    event.preventDefault(); text("adminLoginError", ""); $("adminLoginButton").disabled = true;
    try {
      const response = await fetch("/api/auth/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:$("adminLoginId").value.trim(), password:$("adminLoginPassword").value})});
      const data = await response.json(); if (!response.ok) throw new Error(data.error || "로그인할 수 없습니다.");
      token = data.token; sessionStorage.setItem("safenest-token", token); $("adminLoginPassword").value = ""; await reload(); setAuthenticated(true); connectStream();
    } catch (error) { text("adminLoginError", error.message); setAuthenticated(false); }
    finally { $("adminLoginButton").disabled = false; }
  };

  document.querySelectorAll("nav [data-view]").forEach(button => button.onclick = () => switchView(button.dataset.view));
  document.querySelectorAll("[data-go-view]").forEach(button => button.onclick = () => switchView(button.dataset.goView));
  ["eventSpaceFilter","eventLevelFilter"].forEach(id => $(id).onchange = renderEvents); $("eventSearch").oninput = renderEvents;
  $("clearEventFilters").onclick = () => { $("eventSpaceFilter").value="all"; $("eventLevelFilter").value="all"; $("eventSearch").value=""; renderEvents(); };
  $("renameSpaceSelect").onchange = syncRename;
  $("setCurrentSpaceBtn").onclick = () => { currentSpaceId = $("renameSpaceSelect").value; renderDashboard(); switchView("dashboard"); };
  $("renameSpaceBtn").onclick = async () => { try { await api(`/api/spaces/${encodeURIComponent($("renameSpaceSelect").value)}`, {method:"PATCH", body:JSON.stringify({name:$("renameSpaceInput").value.trim()})}); await reload(); showToast("공간 이름을 변경했습니다."); } catch(error) { showToast(error.message); } };
  $("connectionForm").onsubmit = async event => { event.preventDefault(); try { await api("/api/spaces", {method:"POST", body:JSON.stringify({name:$("newSpaceName").value.trim(),nodeId:$("newNodeId").value.trim(),host:$("newHost").value.trim(),port:$("newPort").value.trim()})}); event.target.reset(); await reload(); showToast("공간을 등록했습니다."); } catch(error) { showToast(error.message); } };
  $("testConnectionBtn").onclick = async () => { try { const host=$("newHost").value.trim(), port=$("newPort").value.trim(), path=$("healthPath").value.trim() || "/health"; const base=/^https?:\/\//i.test(host)?host:`http://${host}`; const url=new URL(base); if(port)url.port=port; const response=await fetch(`${url.origin}${path.startsWith("/")?path:`/${path}`}`,{cache:"no-store"}); if(!response.ok)throw new Error(); text("connectionResult","Raspberry Pi 응답을 확인했습니다."); } catch { text("connectionResult","Connection Error"); } };
  const logout = document.createElement("button"); logout.className="secondary-button"; logout.textContent="로그아웃"; logout.onclick=()=>{token="";sessionStorage.removeItem("safenest-token");if(socket)socket.close();setAuthenticated(false);}; document.querySelector(".header-actions").append(logout);

  setInterval(() => { if (token) reload().catch(renderConnectionError); }, 3000);
  if (token) reload().then(() => { setAuthenticated(true); connectStream(); }).catch(error => { text("adminLoginError", error.message); setAuthenticated(false); });
  else setAuthenticated(false);
})();
