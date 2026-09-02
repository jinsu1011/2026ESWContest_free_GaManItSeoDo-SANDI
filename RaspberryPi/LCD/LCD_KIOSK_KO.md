# LCD 키오스크 실행 방법

Raspberry Pi 에 연결된 LCD 에 SafeNest 상태 화면을 띄우고 내리는 방법이다.

LCD 화면은 별도 서버가 아니라 **SafeNest 통합 백엔드(`:8000`)가 직접 서빙**한다.

| 경로 | 파일 |
|---|---|
| `http://127.0.0.1:8000/display` | `RaspberryPi/LCD/static/display.html` |
| `http://127.0.0.1:8000/common.css` | `RaspberryPi/LCD/static/common.css` |

화면은 `GET /api/state` 를 주기적으로 폴링해 갱신한다. 별도의 LCD 서버 프로세스는 없다.

## 1. 백엔드 확인

```bash
pgrep -af run_backend.py || true
curl -fsS http://127.0.0.1:8000/health
curl -fsS -o /dev/null http://127.0.0.1:8000/display
```

백엔드가 실행 중이 아니면 저장소 루트에서 시작한다.

```bash
mkdir -p logs
nohup bash ./run_safenest.sh > logs/runtime.log 2>&1 &
echo $! > .runtime.pid
```

## 2. LCD 화면 켜기

```bash
export DISPLAY=:0
export WAYLAND_DISPLAY=wayland-0
export XDG_RUNTIME_DIR=/run/user/1000
[ -f "$HOME/.Xauthority" ] && export XAUTHORITY="$HOME/.Xauthority"

pkill -f "chromium.*8000/display" 2>/dev/null || true

nohup chromium --kiosk --noerrdialogs --disable-infobars \
  --check-for-update-interval=31536000 \
  --user-data-dir=/tmp/safenest-chromium-display \
  --ozone-platform=x11 \
  http://127.0.0.1:8000/display \
  >/tmp/chromium-display.log 2>&1 &
```

## 3. 화면만 끄기

통합 백엔드는 유지하고 Chromium 만 종료한다.

```bash
pkill -f "chromium.*8000/display" 2>/dev/null || true
```

## 4. 화면과 백엔드 모두 끄기

```bash
pkill -f "chromium.*8000/display" 2>/dev/null || true
kill "$(cat .runtime.pid)" 2>/dev/null || true
pkill -f "backend/run_backend.py" 2>/dev/null || true
```

## 5. 실행 확인

```bash
pgrep -af "chromium.*8000/display" | head -3
curl -fsS http://127.0.0.1:8000/health
```

## 6. 데모용 수동 상태 전환 (선택)

`SAFENEST_DEMO_MODE=1` 로 백엔드를 실행하면 `http://<pi>:8000/control` 조작
화면이 함께 열리고, `POST /api/state` 로 LCD 표시 상태를 수동 전환할 수 있다.
평시 운영에서는 사용하지 않는다.
