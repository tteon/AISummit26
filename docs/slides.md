# 발표 차트 — 2장 체제

슬라이드에 올라가는 차트는 두 장. 나머지 figures는 리포/README의 근거 자료로 남고,
질문이 나오면 백업으로 띄운다.

---

## 1 · 총체 — `figures/overview-by-question.svg`

**문장: 실험 전체가 이 한 장이다 — 13개 질문 × SF1→100 × 7개 설계, 819 에피소드.
스케일이 커질수록 설계가 갈라지고, 마커 채움이 정답 여부를 같이 보여준다.**

- 세로축 median db hits(로그) — 박스 위 다른 부하에 영향받지 않는 유일한 비용 단위.
- 마커: 채움 = 3반복 전부 정답, 빈 것 = 전부 오답, 회색 = 일부.
- 읽는 순서: ① easy 패널들 — 설계가 포개짐(무엇을 알려주든 무관) → ② ext_hard/int_med —
  라벨-only(주황 ○)가 위로 뜨고 속이 빔 → ③ blind(연보라 +)의 평평하고 속 빈 선 —
  제일 싸게, 모든 스케일에서 틀림 → ④ int_hard_2의 in-context 선들 10⁸ —
  집계 금지의 최상단 가격.
- 라벨(범례)이 곧 스토리: 1–4는 DB가 집계, 5–7은 모델이 집계. 색+마커 모양으로 7개
  설계 구분(CVD 검증 통과).
- 수치는 전부 렌더 시점에 `results/agent_interaction.json`에서 직접 계산 — 하드코딩 없음
  (`python scripts/check_chart_provenance.py`가 검증).

## 2 · 엔지니어링 디테일 — `figures/engineering-detail.svg`

**문장: 행 하나가 모델에 닿기까지 네 군데서 과금된다 — 인코딩, 트랜스포트, 런타임,
동시성. 전부 실측이다.**

4패널, 각각의 숫자:

| 패널 | 수치 | 말할 포인트 |
|---|---|---|
| Encoding | 같은 200행: JSON 9,017 vs CSV 5,211 토큰 | 데이터는 ~2,100토큰뿐, 나머지는 행마다 반복되는 키 |
| Transport | 10만 행 생산·50행 소비: HTTP 398ms vs Bolt 12ms vs LIMIT 1.4ms | 276× 격차를 닫는 건 transport가 아니라 쿼리 속 한 구절 |
| Runtime | 행당 CPU: 소비 20.8µs vs 생산 2.9µs (7×), 346B/row는 양 빌드 동일 | 비용은 코덱이 아니라 표현(PyObject) |
| Concurrency | 8워커 p50: 스레드 769 → 프로세스 81 → 네이티브 7.7ms | 천장은 하드웨어가 아니라 GIL; 결론은 Python control plane + native data plane |

- 근거 요청 시: 반복별 원시 샘플과 머신 매니페스트가 `results/bench/`에, 에피소드
  819건이 `results/agent_interaction.json`에 있다.

---

## 백업 (Q&A용 — 리포엔 위 2장만 커밋, 아래는 필요할 때 재생성)

| 재생성 명령 | 나오는 차트 | 방어하는 질문 |
|---|---|---|
| `python scripts/dump_conditions.py` | conditions 매트릭스 | "정확히 한 가지만 다르다"는 설계 주장 |
| `python scripts/plot_interaction.py` | p99 by difficulty/question, 정확도·비용 | 스케일 축 p99 원결과 (replay 기반) |
| `python scripts/plot_in_context.py` | outcomes (71 vs 11), 질문별 db hits | 필드 하나의 인과, 트리오 스케일 |
| `python scripts/plot_depth.py` | 인코딩/runaway/CPU/스케일링 각 확대판 | engineering-detail 패널별 심화 |
| `python scripts/plot_levers.py` | 레버 8개 2-블록 요약 | "수리는 두 종류" 클로징 멘트용 |

**수치 검증**: `python scripts/check_chart_provenance.py` — 두 차트의 모든 상수를
`results/bench/*.json`·`results/agent_interaction.json`과 대조 (실제로 1차 rust 실행값
2건이 기록값과 어긋난 걸 잡아내 교정했음).

## 한 줄 아크

그래프가 100배 커지면 에이전트 설계가 갈라진다 → 갈라짐의 절반은 모델이 아는 것(계약),
나머지 절반은 행이 모델까지 오는 길(런타임) → 수리는 두 종류이고 둘 다 필요하다.
