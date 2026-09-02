# SafeNest 통신 규격 요약

## 포트

| 용도 | 프로토콜 | 기본 포트 | 방향 |
|---|---|---:|---|
| 센서 데이터 | TCP | 9000 | ESP32 → Raspberry Pi |
| Thermal frame | UDP | 5005 | ESP32 → Raspberry Pi |
| LCD/API | HTTP | 8000 | LCD·노트북 → Raspberry Pi |

## TCP 패킷 헤더

모든 정수는 네트워크 바이트 순서(big-endian)입니다. 헤더 크기는 16바이트입니다.

| 오프셋 | 크기 | 필드 | 값/설명 |
|---:|---:|---|---|
| 0 | 4 | magic | ASCII `SNST` |
| 4 | 1 | version | `1` |
| 5 | 1 | type | `1`: telemetry JSON, `2`: thermal frame |
| 6 | 2 | flags | v1에서는 `0` |
| 8 | 4 | sequence | 패킷 순번 |
| 12 | 4 | payload_length | 뒤따르는 payload의 바이트 수 |

TCP는 패킷 경계를 보존하지 않으므로 Raspberry Pi는 `recv_exact()`로 헤더와 payload 길이만큼 끝까지 읽습니다.

## Type 1: 센서 telemetry JSON

스키마 이름은 `safenest.telemetry.v1`입니다.

```json
{
  "schema": "safenest.telemetry.v1",
  "device_id": "esp32-01",
  "boot_id": "0123456789abcdef0123456789abcdef",
  "seq": 42,
  "uptime_ms": 12345,
  "resp_rate_bpm": 16.25,
  "heart_rate_bpm": 72.5,
  "co2_ppm": 820,
  "co2_measurement_event_id": 42,
  "co2_measurement_monotonic_ms": 12000,
  "co2_measurement_event_valid": true,
  "pir_motion": true,
  "pir_event_id": 3,
  "pir_last_transition_monotonic_ms": 11500,
  "valid": {
    "respiration": true,
    "heart": true,
    "co2": true
  },
  "health": {
    "telemetry_queue_overwrites": 0,
    "thermal_queue_overwrites": 0,
    "tcp_connection_failures": 0,
    "tcp_send_failures": 0,
    "thermal_udp_frames_sent": 42,
    "thermal_udp_send_failures": 0,
    "co2_data_ready_query_failures": 0,
    "co2_read_failures": 0,
    "thermal_status_query_failures": 0
  }
}
```

유효하지 않거나 아직 측정되지 않은 수치 값은 `null`로 보냅니다. `valid` 플래그와 값을 함께 확인해야 합니다. 기존 v1 sender의 확장 필드 부재는 허용하지만, 새 sender의 `boot_id + co2_measurement_event_id`가 실제 측정의 reboot-safe identity입니다. 같은 event ID가 약 1초마다 재전송되어도 새 물리(또는 추론) 측정으로 중복 계산하지 않습니다.

### CO₂ 계측기 (SCD4x와 MH-Z19B)

**이 저장소가 포함하는 sender 는 MH-Z19B 하나뿐입니다** (`ESP32/Arduino/esp32_sensor_node/`). 아래 표의 SCD4x 열은 같은 프로토콜을 쓰던 이전 팀 변형으로, 제출본에 포함하지 않았습니다. 대조해 두는 이유는 event identity 의미가 서로 다르고, 이어지는 규칙이 그 차이를 전제하기 때문입니다.

**ppm 단위가 같다고 drop-in 호환이 아닙니다.** Pi는 계속 `co2_ppm`과 event identity로 150 s slope를 재구성하며, ESP32는 slope/점유 모델을 돌리지 않습니다. C-B6 threshold 0.43과 모델 아티팩트는 이 프로토콜 변경으로 건드리지 않습니다.

| CO₂ 센서 | 제출본 포함 | 버스 | 이벤트 정체성 |
|---|---|---|---|
| Winsen MH-Z19B | **예 — 정본 sender** | UART1 9600 8N1, ESP32 RX=32 / TX=33 (**MR60 UART2 16/17과 공유 금지**) | **INFERRED_UART_SAMPLE**. manufacturer data-ready 없음. checksum-valid `0x86`를 선언된 UART poll 주기(기본 5 s)당 최대 1회 수락한 뒤에만 `co2_measurement_event_id` 증가. 이 주기는 물리 NDIR 변환 주기가 아님 |
| Sensirion SCD40/SCD4x | 아니오 — 이전 변형 | I²C `0x62`, SDA=21 SCL=22 | SCD4x `getDataReadyStatus` 후 성공한 `readMeasurement`만 새 이벤트. `co2_ppm==0`은 이벤트 아님 |

MH-Z19B sender는 다음 필드를 추가로 보냅니다.

```json
{
  "firmware_version": "safenest-esp32-sensor-node/1.7.0-mhz19b.1",
  "co2_sensor_model": "MH-Z19B",
  "co2_event_identity_class": "INFERRED_UART_SAMPLE",
  "co2_measurement_event_id": 42,
  "co2_measurement_monotonic_ms": 180000,
  "co2_measurement_event_valid": true,
  "co2_preheat": false
}
```

규칙:

- checksum 실패, timeout, short frame, 예열(3분), stale(`CO2_STALE_MS` 15 s)에서는 `co2_ppm`이 `null`이고 `valid.co2=false`이며, event tuple은 프로토콜이 요구하는 `0 / 0 / false`입니다.
- `valid.co2`(최근 성공이 stale 한도 안)와 `co2_measurement_event_valid`(이번 snapshot이 그 이벤트를 현재로 실어도 되는지)는 다릅니다.
- `co2_event_identity_class`가 `INFERRED_UART_SAMPLE`이면 SCD40 conversion이라고 기록하지 마십시오. 두 센서를 한 device-domain으로 풀링하지 않습니다.
- MH-Z19B는 Vin 4.5–5.5 V(피크 150 mA)가 필요합니다. ESP32 3.3 V 레일로 켜지 않습니다.

## Thermal UDP v1 logical frame

payload는 16바이트 metadata 다음에 80 × 62개의 `uint16` 픽셀이 이어집니다.

| 오프셋 | 크기 | 필드 |
|---:|---:|---|
| 0 | 2 | width (`80`) |
| 2 | 2 | height (`62`) |
| 4 | 4 | frame_sequence |
| 8 | 4 | uptime_ms |
| 12 | 2 | minimum_raw |
| 14 | 2 | maximum_raw |
| 16 | 9,920 | 픽셀 4,960개, 각 `uint16` big-endian |

logical payload 9,936바이트는 1,168바이트 이하 payload를 가진 9개 UDP datagram으로 나뉩니다. 각 32바이트 UDP header는 `SNTU`, version 1, frame sequence, chunk index/count, logical size/offset/length와 logical-frame CRC32를 포함합니다. Pi는 순서와 무관하게 bounded reassembly한 뒤 CRC32, shape, payload length와 min/max를 모두 검증합니다. Thermal은 TCP로 보내지 않습니다.

## Sequence와 시간 의미

- TCP telemetry `seq`: scalar publication 때 증가
- `co2_measurement_event_id`: SCD4x sender는 새 `readMeasurement` 성공 때만 증가. MH-Z19B sender는 checksum-valid `0x86`를 선언된 UART poll 주기로 수락했을 때만 증가(`INFERRED_UART_SAMPLE`). 어느 sender든 1 Hz TCP 재전송에서는 증가하지 않음
- Thermal `frame_sequence`: frame 획득 때 증가
- Thermal `chunk_index`: 동일 frame 안의 chunk 위치이며 sequence가 아님
- `pir_event_id`: PIR 상태 전환 때만 증가
- ESP `uptime_ms`와 event time은 source monotonic domain, Pi 수신 wall/monotonic time은 receiver domain이며 clock synchronization을 가정하지 않음

`health`는 scalar/thermal queue overwrite, TCP connect/send failure, Thermal UDP frame send success/failure와
센서 취득 경로에서 실제로 관찰된 실패의 누적값을 담는다. SCD4x sender의 `co2_data_ready_query_failures`는
`getDataReadyStatus` API 오류만 세며 정상적인 `not-ready` 응답은 실패가 아니다. `co2_read_failures`는
`readMeasurement` 오류만 세고, 성공했지만 유효한 CO₂ 값이 없는 경우에는 새 measurement event를 만들지
않는다. MH-Z19B v2-port sender는 slim LCD JSON에 `health` 객체를 넣지 않으며, UART checksum/timeout/short
frame 카운터는 Serial `[health] co2` 줄에만 남긴다. `thermal_status_query_failures`는 READY 핀 fallback에서
실제 I2C status query가 실패한 경우만 센다. Queue overwrite는 소비 task가 dequeue한 sequence gap으로
계산하므로 실제로 건너뛴 one-slot queue item만 센다.

현재 MI48xx/SPI 경로는 독립적인 acquisition result나 camera-native CRC를 제공하지 않는다
(`THERMAL_ACQUISITION_ERROR_DETAIL_UNAVAILABLE`). 따라서 그런 값을 꾸며내지 않는다. SafeNest logical-frame
CRC32와 Pi reassembly failure는 별도 metric이다.

## HTTP API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/display` | Raspberry Pi LCD 화면 |
| GET | `/control` | 노트북 원격 제어 화면 |
| GET | `/api/state` | 화면 상태와 최신 센서 데이터 |
| POST | `/api/state` | 화면 상태 또는 공간 이름 변경 |
| GET | `/health` | 서버, 부저, 센서 연결 상태 |

상태 변경 예시 (`SAFENEST_DEMO_MODE=1` 로 기동했을 때만 열리는 데모 전용 경로):

```bash
curl -s -X POST http://127.0.0.1:8000/api/state \
  -H 'Content-Type: application/json' \
  -d '{"state":"emergency"}'
```

허용 상태:

- `normal-empty`
- `normal-occupied`
- `warning`
- `danger`
- `emergency`
- `offline`

## 장애 확인 순서

1. Raspberry Pi에서 `ss -ltn | grep ':9000 '`으로 TCP 수신 포트를 확인합니다.
2. ESP32 시리얼 로그에서 Wi-Fi 연결과 `Raspberry Pi connected`를 확인합니다.
3. `/health`의 `sensors.status`, `peer`, `age_seconds`, `listener_error`를 확인합니다.
4. HTTP 서버는 정상이지만 LCD가 보이지 않으면 `logs/chromium.log`를 확인합니다.
5. 종료할 때는 `bash stop_lcd.sh`를 사용합니다.

## Pi ingest note (MH-Z19B, 2026-08-31)

ESP32 스케치 변경은 PR #68 (`feature/esp32-mhz19b-co2-v2-port`)으로 `main`에 들어갔습니다. 이 절은 Raspberry Pi 수신만 적습니다.

Pi Runtime은 아래 필드를 `safenest.telemetry.v1`의 **optional** 확장으로 파싱합니다. 없으면 거절하지 않습니다.

- `co2_sensor_model` (`MH-Z19B`)
- `co2_event_identity_class` (`INFERRED_UART_SAMPLE`; SCD40 `getDataReady` 변환이 아님)
- `co2_preheat_complete` (bool). 펌웨어가 `co2_preheat`만 보내면 Pi가 같은 의미로 매핑합니다.
- `abc_enabled`, `configured_range_ppm` (`2000` / `5000` / `10000`)

H150(`CO2_slope`, 150 s endpoint difference)은 `co2_measurement_event_id`만 이력 키로 씁니다. Pi는 `seq`, `/health`의 `fresh`, `age_seconds`로 event id를 만들지 않습니다. `co2_preheat_complete`가 true가 아니면 C-B6 입력에 넣지 않습니다. 수식·scaler·threshold 0.43은 그대로입니다.

