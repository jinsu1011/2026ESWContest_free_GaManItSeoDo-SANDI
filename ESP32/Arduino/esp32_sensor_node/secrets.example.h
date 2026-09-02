#pragma once

constexpr char WIFI_SSID[] = "YOUR_2_4_GHZ_WIFI_SSID";
constexpr char WIFI_PASSWORD[] = "YOUR_WIFI_PASSWORD";
// RPI_HOST 는 예시 값이다. 반드시 실제 Raspberry Pi 의 WLAN IPv4 주소로 바꾼다.
constexpr char RPI_HOST[] = "192.168.1.44";
constexpr uint16_t RPI_PORT = 9000;

// 사용법: 이 파일을 같은 폴더에 secrets.h 로 복사한 뒤 실제 값을 입력한다.
//   cp secrets.example.h secrets.h
// secrets.h 는 .gitignore(ESP32/Arduino/**/secrets.h)로 추적되지 않는다.
// RPI_HOST 는 Raspberry Pi 의 WLAN IPv4 주소이며, TCP telemetry(RPI_PORT)와
// Thermal UDP(스케치 상수 THERMAL_UDP_PORT = 5005)가 모두 이 주소로 전송된다.
