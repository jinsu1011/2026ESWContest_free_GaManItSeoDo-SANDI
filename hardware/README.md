# hardware

SafeNest 장치의 물리 하드웨어 설계 자산을 보관한다.

## 목적

센서 배치와 시야각 제약을 만족하는 외함을 제3자가 그대로 재현할 수 있도록,
출력 가능한 3D 모델과 치수 사양을 함께 둔다.

## 구성

| 경로 | 내용 |
|---|---|
| [`3d_models/`](3d_models/) | 센서 노드 외함과 LCD·부저 하우징 STL 4종 + 설계 사양 |

세부 치수·개구부·출력 조건은 [`3d_models/README.md`](3d_models/README.md) 참고.

## SafeNest 시스템과의 관계

| 하우징 요소 | 대응 구성 |
|---|---|
| 전면 개구부 (PIR / Thermal / CO₂ / mmWave) | ESP32 센서 노드에 연결된 4개 센서 |
| LCD 개구부 | 백엔드가 `:8000/display` 로 서빙하는 현장 LCD 패널 |
| 부저 그릴 | 위험 확정 시 동작하는 경보 부저 |
| 측면 팬/공기 개구부, 후면 환기 슬롯 | MH-Z19B 주변 공기 유통 |

센서 핀 배치와 배선은 [`ESP32/docs/ARDUINO_ENVIRONMENT_SETUP_KO.md`](../ESP32/docs/ARDUINO_ENVIRONMENT_SETUP_KO.md) 와 일치해야 한다.

## 포함 / 비포함 기준

포함: 출처가 확인된 STL·CAD, 치수 사양, 출력 조건, 조립 주의사항.
비포함: 펌웨어·코드, 슬라이서 G-code 및 캐시, 출처 불명 외부 모델, 개인 프린터 프로파일.

## 출처

이 디렉터리의 3D 모델과 설계 사양은 모두 SafeNest 팀이 직접 설계·제작했다.
외부에서 가져온 자산은 없다.
