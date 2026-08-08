# 발표 차트 — 2장 체제

슬라이드에 올라가는 차트는 두 장. 나머지 figures는 리포/README의 근거 자료로 남고,
질문이 나오면 백업으로 띄운다.

---

## 1 · 총체 — `figures/levers.svg`

**문장: 측정한 레버 8개, 수리는 두 종류다 — 계약(ontology/contract)을 고치는 것과
런타임(engineering/runtime)을 고치는 것. 어느 쪽도 서로를 대체하지 못한다.**

- 위 블록(보라): 모델에게 말해주거나 쓰도록 요구하는 것 — LIMIT 계약 ×276, 플랜 피드백
  ×10, 프롬프트 온톨로지 ×9, `more_available` ×6.5.
- 아래 블록(주황): 밑에서 실행되는 것 — 네이티브 드라이버 ×100, 프로세스 분리 ×9.5,
  CSV 인코딩 ×3, rust 코덱 ×1.3.
- 블록 안에서만 비교(각 행은 자기 지표). 말할 포인트: *어떤 드라이버도 모델이 truncation을
  공개하게 못 만들고(71→11은 필드 하나의 일), 어떤 프롬프트도 행 하나를 346바이트
  아래로 못 만든다.*
- 마무리 멘트: 의미론 전부는 SEOCHO — 조건 7이 측정해낸 CSV 인코딩은 seocho#466으로
  upstream, 재현은 `pip install -r requirements.txt` 한 줄.

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
| `python scripts/plot_interaction.py` | p99 by difficulty/question, 정확도·비용 | 스케일 축 원결과 전부 |
| `python scripts/plot_in_context.py` | outcomes (71 vs 11), 질문별 db hits | 필드 하나의 인과, 트리오 스케일 |
| `python scripts/plot_depth.py` | 인코딩/runaway/CPU/스케일링 각 확대판 | engineering-detail 패널별 심화 |

## 한 줄 아크

그래프가 100배 커지면 에이전트 설계가 갈라진다 → 갈라짐의 절반은 모델이 아는 것(계약),
나머지 절반은 행이 모델까지 오는 길(런타임) → 수리는 두 종류이고 둘 다 필요하다.
