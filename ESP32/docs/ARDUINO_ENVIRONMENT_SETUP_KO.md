# SafeNest ESP32 Arduino 개발 환경 구축 가이드

이 문서는 다른 PC에서도 SafeNest ESP32 통합 센서 펌웨어를 빌드하고 ESP32에 업로드할 수 있도록 필요한 프로그램, 보드 패키지, Arduino 라이브러리, 설정 파일과 실행 절차를 정리한 문서입니다.

대상 스케치:

```text
ESP32/Arduino/esp32_sensor_node/esp32_sensor_node.ino
```

이 스케치가 실제 ESP32에 올라가는 **정본(canonical) 펌웨어**입니다. 저장소에는 이 스케치 하나만 있습니다.

> 이 프로젝트의 기준 보드는 일반적인 ESP32-WROOM 계열의 `ESP32 Dev Module`입니다. XIAO ESP32C6용 보드 설정이 아닙니다. 소스의 핀 번호도 `ESP32 Dev Module` 기준이므로 다른 보드로 바꾸면 배선과 핀 정의를 함께 수정해야 합니다.

CO₂는 Sensirion SCD4x가 아니라 **Winsen MH-Z19B UART**를 사용합니다. 따라서 SCD4x 라이브러리가 필요 없습니다. MR60은 UART2(GPIO 16/17), MH-Z19B는 UART1(GPIO 32/33)을 씁니다. MH-Z19B 모듈 전원은 4.5–5.5 V이며 ESP32 3.3 V 레일로 켜지 않습니다. 변경 이력과 배선 상세는 `ESP32/Arduino/esp32_sensor_node/ESP32.md`를 봅니다.

## 1. 준비물

- ESP32 Dev Module(ESP32-WROOM 계열)
- 데이터 통신이 가능한 USB 케이블
- MR60BHA2 60 GHz mmWave 센서
- Winsen MH-Z19B NDIR CO₂ 센서 (별도 4.5–5.5 V 전원 필요)
- PIR 센서
- Waveshare Thermal Camera HAT / MI48xx(80 × 62) 열화상 센서
- Raspberry Pi와 ESP32가 함께 접속할 수 있는 **2.4 GHz Wi-Fi**
- Windows, macOS 또는 Linux PC

ESP32는 저속 센서 데이터를 Raspberry Pi의 TCP `9000` 포트로, 열화상 데이터를 UDP `5005` 포트로 전송합니다. 펌웨어만 단독으로 업로드할 수는 있지만 전체 시스템 동작을 확인하려면 Raspberry Pi 수신 프로그램이 먼저 실행 중이어야 합니다.

## 2. 설치해야 하는 프로그램과 패키지

### 2.1 Arduino IDE

[Arduino IDE 공식 다운로드](https://www.arduino.cc/en/software/)에서 Arduino IDE 2.x의 최신 안정 버전을 설치합니다.

CLI 기반 설치를 원하는 경우에는 [Arduino CLI](https://arduino.github.io/arduino-cli/latest/installation/)를 대신 사용할 수 있습니다. GUI 절차는 3절, CLI 절차는 8절을 따릅니다.

### 2.2 ESP32 보드 패키지

Arduino IDE에서 다음 보드 매니저 URL을 등록합니다.

```text
https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

설치 대상:

| 구분 | 설치/선택 항목 | 비고 |
|---|---|---|
| 보드 패키지 | `esp32` by Espressif Systems | 안정 버전 사용 |
| 보드 | `ESP32 Dev Module` | Arduino FQBN: `esp32:esp32:esp32` |
| 시리얼 속도 | `115200` baud | 업로드 후 Serial Monitor 설정 |

설치 순서:

1. Arduino IDE에서 `File > Preferences`를 엽니다.
2. `Additional boards manager URLs`에 위 URL을 추가합니다.
3. `Tools > Board > Boards Manager`를 엽니다.
4. `esp32`를 검색하고 **Espressif Systems**가 제공하는 패키지를 설치합니다.
5. `Tools > Board > esp32 > ESP32 Dev Module`을 선택합니다.

이 스케치에서 사용하는 `Arduino.h`, `WiFi.h`, `WiFiUdp.h`, `Wire.h`, `SPI.h`, FreeRTOS API는 ESP32 보드 패키지에 포함되므로 별도 라이브러리로 설치하지 않습니다.

### 2.3 외부 Arduino 라이브러리

정본 스케치의 직접 외부 의존성은 하나입니다. CO₂는 MH-Z19B UART를 스케치 안에서 직접 처리하므로 별도 라이브러리가 없습니다.

| Library Manager 검색 이름 | 제공자 | 필요한 헤더 | 권장 버전/주의 사항 |
|---|---|---|---|
| `Seeed Arduino mmWave` | Seeed Studio | `Seeed_Arduino_mmWave.h` | 검증 기준 안정 계열 `1.0.0`; MR60BHA2 지원 필요 |

함께 설치되는 의존성:

| 상위 라이브러리 | 하위 의존성 | 처리 방법 |
|---|---|---|
| Seeed Arduino mmWave | `Adafruit NeoPixel`, `hp_BH1750` | 설치 창에서 `Install all` 선택 |

Arduino IDE에서 설치하는 방법:

1. 왼쪽의 `Library Manager` 아이콘을 누르거나 `Sketch > Include Library > Manage Libraries...`를 엽니다.
2. `Seeed Arduino mmWave`를 검색해 Seeed Studio 제공 라이브러리를 설치합니다.
3. 의존성 설치 여부를 물으면 `Install all`을 선택합니다.
4. 설치가 끝나면 Arduino IDE를 한 번 재시작합니다.

Seeed 라이브러리가 Library Manager에서 검색되지 않으면 [Seeed mmWave 공식 저장소](https://github.com/Seeed-Projects/Seeed-mmWave-library)의 안정 릴리스 ZIP을 내려받아 `Sketch > Include Library > Add .ZIP Library...`로 설치합니다. ZIP으로 수동 설치할 때는 위 표의 하위 의존성도 Library Manager에서 따로 설치합니다. 프리릴리스나 개발 브랜치는 API 호환성을 다시 확인하기 전까지 사용하지 않는 것이 안전합니다.


## 3. 프로젝트 받기

Git을 사용하는 방법:

```bash
git clone https://github.com/jinsu1011/safenest-embedded-competition-bridege.git
cd safenest-embedded-competition
```

Git을 사용하지 않으면 GitHub의 `Code > Download ZIP`으로 받은 뒤 압축을 풉니다. `.ino` 파일 하나만 전달하지 말고 최소한 다음 구조를 유지해야 합니다.

```text
<repository>/
└── ESP32/
    └── Arduino/
        └── esp32_sensor_node/
            ├── esp32_sensor_node.ino
            ├── secrets.example.h
            └── secrets.h            # 사용자가 생성, Git에 올리지 않음
```

Arduino 스케치 폴더 이름과 `.ino` 파일의 기본 이름은 모두 `esp32_sensor_node`로 같아야 합니다.

## 4. 개인 설정 파일 만들기

Wi-Fi 비밀번호 같은 개인정보는 소스에 직접 적지 않고 스케치 폴더의 `secrets.h`에 둡니다. 이 파일은 `.gitignore`에 등록되어 Git에 커밋되지 않습니다.

Windows PowerShell:

```powershell
Copy-Item .\ESP32\Arduino\esp32_sensor_node\secrets.example.h .\ESP32\Arduino\esp32_sensor_node\secrets.h
```

macOS/Linux:

```bash
cp ESP32/Arduino/esp32_sensor_node/secrets.example.h ESP32/Arduino/esp32_sensor_node/secrets.h
```

생성한 `secrets.h`를 다음과 같이 수정합니다.

```cpp
#pragma once

constexpr char WIFI_SSID[] = "2.4_GHz_Wi-Fi_이름";
constexpr char WIFI_PASSWORD[] = "Wi-Fi_비밀번호";
constexpr char RPI_HOST[] = "192.168.1.44";  // Raspberry Pi의 실제 IPv4 주소
constexpr uint16_t RPI_PORT = 9000;
```

주의 사항:

- ESP32는 이 구성에서 2.4 GHz Wi-Fi를 사용합니다.
- `RPI_HOST`에는 `localhost`가 아니라 ESP32에서 접근할 수 있는 Raspberry Pi의 LAN IPv4 주소를 넣습니다.
- `RPI_PORT`는 Raspberry Pi 수신 서버의 TCP 포트 `9000`과 같아야 합니다.
- 열화상 UDP 포트 `5005`는 현재 `.ino`의 `THERMAL_UDP_PORT`에 정의되어 있습니다.
- 실제 `secrets.h`를 메신저, 문서 또는 Git으로 공유하지 마십시오. 다른 사용자는 예제 파일에서 자신의 값을 만들어야 합니다.

## 5. 배선 확인

현재 스케치에 고정된 ESP32 핀은 다음과 같습니다.

| 장치/신호 | ESP32 핀 | 연결 방향 또는 설명 |
|---|---:|---|
| 공통 I²C SDA | GPIO 21 | Thermal 제어 버스 |
| 공통 I²C SCL | GPIO 22 | Thermal 제어 버스 |
| PIR OUT | GPIO 13 | ESP32 디지털 입력 |
| MH-Z19B TX | GPIO 32 | 센서 TX → ESP32 RX (UART1) |
| MH-Z19B RX | GPIO 33 | 센서 RX ← ESP32 TX (UART1) |
| MR60BHA2 TX | GPIO 16 | 센서 TX → ESP32 RX |
| MR60BHA2 RX | GPIO 17 | 센서 RX ← ESP32 TX |
| Thermal SCLK | GPIO 18 | SPI clock |
| Thermal MISO | GPIO 19 | 센서 → ESP32 |
| Thermal MOSI | GPIO 23 | ESP32 → 센서 |
| Thermal CS | GPIO 27 | SPI chip select |
| Thermal READY | GPIO 26 | ESP32 입력 |
| Thermal RESET | GPIO 25 | ESP32 출력 |

모든 모듈과 ESP32의 GND를 공통으로 연결합니다. 센서 전원 전압과 핀 허용 전압은 반드시 각 센서의 데이터시트 및 사용 중인 모듈 보드 사양을 확인하십시오. 상세 통신 규약은 [COMMUNICATION_PROTOCOL.md](./COMMUNICATION_PROTOCOL.md)를 참고합니다.

## 6. 컴파일 및 업로드

1. Arduino IDE에서 `ESP32/Arduino/esp32_sensor_node/esp32_sensor_node.ino`를 엽니다.
2. `Tools > Board`에서 `ESP32 Dev Module`을 선택합니다.
3. `Tools > Port`에서 ESP32가 연결된 포트를 선택합니다.
4. 먼저 `Sketch > Verify/Compile`로 컴파일합니다.
5. 오류가 없으면 `Upload`를 눌러 플래시합니다.
6. 업로드 후 `Tools > Serial Monitor`를 열고 `115200 baud`를 선택합니다.

ESP32가 포트 목록에 나타나지 않으면 다음을 확인합니다.

- 충전 전용이 아닌 데이터 USB 케이블인지 확인
- 다른 USB 포트 사용
- 보드에 장착된 USB-UART 칩에 따라 CP210x 또는 CH340 드라이버 설치
- Windows 장치 관리자 또는 macOS/Linux의 직렬 장치 목록에서 포트 확인
- 업로드 연결 중 필요하면 보드의 `BOOT` 버튼을 누른 상태에서 업로드 시작

## 7. 정상 동작 확인

업로드 직후 Serial Monitor에서 다음 흐름을 확인합니다.

```text
SafeNest ESP32 sensor node starting
[identity] device=esp32-01 ...
[wifi] connecting to ... asynchronously
```

이후 10초마다 출력되는 `[health]` 로그에서 다음 항목을 확인합니다.

| 항목 | 정상 예시 | 의미 |
|---|---|---|
| `wifi` | `up` | Wi-Fi 연결 성공 |
| `rpi` | 설정한 Raspberry Pi IP | 전송 목적지 |
| `co2` | 0이 아닌 측정값 | MH-Z19B 측정 수신 |
| `resp`, `heart` | 사람이 감지될 때 값 출력 | MR60BHA2 데이터 수신 |
| `thermal_frames` | 계속 증가 | 열화상 프레임 캡처 진행 |
| `udp_sent` | 계속 증가 | Raspberry Pi로 UDP 전송 진행 |
| `udp_failed` | 지속 증가하지 않음 | UDP 전송 오류 여부 |

Raspberry Pi 쪽에서도 TCP `9000`과 UDP `5005`가 수신 대기 중이어야 합니다. PC 또는 Raspberry Pi 방화벽이 이 통신을 막지 않는지 확인합니다.

## 8. 선택 사항: Arduino CLI로 동일 환경 만들기

다음 명령은 Arduino CLI가 설치되어 있고 터미널에서 `arduino-cli`를 실행할 수 있다는 전제입니다.

```bash
arduino-cli config init
arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli lib install "Seeed Arduino mmWave"
```

컴파일:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 ESP32/Arduino/esp32_sensor_node
```

업로드 예시:

```bash
arduino-cli upload --fqbn esp32:esp32:esp32 --port COM5 ESP32/Arduino/esp32_sensor_node
```

macOS/Linux에서는 `COM5` 대신 실제 포트(예: `/dev/cu.usbserial-...`, `/dev/ttyUSB0`)를 넣습니다. 포트는 다음 명령으로 확인할 수 있습니다.

```bash
arduino-cli board list
```

## 9. 재현성을 위한 버전 기록

모든 팀원이 완전히 같은 결과를 얻어야 한다면 “최신 버전”만 사용하지 말고, 최초로 정상 컴파일·실기 동작을 확인한 뒤 아래 버전을 기록해 공유합니다.

```bash
arduino-cli version
arduino-cli core list
arduino-cli lib list
```

권장 기록표:

| 구성 요소 | 프로젝트 요구 조건 | 팀 검증 버전 기입란 |
|---|---|---|
| Arduino IDE 또는 CLI | IDE 2.x 또는 호환 CLI | `____________` |
| ESP32 by Espressif Systems | 안정 버전, `ESP32 Dev Module` 지원 | `____________` |
| Seeed Arduino mmWave | 안정 계열 `1.0.0` | `____________` |
| Adafruit NeoPixel | Seeed 라이브러리가 요구하는 버전 | `____________` |
| hp_BH1750 | Seeed 라이브러리가 요구하는 버전 | `____________` |

버전을 고정한 CLI 설치 예시는 다음 형식입니다.

```bash
arduino-cli core install esp32:esp32@<검증한_버전>
arduino-cli lib install "Seeed Arduino mmWave@1.0.0"
```

## 10. 자주 발생하는 오류

| 오류/증상 | 원인 | 해결 방법 |
|---|---|---|
| `Seeed_Arduino_mmWave.h: No such file or directory` | 다른 24 GHz radar 라이브러리를 설치했거나 라이브러리 누락 | 정확히 `Seeed Arduino mmWave` 설치 |
| `secrets.h: No such file or directory` | 개인 설정 파일 미생성 | 4절의 명령으로 예제 파일을 복사 |
| 여러 라이브러리가 발견되었다는 메시지 | 이름이 비슷한 구형/복제 라이브러리가 함께 설치됨 | Arduino libraries 폴더에서 중복을 제거하고 정확한 제공자 버전만 유지 |
| 포트가 보이지 않음 | USB 케이블 또는 USB-UART 드라이버 문제 | 데이터 케이블, CP210x/CH340 드라이버, 다른 USB 포트 확인 |
| 업로드 중 연결 실패 | 자동 부트 진입 실패 또는 포트 점유 | Serial Monitor를 닫고 재시도, 필요하면 `BOOT` 버튼 사용 |
| Wi-Fi 연결 실패 | 5 GHz 전용 SSID, 잘못된 비밀번호 | 2.4 GHz SSID와 `secrets.h` 재확인 |
| `wifi=up`인데 Pi에 데이터가 없음 | Pi IP/포트/방화벽 또는 수신 서버 문제 | `RPI_HOST`, TCP 9000, UDP 5005, Pi 실행 상태와 방화벽 확인 |
| 센서 값만 비어 있음 | 배선, 전원, RX/TX 방향 또는 센서 펌웨어 문제 | 5절 배선과 공통 GND 확인; MR60BHA2는 필요 시 공식 안내에 따라 펌웨어 확인 |

## 11. 다른 사람에게 전달할 때 체크리스트

- [ ] 저장소 전체 또는 최소 ESP32 폴더 구조를 전달했다.
- [ ] 실제 `secrets.h`가 아니라 `secrets.example.h`만 전달했다.
- [ ] 사용 보드가 `ESP32 Dev Module`임을 알렸다.
- [ ] ESP32 보드 패키지와 `Seeed Arduino mmWave` 라이브러리의 설치를 안내했다.
- [ ] 라이브러리 설치 시 `Install all`로 하위 의존성을 설치했다.
- [ ] 검증에 사용한 IDE, ESP32 core, 라이브러리 버전을 기록했다.
- [ ] 2.4 GHz Wi-Fi 및 Raspberry Pi IP를 각 사용자 환경에 맞게 설정했다.
- [ ] TCP 9000, UDP 5005 포트와 방화벽을 확인했다.
- [ ] 업로드 후 Serial Monitor를 `115200 baud`로 확인했다.

## 공식 참고 자료

- [Espressif Arduino-ESP32 설치 문서](https://docs.espressif.com/projects/arduino-esp32/en/latest/installing.html)
- [Seeed Arduino mmWave 라이브러리](https://github.com/Seeed-Projects/Seeed-mmWave-library)
- [Seeed MR60BHA2 시작 가이드](https://wiki.seeedstudio.com/getting_started_with_mr60bha2_mmwave_kit/)
- [SafeNest ESP32 통신 규약](./COMMUNICATION_PROTOCOL.md)
