# ESP32 MH-Z19B v2 포트 변경 로그

## 범위

이 스케치는 SCD40/SCD4x I²C 를 쓰던 v2 스케치를 **제자리에서 덮어쓰지 않고** 복사한 뒤, CO₂ 계측만 Winsen MH-Z19B UART 로 바꾼 것에서 출발했습니다. 아래 변경 이력의 폴더명은 각 버전 당시의 이름이며, 현재 정본 위치는 다음과 같습니다.

- 폴더: `ESP32/Arduino/esp32_sensor_node/`
- 펌웨어: `safenest-esp32-sensor-node/1.7.10-mhz19b.1` (`esp32_sensor_node.ino` 의 `NODE_FIRMWARE_VERSION`)
- 출발점이던 SCD40 v2 스케치는 이 제출 저장소에 포함하지 않았습니다.

모델 재학습, threshold 0.43, C-B6 scaler/TFLite, ESP32 쪽 slope/점유 모델은 **범위 밖**입니다. Pi `RaspberryPi/Runtime/ai/co2_canonical_runtime.py`가 이미 ENDPOINT_H150 / 150 s slope를 재구성합니다.

## 배선 / 전원 (MH-Z19B)

Winsen MH-Z19B User’s Manual v1.7 (2020-10-15) 기준입니다. 임의 블로그가 아닙니다.

| 항목 | 값 | 주의 |
|---|---|---|
| Vin | 4.5–5.5 V DC | **ESP32 3.3 V 레일에서 켜지 말 것.** 피크 150 mA |
| UART | 9600 8N1 TTL 3.3 V (5 V 호환) | TX/RX **교차** |
| 이 스케치 UART | **UART1** GPIO **32 (RX)** / **33 (TX)** | MR60 UART2 GPIO **16/17을 절대 공유하지 않음** |
| PWM | 미사용 | PWM 주기(~1 s)를 UART 변환 주기로 취급하지 않음 |
| HD | 미연결 | 잠긴 교정 정책이 생기기 전에는 쓰지 않음 |
| 예열 | 3분 | 예열 중 TCP 패킷은 나가지만 모델 측정으로 표시하지 않음 |

핀을 32/33으로 고른 이유: v2 핀맵(I²C 21/22, PIR 13, Thermal SPI/I2C 18/19/23/27/26/25, MR60 16/17) 이후 남는 출력 가능 GPIO이고, WROOM 플래시/스트래핑 핀이 아닙니다. `HardwareSerial(1).setPins()`를 `begin()` 전에 호출해 기본 9/10(플래시)을 피합니다.

공통 GND는 ESP32와 5 V 공급을 함께 묶습니다.

## ABC / 측정 범위

- Winsen 문서상 출하 ABC는 **ON** (24 h, 400 ppm). 이 스케치는 `0x79`를 **보내지 않습니다.** 의도 상태는 factory default ON입니다.
- 범위 명령 `0x99`도 **보내지 않습니다.** 모듈 variant를 실측으로 증명하기 전입니다.
- 수용 상한은 **10000 ppm** (0은 fail-closed). UCI 학습이 2076 ppm까지 갔으므로 2000 ppm 클립을 가정하지 않습니다. 구성된 실측 범위는 **미확인**이며 5000 ppm class를 선호할 뿐입니다.
- SCD40 숫자에 맞추려고 ABC를 켜고 끄는 “같아 보일 때까지 교정”은 구현하지 않았습니다.

ppm 단위가 같다고 drop-in 호환이 **아닙니다.** 감사 결론은 `COMPATIBLE_ONLY_AS_NEW_DEVICE_DOMAIN` / PATH B, 재학습 NO, threshold 0.43 유지입니다.

## 이벤트 정체성 (INFERRED_UART_SAMPLE)

MH-Z19B에는 SCD4x `getDataReadyStatus`가 없습니다. 공식 문서는 각 `0x86` 응답이 새 NDIR 변환임을 증명하지 않습니다. 반복 UART 읽기는 최신 가용 값을 돌려줍니다.

따라서 이 노드는 **추론된** producer identity를 붙입니다.

| JSON 필드 | 의미 |
|---|---|
| `co2_sensor_model` | `"MH-Z19B"` |
| `co2_event_identity_class` | `"INFERRED_UART_SAMPLE"` — SCD40 conversion이라고 주장하지 않음 |
| `co2_measurement_event_id` | checksum-valid `0x86`를 **선언된 UART poll 주기당 최대 1회** 수락했을 때만 증가. wrap 시 0 건너뜀 |
| `co2_measurement_monotonic_ms` | 수락 시점의 `millis()` |
| `co2_measurement_event_valid` | 그 샘플이 stale/예열이 아닐 때만 true. 아니면 `0/0/false` |
| `co2_preheat` | boot 후 180 s 동안 true (지금 예열 중) |
| `co2_preheat_complete` | `!co2_preheat`. Pi C-B6 게이트. canonical이 있으면 alias를 이긴다 |
| `valid.co2` | 예열 종료 후 마지막 성공이 `CO2_STALE_MS`(15 s) 안. **새 이벤트와 다름** |

정책:

- 선언된 UART 샘플 주기: `CO2_UART_SAMPLE_PERIOD_MS = 5000`. SCD4x가 ~5 s였고 PWM 인코딩은 ~1 s라서 그 사이에 맞춘 것입니다. **UART poll rate = 물리 변환 rate가 아닙니다.**
- 1 Hz TCP snapshot마다 event_id를 올리지 않습니다. 같은 event_id는 캐시 재전송이며 Pi slope가 무시해야 합니다.
- ppm이 안 변할 때만 올리는 방식은 쓰지 않습니다(빈 방이 영원히 freeze됨).
- checksum 실패 / timeout / short frame → 추정 ppm을 발행하지 않음. JSON `co2_ppm`은 `null`, `valid.co2=false`.
- stale ppm을 새 이벤트로 forward-fill하지 않음.
- 3분 예열: 패킷은 계속 나가지만 `event_valid=false`, `valid.co2=false`.

ESP32는 slope를 계산하지 않습니다.

## v2에서 유지한 것

- MR60 UART2 16/17, `human_detected_raw` 3상태 null/true/false
- Thermal SPI/I2C/UDP 계약, PIR
- TCP `safenest.telemetry.v1`, FreeRTOS TCP/UDP, backpressure / mutex
- loop에 `delay()` 없음, `millis()` 스케줄
- 무효 수치는 JSON `null`
- `secrets.h` 미커밋

## 검증 및 제한

- v2 대비 차이는 CO₂ 드라이버와 그에 필요한 JSON 필드에 한정되도록 정적 비교했습니다.
- JSON `snprintf` truncation은 전송 실패(fail-closed). 호스트에서 worst-case payload < 1024 B를 확인합니다.
- Arduino CLI가 있으면 이 스케치를 컴파일합니다.
- **실기 flash / MH-Z19B UART ppm 관측은 이 작업에서 수행하지 않았습니다.** `DEVICE_VALIDATED=NO`.
- 잔여 위험: ABC 출하 상태, 범위 variant(2000/5000/10000), manufacturer data-ready 부재.

## 1.7.1 — UDP 청크 양보 + 주기 힙 회수

MH-Z19B 스케치는 PR #71 청크 양보가 빠져 있었고, 9개 UDP datagram을 2 ms 간격으로 연속 송신하면 lwIP 풀이 차고 TCP 1 Hz JSON이 밀렸습니다. 힙이 ~40 KB 떨어지는 현상과 같은 경로입니다.

- 청크 사이 `UDP_CHUNK_GAP_MS = 20`. TCP가 그 틈에 mutex를 잡고 JSON을 보냅니다.
- TCP write/connect 중이면 같은 청크를 버리고 끝내지 않고 기다렸다가 이어서 보냅니다 (`yieldRadioToTcp`). 센서 JSON 값/시퀀스는 바꾸지 않습니다.
- 5초마다, 또는 free heap이 48 KB 아래로 떨어지면 thermal UDP 소켓만 `stop()`/`begin()` 해서 lwIP pbuf를 돌려줍니다. TCP 클라이언트와 큐에 있는 telemetry JSON은 건드리지 않습니다. TCP가 보내는 중이면 회수를 건너뜁니다.
- 펌웨어 id: `safenest-esp32-sensor-node/1.7.1-mhz19b.1`

## 1.7.2 — mmWave 0.0 vs stale null

`[health] sensors resp=0.0`는 Pi의 `nan`/`null`과 다른 의미였습니다. 헬스는 마지막 캐시를 stale 검사 없이 찍고, `mmw_miss`는 레이더 고장이 아니라 `update(0)`이 빈 UART에서 false를 준 루프 횟수였습니다.

- 헬스 `resp`/`heart`는 TCP와 같이 10 s stale이면 `null`. `resp_age_ms` / `heart_age_ms` / `presence_age_ms`를 같이 찍습니다.
- `mmw_miss`는 UART에 바이트가 있는데 프레임이 안 열릴 때만 증가합니다.
- thermal SPI 전후에 mmWave를 drain합니다.
- 레이더가 보낸 0.0 bpm(미검출)은 그대로 0.00으로 둡니다. 0을 숨기지 않습니다.

## 1.7.4 — MR60 breath_phase 복구 (Pi 300윈도우)

LCD 슬림 JSON이 `mmwave.human_detected_raw`만 남겨서 Pi M-N4/B23 창이 한 샘플도 못 쌓았습니다. 벤더 `resp_rate_bpm`은 창에 들어가지 않습니다.

- `pollMmWave()`가 다시 `getHeartBreathPhases()`를 호출합니다 (0x0A13).
- JSON `mmwave`에 1.3.0과 같은 `breath_phase` / `total_phase` / `heart_phase` / `breath_rate_raw` / `phase_age_ms` / `ts_monotonic_ms` / `seq` / `schema_version`을 넣습니다. MH-Z19B 이벤트 필드는 유지합니다.
- 위상 값은 `PHASE_MAX_AGE_MS = 500` 안에서만 숫자로 나갑니다. seq/시각은 마지막 샘플이 있으면 남깁니다.
- `TELEMETRY_PERIOD_MS`를 1000 → 100으로 되돌립니다. 1 Hz면 500 ms freshness에 걸려 `breath_phase`가 거의 항상 null입니다.
- JSON 버퍼 1536 B. 펌웨어 id: `safenest-esp32-sensor-node/1.7.4-mhz19b.1`

## 1.7.5 — 부분 TCP 패킷 스킵

UDP를 꺼도 TCP가 `wrote=97/808 errno=11`로 세션을 닫고, 실패 로그가 Serial mutex를 잡아 MR60 파서가 풀렸습니다.

- 한 바이트도 못 나가면 40 ms 후 그 스냅샷만 버리고 소켓은 유지.
- 이미 나간 부분 패킷만 200 ms 안에 끝내고, 실패 시 세션 종료.
- `[tcp-drop]`/`[tcp-send-fail]`/`[tcp-send-timeout]`는 1초에 한 줄.
- `u`로 UDP를 끄면 SPI capture도 같이 멈춤.
- 펌웨어 id: `safenest-esp32-sensor-node/1.7.5-mhz19b.1`

## 1.7.6 — TCP 원자 송신 + 로컬 mmWave 프레임 조립

1.7.5를 켠 부팅 로그에서 mmWave는 180 s 동안 `phase=nan` / `mmw_ok=0`이었고, TCP는 `wrote=186/794 errno=11`로 세션을 닫은 뒤 `connect_ms=1502 errno=119` 폭풍이 났습니다. thermal UDP는 기본이 켜져 있었습니다.

- 첫 `lwip_send`가 0 바이트면 재시도하지 않고 스냅샷만 건너뜀. 40 ms 동안 EAGAIN을 기다리다가 186 바이트만 나가는 경로를 없앰.
- thermal UDP/capture 기본 OFF. 시리얼 `u` 다음에 `c`.
- 부분 송신으로 소켓을 닫은 뒤 300 ms를 기다리고 다시 SYN.
- Seeed `fetch()`의 static `startFrame`을 쓰지 않음. UART2를 로컬 조립하고 100 ms 미완성 프레임은 버림. 5 s 동안 `mmw_ok=0`이면 `[mmw-rx]` hex dump.
- 펌웨어 id: `safenest-esp32-sensor-node/1.7.6-mhz19b.1`
- 스케치 폴더: `ESP32/Arduino/esp32_sensor_node_mhz19b_20260901-0431-junwoo/`

## 1.7.10 — 열화상 런타임 재초기화

1.7.9는 `initializeThermalCamera()`를 `setup()`에서만 호출한다. 부팅 때 MI48가 I2C에 없으면 `thermalStarted`가 false로 남고, 카메라가 나중에 켜져도 프레임이 0이다. 현장은 시리얼 입력을 전제로 하지 않는다.

- 루프가 2초마다 상태를 본다. 캡처가 7초 멈추거나 시작 실패면 GPIO25를 **먼저** 리셋하고 그다음 0x40/0x41을 본다. I2C ACK를 기다렸다가 리셋하면, 버스에서 빠진 MI48는 영원히 STALE이다.
- 캡처가 돌고 있으면 카메라를 건드리지 않는다.
- 시리얼 `t`는 디버그 강제 재init일 뿐이고, 복구 조건이 아니다.
- `c`/`u`로 끈 상태는 죽은 카메라로 보지 않는다.
- 펌웨어 id: `safenest-esp32-sensor-node/1.7.10-mhz19b.1`
- 스케치 폴더: `ESP32/Arduino/esp32_sensor_node/`

## 1.7.9 — C-B6 H150 예열 극성

1.7.8은 `co2_preheat`(예열 중 true)만 보냈다. Pi는 그 필드를 반전 없이 `co2_preheat_complete`로 써서, 예열이 끝나면 모델 입력이 영원히 0건이 된다.

- JSON에 `co2_preheat_complete`를 `!co2_preheat`로 보낸다. Pi는 canonical을 우선한다.
- `co2_preheat` 의미는 그대로(180 s 동안 true). Pi 코드는 안 바꾼다.
- 펌웨어 id: `safenest-esp32-sensor-node/1.7.9-mhz19b.1`
- 스케치 폴더: `ESP32/Arduino/esp32_sensor_node_mhz19b_20260901-1958-junwoo/`

## 1.7.8 — TCP 다운 시 thermal capture 정지 해제

1.7.7은 `tcpLinkHealthy`가 false면 SPI capture까지 멈췄다. UDP send는 이미 `TCP_CRITICAL_BIT`에 양보하므로, 재연결 동안 capture를 끄면 Pi thermal만 STALE이 되고 TCP는 그대로 흔들렸다.

- `captureThermalIfReady()`에서 `tcpLinkHealthy` 가드를 뺀다. UDP가 꺼져 있으면 (`u`) SPI는 여전히 안 한다.
- thermal UDP/capture 기본 ON. 시리얼 `u`/`c`로 끌 수 있다.
- UDP send의 `tcpLinkHealthy` defer와 `yieldRadioToTcp`는 그대로. 캡처만 계속하고, 세션이 내려가 있으면 송신은 미룬다.
- 펌웨어 id: `safenest-esp32-sensor-node/1.7.8-mhz19b.1`
- 스케치 폴더: `ESP32/Arduino/esp32_sensor_node_mhz19b_20260901-1921-junwoo/`

## 1.7.7 — AP 업링크 TCP 유지

SafeNest-ESP에서 Pi가 ESP를 −80 dBm으로 듣고 ARP/SYN이 깨진 뒤 `connect_ms=1503 errno=119` 폭풍이 났습니다. 리셋만으로는 같은 패턴이 반복됩니다.

- STA TX를 `WIFI_POWER_19_5dBm`으로 고정. Pi가 ESP를 듣게 하는 방향.
- connect timeout 1.5 s → 4 s. 실패 후 2.5 s 대기. 5연속 실패 시 `WiFi.reconnect()`.
- 부분 송신 후 재SYN 대기를 300 ms → 2 s. SYN-RECV 적체 완화.
- 펌웨어 id: `safenest-esp32-sensor-node/1.7.7-mhz19b.1`

