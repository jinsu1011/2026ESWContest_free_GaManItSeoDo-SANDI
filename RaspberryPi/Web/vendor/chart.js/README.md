# Chart.js (vendored)

관리자 화면(`/admin`)의 위험도·CO₂ 추이 그래프에 사용한다. 이전에는 CDN 에서
불러왔으나, 외부 인터넷이 없는 현장에서도 동작하도록 저장소에 포함했다.

| 항목 | 값 |
|---|---|
| 구성요소 | Chart.js |
| 버전 | **4.5.1** (고정) |
| 파일 | `chart.umd.min.js` (UMD, `window.Chart` 전역 제공) |
| 크기 | 208522 bytes |
| SHA-256 | `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
| 취득처 | `https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js` (공식 npm 배포본) |
| 라이선스 | MIT — 전문은 `LICENSE.md` |
| 포함 의존성 | @kurkle/color 0.3.2 (MIT, 번들에 포함) |

이 파일은 이전에 참조하던 무버전 CDN(`https://cdn.jsdelivr.net/npm/chart.js`)이
제공하던 바이트와 **동일**하다. 따라서 이번 vendoring 으로 프런트엔드 동작이
달라지지 않는다.

원본 배포본을 수정하지 않는다. 버전을 올릴 때는 위 표의 버전·SHA-256·취득처를
함께 갱신하고 `THIRD_PARTY_NOTICES.md` 도 같이 고친다.
