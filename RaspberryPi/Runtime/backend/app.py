"""FastAPI application factory; framework imports remain optional for core tests."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any

from backend.runtime import SafeNestRuntime
from backend.portal import PortalAuth, PortalStore, portal_event, portal_space, thermal_payload
from backend.store import RuntimeStore
from backend.views import (
    ROUTE_CONTRACTS,
    events_document,
    health_document,
    history_document,
    legacy_state_document,
    sensors_document,
    status_document,
)
from services.buzzer import BuzzerProtocol, create_buzzer_from_env
from services.emergency import EmergencyActionError, EmergencyActionService
from services.sms_service import SMSProvider
from paths import DATA_ROOT, LCD_STATIC, WEB_GUEST, WEB_PORTAL, WEB_ROOT


class BackendDependencyError(RuntimeError):
    pass


_DEMO_TTS_LEVELS = {
    "normal-empty": "NORMAL",
    "normal-occupied": "NORMAL",
    "warning": "WARNING",
    "danger": "DANGER",
    "emergency": "DANGER",
    "offline": "INDETERMINATE",
}


def _notify_demo_tts(
    runtime: SafeNestRuntime,
    store: RuntimeStore,
    state: str,
) -> bool:
    """Feed only the manual LCD demo state into the existing TTS controller."""

    publication = {
        "risk": {
            "risk_level": _DEMO_TTS_LEVELS[state],
            "reasons": [f"LCD_DEMO_{state.upper().replace('-', '_')}"],
            "escalation_floors": [],
        },
        "emergency": {"active": state == "emergency"},
    }
    try:
        return bool(runtime.tts.handle_publication(publication))
    except Exception as error:
        store.record_runtime_error("demo_tts", error)
        return False


def create_app(
    runtime: SafeNestRuntime | None = None,
    *,
    store: RuntimeStore | None = None,
    start_runtime: bool = True,
    room: str = "밀폐공간 A-01",
    websocket_interval_seconds: float = 1.0,
    emergency_service: EmergencyActionService | None = None,
    sms_provider: SMSProvider | None = None,
    buzzer: BuzzerProtocol | None = None,
    sms_cooldown_seconds: float | None = None,
    demo_mode: bool | None = None,
) -> Any:
    if websocket_interval_seconds <= 0:
        raise ValueError("websocket interval must be positive")
    try:
        from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
        from fastapi.staticfiles import StaticFiles
    except ImportError as error:
        raise BackendDependencyError(
            "FastAPI is not installed; install requirements-backend.txt"
        ) from error

    if runtime is not None and store is not None and runtime.store is not store:
        raise ValueError("runtime and store must reference the same RuntimeStore")
    selected_store = store or (runtime.store if runtime is not None else RuntimeStore())
    selected_runtime = runtime or SafeNestRuntime(store=selected_store)
    selected_sms_cooldown = (
        _float_env("SAFENEST_SMS_COOLDOWN_SECONDS", 60.0)
        if sms_cooldown_seconds is None
        else float(sms_cooldown_seconds)
    )
    selected_demo_mode = (
        _bool_env("SAFENEST_DEMO_MODE", False)
        if demo_mode is None
        else bool(demo_mode)
    )
    selected_buzzer = buzzer
    if emergency_service is None:
        selected_buzzer = selected_buzzer or create_buzzer_from_env()
        selected_emergency = EmergencyActionService(
            selected_store,
            sms_provider=sms_provider,
            room=room,
            sms_cooldown_seconds=selected_sms_cooldown,
            buzzer=selected_buzzer,
        )
    else:
        selected_emergency = emergency_service
    if selected_buzzer is not None:
        selected_store.attach_buzzer(selected_buzzer)

    @asynccontextmanager
    async def lifespan(_app):
        if start_runtime:
            selected_runtime.start()
        try:
            yield
        finally:
            if start_runtime:
                selected_runtime.stop()
            close = getattr(selected_store, "close", None)
            if callable(close):
                close()
            close_buzzer = getattr(selected_buzzer, "close", None)
            if callable(close_buzzer):
                close_buzzer()

    app = FastAPI(
        title="SafeNest Integrated Backend",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.safenest_runtime = selected_runtime
    app.state.safenest_store = selected_store
    app.state.safenest_emergency = selected_emergency
    app.state.safenest_demo_mode = selected_demo_mode

    frontend_dir = Path(
        os.getenv("SAFENEST_WEB_DIR", str(WEB_PORTAL))
    ).resolve()
    portal_store = PortalStore(
        os.getenv("SAFENEST_SPACES_FILE", str(DATA_ROOT / "web" / "spaces.json"))
    )
    portal_auth = PortalAuth()
    if not portal_auth.configured:
        print(
            "[SafeNest] 관리자 계정이 설정되지 않았습니다. "
            "SAFENEST_ADMIN_ID / SAFENEST_ADMIN_PASSWORD 를 설정하기 전까지 "
            "/admin 로그인은 항상 거부됩니다. (그 외 런타임은 정상 동작)"
        )
    offline_grace = _float_env("SAFENEST_PORTAL_OFFLINE_SECONDS", 30.0)
    app.state.safenest_portal_store = portal_store
    app.state.safenest_portal_auth = portal_auth

    dashboard_dir = WEB_ROOT
    dashboard_index = "index.html" if selected_demo_mode else "index_final.html"
    app.mount(
        "/dashboard/assets",
        StaticFiles(directory=str(dashboard_dir)),
        name="dashboard-assets",
    )
    # Vendored third-party front-end libraries, served same-origin so the
    # administrator UI needs no internet access. See RaspberryPi/Web/vendor/.
    vendor_dir = WEB_ROOT / "vendor"
    if vendor_dir.is_dir():
        app.mount("/vendor", StaticFiles(directory=str(vendor_dir)), name="web-vendor")
    lcd_override: dict[str, str | None] = {"state": None, "room": room}

    @app.get("/dashboard", include_in_schema=False)
    @app.get("/dashboard/", include_in_schema=False)
    def dashboard() -> Any:
        return FileResponse(dashboard_dir / dashboard_index)

    # LCD panel (RaspberryPi/LCD/static) — served by the same :8000 runtime
    if LCD_STATIC.is_dir():
        @app.get("/display", include_in_schema=False)
        @app.get("/display/", include_in_schema=False)
        @app.get("/display.html", include_in_schema=False)
        def lcd_display() -> Any:
            return FileResponse(LCD_STATIC / "display.html")

        @app.get("/common.css", include_in_schema=False)
        def lcd_common_css() -> Any:
            return FileResponse(LCD_STATIC / "common.css")

        if selected_demo_mode:
            app.mount(
                "/lcd/assets",
                StaticFiles(directory=str(LCD_STATIC)),
                name="lcd-assets",
            )

            @app.get("/control", include_in_schema=False)
            @app.get("/control/", include_in_schema=False)
            @app.get("/control.html", include_in_schema=False)
            def lcd_control() -> Any:
                return FileResponse(LCD_STATIC / "control.html")

    def require_admin(request: Request) -> None:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not portal_auth.verify(token):
            raise HTTPException(status_code=401, detail="관리자 로그인이 필요합니다.")

    def live_portal_status() -> dict[str, Any]:
        """Overlay latest sensor state without changing the 15 s risk cadence."""
        document = status_document(selected_store.latest())
        snapshot = selected_runtime.manager.snapshot()
        sensors = snapshot.get("sensors", {})
        if isinstance(sensors, dict):
            for sensor_id in ("mmwave", "thermal", "co2", "pir"):
                if sensor_id in sensors and isinstance(document.get(sensor_id), dict):
                    document[sensor_id]["state"] = sensors[sensor_id]
        document["system"] = snapshot.get("system", document.get("system"))
        document["timestamp"] = snapshot.get("timestamp", document.get("timestamp"))
        return document

    def lcd_state_document() -> dict[str, Any]:
        document = legacy_state_document(
            selected_store.latest(),
            room=str(lcd_override["room"] or room),
        )
        if selected_demo_mode and lcd_override["state"] is not None:
            document["state"] = lcd_override["state"]
        snapshot = selected_runtime.manager.snapshot()
        current_sensors = snapshot.get("sensors")
        if isinstance(current_sensors, dict):
            sensors = document.setdefault("sensors", {})
            for sensor_id in ("mmwave", "thermal", "co2", "pir"):
                current = current_sensors.get(sensor_id)
                if not isinstance(current, dict):
                    continue
                existing = sensors.get(sensor_id)
                if not isinstance(existing, dict):
                    existing = {}
                    sensors[sensor_id] = existing
                existing["state"] = current
        document["revision"] = snapshot.get("revision", document.get("revision", 0))
        document["updated_at"] = int(float(snapshot.get("timestamp", document.get("updated_at", 0))))
        return document

    def frontend_file(name: str) -> Any:
        path = frontend_dir / name
        if not path.is_file():
            raise HTTPException(
                status_code=503,
                detail=f"웹 화면 파일을 찾을 수 없습니다: {path}",
            )
        return FileResponse(path)

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_page() -> Any:
        return frontend_file("preview.html")

    @app.get("/admin-api.js", include_in_schema=False)
    @app.get("/admin/admin-api.js", include_in_schema=False)
    def admin_script() -> Any:
        return frontend_file("admin-api.js")

    @app.get("/thermal-client.js", include_in_schema=False)
    @app.get("/admin/thermal-client.js", include_in_schema=False)
    def thermal_script() -> Any:
        return frontend_file("thermal-client.js")

    @app.get("/guest/dashboard/{space_id}", include_in_schema=False)
    def guest_dashboard(space_id: str) -> Any:
        if portal_store.get(space_id) is None:
            raise HTTPException(status_code=404, detail="등록되지 않은 공간입니다.")
        return FileResponse(WEB_GUEST / "index.html")

    @app.get("/")
    def root() -> Any:
        return RedirectResponse(url="/admin", status_code=307)

    @app.post("/api/auth/login")
    async def api_login(request: Request) -> Any:
        payload = await json_payload(request)
        token = portal_auth.login(payload.get("id"), payload.get("password"))
        if token is None:
            if not portal_auth.configured:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "관리자 계정이 설정되지 않았습니다. "
                                 "SAFENEST_ADMIN_ID / SAFENEST_ADMIN_PASSWORD 를 설정하세요."
                    },
                )
            return JSONResponse(status_code=401, content={"error": "아이디 또는 비밀번호가 올바르지 않습니다."})
        return {"token": token, "expiresIn": 12 * 60 * 60}

    @app.get("/api/spaces")
    def api_spaces(request: Request) -> list[dict[str, Any]]:
        require_admin(request)
        status = live_portal_status()
        return [portal_space(item, status, offline_after_seconds=offline_grace) for item in portal_store.list()]

    @app.post("/api/spaces")
    async def api_create_space(request: Request) -> Any:
        require_admin(request)
        payload = await json_payload(request)
        try:
            item = portal_store.create(payload)
        except ValueError as error:
            return JSONResponse(status_code=422, content={"error": str(error)})
        selected_store.record_event("SPACE_CREATED", {"space_id": item["id"], "name": item["name"]})
        return portal_space(item, live_portal_status(), offline_after_seconds=offline_grace)

    @app.patch("/api/spaces/{space_id}")
    async def api_update_space(space_id: str, request: Request) -> Any:
        require_admin(request)
        payload = await json_payload(request)
        try:
            item = portal_store.update(space_id, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="등록되지 않은 공간입니다.")
        except ValueError as error:
            return JSONResponse(status_code=422, content={"error": str(error)})
        selected_store.record_event("SPACE_UPDATED", {"space_id": item["id"], "name": item["name"]})
        return portal_space(item, live_portal_status(), offline_after_seconds=offline_grace)

    @app.delete(
        "/api/spaces/{space_id}",
        status_code=204,
        response_class=Response,
        response_model=None,
    )
    def api_delete_space(space_id: str, request: Request) -> Response:
        require_admin(request)
        try:
            portal_store.delete(space_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="등록되지 않은 공간입니다.")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        selected_store.record_event("SPACE_DELETED", {"space_id": space_id})
        return Response(status_code=204)

    @app.get("/api/portal/events")
    def api_portal_events(request: Request, limit: int = 100) -> list[dict[str, Any]]:
        require_admin(request)
        try:
            return [portal_event(item) for item in selected_store.events(limit)]
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/guest/spaces/{space_id}")
    def api_guest_space(space_id: str) -> dict[str, Any]:
        item = portal_store.get(space_id)
        if item is None:
            raise HTTPException(status_code=404, detail="등록되지 않은 공간입니다.")
        return portal_space(item, live_portal_status(), offline_after_seconds=offline_grace)

    @app.get("/api/thermal/{space_id}")
    def api_thermal(space_id: str, request: Request) -> Any:
        if portal_store.get(space_id) is None:
            raise HTTPException(status_code=404, detail="등록되지 않은 공간입니다.")
        frame = selected_runtime.manager.latest_thermal_frame() if space_id == "A01" else None
        if frame is None:
            return Response(status_code=204)
        etag = f'"thermal-{frame.frame_sequence}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
        return Response(
            content=thermal_payload(frame),
            media_type="application/octet-stream",
            headers={"ETag": etag, "Cache-Control": "no-store"},
        )

    @app.get("/api/qr/{space_id}.png")
    def api_space_qr(space_id: str, request: Request) -> Any:
        if portal_store.get(space_id) is None:
            raise HTTPException(status_code=404, detail="등록되지 않은 공간입니다.")
        public_base = os.getenv("SAFENEST_PUBLIC_URL", str(request.base_url).rstrip("/"))
        target = f"{public_base.rstrip('/')}/guest/dashboard/{space_id}"
        try:
            import io
            import qrcode
            image = qrcode.make(target)
            output = io.BytesIO()
            image.save(output, format="PNG")
        except ImportError as error:
            raise HTTPException(status_code=503, detail="QR 패키지가 설치되지 않았습니다.") from error
        return Response(content=output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        return status_document(selected_store.latest())

    @app.get("/api/sensors")
    def api_sensors() -> dict[str, Any]:
        return sensors_document(selected_store.latest())

    @app.get("/api/events")
    def api_events(limit: int = 100) -> dict[str, Any]:
        try:
            events = selected_store.events(limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        diagnostics = selected_store.diagnostics()
        database = diagnostics.get("database")
        persistent = isinstance(database, dict) and database.get("available") is True
        return events_document(events, persistent=persistent)

    @app.get("/api/history")
    def api_history(limit: int = 100) -> dict[str, Any]:
        try:
            history = selected_store.history(limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        diagnostics = selected_store.diagnostics()
        database = diagnostics.get("database")
        persistent = isinstance(database, dict) and database.get("available") is True
        return history_document(history, persistent=persistent)

    @app.get("/api/state")
    def api_state_compatibility() -> dict[str, Any]:
        return lcd_state_document()

    if selected_demo_mode:
        @app.post("/api/state")
        async def api_state_update(request: Request) -> dict[str, Any]:
            payload = await json_payload(request)
            state = payload.get("state")
            requested_room = payload.get("room")
            allowed_states = {"normal-empty", "normal-occupied", "warning", "danger", "emergency", "offline"}
            if state is not None and state not in allowed_states:
                raise HTTPException(status_code=422, detail="지원하지 않는 LCD 상태입니다.")
            if requested_room is not None and (not isinstance(requested_room, str) or not requested_room.strip()):
                raise HTTPException(status_code=422, detail="공간 이름을 입력하세요.")
            if state is None and requested_room is None:
                raise HTTPException(status_code=422, detail="변경할 항목이 없습니다.")
            if state is not None:
                demo_state = str(state)
                lcd_override["state"] = demo_state
                _notify_demo_tts(selected_runtime, selected_store, demo_state)
            if requested_room is not None:
                lcd_override["room"] = str(requested_room).strip()[:24]
            selected_store.record_event(
                "LCD_STATE_UPDATED",
                {"state": lcd_override["state"], "room": lcd_override["room"]},
            )
            return lcd_state_document()

    @app.get("/api/emergency/state")
    def api_emergency_state() -> dict[str, Any]:
        return {"ok": True, "emergency": selected_store.emergency_snapshot()}

    def action_error(error: EmergencyActionError) -> JSONResponse:
        body: dict[str, Any] = {
            "ok": False,
            "error_code": error.code,
            "message": str(error),
        }
        if error.retry_after_seconds is not None:
            body["retry_after_seconds"] = round(max(0.0, error.retry_after_seconds), 3)
        return JSONResponse(status_code=error.status_code, content=body)

    async def json_payload(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON object body is required")
        return payload

    if selected_demo_mode:
        @app.post("/api/emergency/119/simulation/start", response_model=None)
        def start_119_simulation() -> dict[str, Any] | JSONResponse:
            try:
                return selected_emergency.start_119_simulation()
            except EmergencyActionError as error:
                return action_error(error)

        @app.post("/api/emergency/119/simulation/complete", response_model=None)
        async def complete_119_simulation(request: Request) -> dict[str, Any] | JSONResponse:
            payload = await json_payload(request)
            try:
                return selected_emergency.complete_119_simulation(str(payload.get("simulation_id", "")))
            except EmergencyActionError as error:
                return action_error(error)

    @app.post("/api/emergency/contact", response_model=None)
    async def contact_manager(request: Request) -> dict[str, Any] | JSONResponse:
        payload = await json_payload(request)
        try:
            return selected_emergency.send_manager_sms(
                idempotency_key=payload.get("idempotency_key"),
            )
        except EmergencyActionError as error:
            return action_error(error)

    @app.post("/api/emergency/acknowledge", response_model=None)
    def acknowledge_alarm() -> dict[str, Any] | JSONResponse:
        try:
            return selected_emergency.acknowledge_alarm()
        except (EmergencyActionError, RuntimeError) as error:
            if isinstance(error, EmergencyActionError):
                return action_error(error)
            return action_error(EmergencyActionError("DANGER_NOT_ACTIVE", str(error), status_code=409))

    @app.post("/api/emergency/recovery/acknowledge", response_model=None)
    def acknowledge_recovery() -> dict[str, Any] | JSONResponse:
        try:
            return selected_emergency.acknowledge_recovery()
        except EmergencyActionError as error:
            return action_error(error)

    @app.post("/api/emergency/voice", response_model=None)
    async def record_voice(request: Request) -> dict[str, Any] | JSONResponse:
        payload = await json_payload(request)
        try:
            return selected_emergency.record_voice_event(str(payload.get("action", "")))
        except EmergencyActionError as error:
            return action_error(error)

    @app.post("/api/client-connection", response_model=None)
    async def record_client_connection(request: Request) -> dict[str, Any] | JSONResponse:
        payload = await json_payload(request)
        try:
            return selected_emergency.record_client_connection(
                source=str(payload.get("source", "")),
                status=str(payload.get("status", "")),
            )
        except EmergencyActionError as error:
            return action_error(error)

    @app.get("/health")
    def health() -> dict[str, Any]:
        document = health_document(selected_store.diagnostics(), selected_runtime.receiver_stats())
        # Deployment hygiene, not a credential: only whether the admin account
        # was supplied through the environment.
        document["admin_auth_configured"] = portal_auth.configured
        return document

    @app.websocket("/ws")
    async def websocket_status(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            selected_emergency.record_client_connection(source="websocket", status="online")
        except EmergencyActionError:
            pass
        last_revision = -1
        try:
            while True:
                publication = selected_store.latest()
                revision = publication.get("publication_revision", 0) if publication else 0
                if revision != last_revision:
                    await websocket.send_json(status_document(publication))
                    last_revision = int(revision)
                await asyncio.sleep(websocket_interval_seconds)
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            try:
                selected_emergency.record_client_connection(source="websocket", status="offline")
            except EmergencyActionError:
                pass

    return app


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")
