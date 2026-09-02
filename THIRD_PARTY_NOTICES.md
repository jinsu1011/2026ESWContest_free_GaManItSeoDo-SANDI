# Third-Party Notices

SafeNest가 사용하는 외부 라이브러리·모델·데이터셋의 출처와 이용 조건이다.

라이선스 식별자는 **저장소 안에 근거 기록이 있는 경우에만** 적었다.
근거가 없는 항목은 임의로 라이선스를 지정하지 않고 `확인 필요`로 남겼으며,
재배포 여부가 불확실한 원본 데이터는 이 저장소에 포함하지 않았다.

---

## 1. ESP32 펌웨어가 사용하는 Arduino 라이브러리

정본 스케치
`ESP32/Arduino/esp32_sensor_node/esp32_sensor_node.ino`
가 `#include` 하는 외부 라이브러리다. 소스는 저장소에 포함하지 않으며,
Arduino IDE / `arduino-cli` 로 각자 설치한다.

| 라이브러리 | 제공 | 용도 | 검증 버전 | 출처 | 라이선스 |
|---|---|---|---|---|---|
| Seeed Arduino mmWave (`Seeed_Arduino_mmWave.h`) | Seeed Studio | MR60BHA2 mmWave 레이더 파싱 | 안정 계열 `1.0.0` | https://github.com/Seeed-Projects/Seeed-mmWave-library | 확인 필요 — 상단 저장소 참조 |
| Adafruit NeoPixel | Adafruit | 위 라이브러리의 하위 의존성 | Seeed 라이브러리 요구 버전 | https://github.com/adafruit/Adafruit_NeoPixel | 확인 필요 — 상단 저장소 참조 |
| hp_BH1750 | Stefan Armborst | 위 라이브러리의 하위 의존성 | Seeed 라이브러리 요구 버전 | https://github.com/Starmbi/hp_BH1750 | 확인 필요 — 상단 저장소 참조 |
| Arduino ESP32 core (`WiFi`, `WiFiUdp`, `Wire`, `SPI`, FreeRTOS, lwIP) | Espressif | 보드 런타임 | ESP32 Dev Module 대응 core | https://github.com/espressif/arduino-esp32 | 확인 필요 — 상단 저장소 참조 |

설치 절차는 `ESP32/docs/ARDUINO_ENVIRONMENT_SETUP_KO.md` 에 있다.

## 2. 하드웨어 (센서 · 보드)

| 부품 | 제조사 | 역할 | 문서 |
|---|---|---|---|
| ESP-WROOM-32 (ESP32 Dev Module) | Espressif | 센서 노드 MCU | — |
| MR60BHA2 60 GHz mmWave | Seeed Studio | 호흡/심박/재실 | https://wiki.seeedstudio.com/getting_started_with_mr60bha2_mmwave_kit/ |
| MH-Z19B NDIR CO₂ | Winsen | CO₂ 농도 | — |
| MI48xx 기반 Thermal Camera HAT (80×62) | Waveshare / Meridian Innovation | 열화상 프레임 | — |
| PIR 모션 센서 | — | 움직임 감지 | — |
| Raspberry Pi 5 | Raspberry Pi Ltd. | 게이트웨이 · 온디바이스 AI · 백엔드 | — |

## 3. Raspberry Pi Python 의존성

설치 목록은 `RaspberryPi/Runtime/requirements-backend.txt`,
`RaspberryPi/Ondevice_AI/requirements-pi.txt` 에 있다.
PyPI 배포본을 그대로 설치하며 소스는 저장소에 포함하지 않는다.
각 패키지의 라이선스는 배포처 메타데이터를 따른다.

| 패키지 | 용도 |
|---|---|
| `fastapi`, `uvicorn[standard]` | HTTP/WebSocket 백엔드 |
| `qrcode[pil]` | 게스트 대시보드 QR 생성 |
| `piper-tts` | 로컬 한국어 TTS 음성 안내 |
| `ai-edge-litert` | TFLite 인터프리터 (Thermal · CO₂ 모델) |
| `torch` | mmWave B23 PyTorch float32 모델 |
| `numpy`, `scipy`, `pandas`, `scikit-learn`, `joblib`, `matplotlib`, `Pillow` | 전처리·수치 연산 |
| `spidev`, `smbus2` | Raspberry Pi SPI/I²C |

`torch` 는 반드시 공식 CPU wheel index
(`https://download.pytorch.org/whl/cpu`) 를 함께 사용한다.
일반 PyPI 의 ARM64 배포본은 Raspberry Pi 에 없는 CUDA/NVIDIA 의존성을 끌어온다.

## 4. TTS 음성 모델

| 항목 | 값 |
|---|---|
| 음성 | `ko_KR-kss-medium` (Piper) |
| 취득 | `./run_safenest.sh --install` 이 `piper.download_voices` 로 내려받음 |
| 저장 위치 | `RaspberryPi/Runtime/data/tts/` — **Git 미추적** |
| 라이선스 | 음성 데이터셋 **CC BY-NC-SA 4.0** (저장소 내 `RaspberryPi/Runtime/deployment/run_pi.sh` 주석에 기록) |

비상업적 조건이 있어 모델 바이너리를 저장소에 포함하지 않는다.

## 5. 온디바이스 AI 모델의 학습 데이터 출처

학습에 사용한 원본 데이터셋은 **이 저장소에 포함하지 않는다.**
배포되는 것은 학습 결과 아티팩트(`.tflite` / `.pt`)와 계약 메타데이터뿐이다.

### 5.1 Thermal — 활성 모델 `thermal_public_sdt_fp32_active`

| 항목 | 값 |
|---|---|
| 데이터셋 | SDT Dataset (Simulated/real thermal-depth posture dataset, TU Wien CVL) |
| 공식 문서 | https://cvl.tuwien.ac.at/research/cvl-databases/sdt-icip/ |
| 배포 기록 | https://zenodo.org/records/4124309 (doi:10.5281/zenodo.4124309) |
| 원 라벨 | `LYING`, `SITTING`, `STANDING`, `EMPTY_ROOM` |
| 이용 조건 | **비상업적 연구 목적 + 출처 표시**. 원 배포처 조건을 따른다 |
| 저장소 포함 여부 | 원본 아카이브 미포함 (모델 아티팩트만 포함) |

SafeNest 3-class(`NOT_HUMAN` / `HUMAN_NORMAL` / `HUMAN_FALL_PROXY`)는
SDT 자세 라벨에서 파생한 **proxy** 매핑이며, 실제 낙상 이벤트 라벨이 아니다.

### 5.2 CO₂ — 활성 모델 `co2_occupancy_c_b6`

| 항목 | 값 |
|---|---|
| 데이터셋 | UCI Occupancy Detection Dataset |
| 공식 출처 | https://archive.ics.uci.edu/dataset/357/occupancy+detection |
| 원 논문 | Candanedo, L. M., & Feldheim, V. (2016). *Accurate occupancy detection of an office room from light, temperature, humidity and CO2 measurements using statistical learning models.* Energy and Buildings, 112, 28–39 |
| 이용 조건 | UCI Machine Learning Repository 배포 조건 — 원 출처 확인 필요 |
| 저장소 포함 여부 | 원본 아카이브 미포함 (모델 아티팩트만 포함) |

의미 범위는 **실내 재실(occupancy) 판정**이며, 질식·유해가스 ground truth가 아니다.

### 5.3 mmWave — 활성 모델 `mmwave` (M-PROT-B23)

| 항목 | 값 |
|---|---|
| 데이터 | 팀이 MR60BHA2 로 직접 수집한 `breath_phase` 파형 |
| 외부 데이터셋 | 사용하지 않음 |
| 저장소 포함 여부 | 원 캡처 로그 미포함 (모델 아티팩트 + scaler 통계만 포함) |

## 6. 웹 프론트엔드에 포함한 외부 자산

관리자 화면의 추이 그래프에 사용하는 Chart.js 를 저장소에 포함(vendoring)했다.
외부 인터넷이 없는 현장에서도 동작해야 하기 때문이다.

| 항목 | 값 |
|---|---|
| 구성요소 | **Chart.js** |
| 버전 | **4.5.1** (고정) |
| 포함 경로 | `RaspberryPi/Web/vendor/chart.js/chart.umd.min.js` |
| 서빙 경로 | `/vendor/chart.js/chart.umd.min.js` (백엔드 same-origin) |
| 크기 · SHA-256 | 208,522 bytes · `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
| 취득처 | `https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js` (공식 npm 배포본) |
| 저작권 | (c) Chart.js Contributors — https://www.chartjs.org |
| **라이선스** | **MIT** — 전문은 `RaspberryPi/Web/vendor/chart.js/LICENSE.md` |

번들에 포함된 의존성:

| 구성요소 | 버전 | 저작권 | 라이선스 |
|---|---|---|---|
| @kurkle/color | 0.3.2 | (c) 2023 Jukka Kurkela | MIT (배포본 배너에 명시) |

배포본은 수정하지 않고 원본 바이트 그대로 포함했다. 위 SHA-256 은 취득 시점의
공식 배포본과 일치하며, `.gitattributes` 에서 줄바꿈 정규화를 제외해 바이트
동일성을 유지한다.

그 외 웹·LCD 자산(HTML/CSS/JS, 아이콘, 폰트)은 모두 SafeNest 팀이 작성했고
외부 호스트를 참조하지 않는다. 현재 제출본의 웹·LCD 화면은 **외부 네트워크
자산을 하나도 참조하지 않는다.**

## 7. 이 저장소 자체의 라이선스

프로젝트 라이선스는 **아직 지정되지 않았다.** 별도로 결정한다.
라이선스가 없는 동안 위 3항의 외부 구성요소 조건은 그대로 유효하며,
5항 데이터셋의 비상업적/출처표시 조건은 어떤 경우에도 지켜야 한다.
