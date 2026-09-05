# Finance Ontology Lab

금융 온톨로지 연구를 위한 개인용 로컬 웹 워크스페이스입니다. 실험 의도, 업무 요청,
입출력, 변경 조건, 실제 측정 기록, 연구자의 판정을 연결합니다.

```bash
python3 experiment_workspace/server.py
```

브라우저에서 <http://127.0.0.1:8765>에 접속합니다. Python 3.10 이상과 저장소의 기존
`pyyaml` 의존성을 사용합니다. 프런트엔드 빌드나 외부 서비스는 필요하지 않습니다.

## 사용할 수 있는 기능

- **연구 개요:** ontology × finance 중심의 연구 질문과 설계 완성도를 확인합니다.
- **금융 업무 사례:** suite별 요청, 파라미터, 의미·물리 매핑, 결과·검증 명세를 검색합니다.
- **실험 설계:** 계약 생성·편집, 필수 항목 확인, 관측·해석·한계·판정 기록, 버전 이력을 제공합니다.
- **실행 근거·비교:** 실제 report와 conversations에서 조건별 비용 및 episode의 Input →
  Cypher → 반환 기록 → Gold·verifier 판정을 조회합니다. 선택한 episode를 계약에 연결합니다.
- **발표 구성:** 제목 초안, 이야기 순서, 계약에 기록한 판정, 의미 매핑의 지원 범위를 모읍니다.

연구 계획과 판단을 저장하는 도구이며, 작업 이슈는 계속 **bd**로 관리합니다.
구현 이슈는 `AIsummit26-as8`입니다. 실험 실행·중단·클라우드 비용 집행 API는 포함하지 않습니다.

## 첫 사용 순서

1. 업무 사례에서 요청의 뜻과 기대 출력을 확인합니다.
2. 실험 설계에서 내릴 결정과 변경점, 입력값, 판정 기준, 비용 상한을 작성합니다.
3. 실행 근거에서 동일한 run·그래프·anchor·요청·반복의 조건을 비교합니다.
4. episode를 열어 근거를 계약에 연결합니다.
5. 관측·해석·한계를 구분해 적고 판정을 저장합니다.

기본 계약의 판정 기준과 비용 상한은 미정이므로 비어 있습니다. `필수 항목 작성됨`은
문자열 항목의 완성도만 뜻합니다. 실제 바인딩, Gold, 수치 기준, 실행 예산을 검증하거나
유료 실행을 승인하는 상태가 아닙니다. 기본 계약은 가설 초안이며 기존 run의 사전등록
내용을 소급해서 바꾸지 않습니다.

## 데이터와 해석의 경계

자료는 이 저장소의 다음 위치에서 읽습니다. 디렉터리가 없는 새 checkout에서는 존재하는
자료만 연결하며, 누락한 데이터를 생성하거나 비슷한 사례로 대체하지 않습니다.

- `configs/fibo_text2cypher_suite.yaml`
- `configs/agentic_request_schema_contracts_v2.yaml`
- `configs/mara_stage_b_suite_v1.yaml` (있을 때)
- `ontology/business_request_finbench.mapping.yaml`
- `results/episodes/fibo_schema_context/*/report.json`
- `results/episodes/agent_topology/*/report.json`
- `results/episodes/agent_model_matrix/*/models/*/report.json`

`invalid_*` 디렉터리와 matrix 부모 집계 파일은 가져오지 않습니다. 파일 읽기 실패는
연구 개요에 표시합니다. 보고서의 manifest·raw samples·endpoint 누락, dirty commit,
실패한 trace/monitor receipt 등은 화면에 표시하며 플랫폼이 run 유효성을 인증하지 않습니다.

비교는 단일 run과 `(sf, database, anchor)` 안에서 모든 arm에 `(question_id, repeat)`가
정확히 한 번씩 있는 표본만 사용합니다. 누락·중복 때문에 비교에서 빠진 기록도 원본
목록에 보존합니다. 실패 표본은 제거하지 않고, 미기록 토큰·DB 비용을 0으로 만들지 않습니다.
정답률의 분모는 boolean 판정이 기록된 표본이며 전체 표본 수도 함께 표시합니다.

`ResultEnvelope.rows` 또는 `EvidencePacket.rows`가 verifier 입력 JSON에 명시적으로
기록되어 있으면 실제 전달 결과로 표시합니다. 그 외에는 행 개수와 값의 미기록을
구분합니다. 평가기의 Gold를 실제 출력으로 표시하지 않습니다. manifest에 scale이 없으면
DB 이름에서 추정하지 않습니다. 현재 YAML 명세와 과거 실행 입력은 별도로 표시합니다.

표는 원본 samples로 계산한 기존 측정의 기술 통계입니다. 원인 재현 실험, 유의성 검정,
ontology의 일반적 효과 입증을 수행하지 않습니다. ontology 문맥 비교와 agent 계약 비교의
종류를 표시하며, 후자의 개선을 ontology의 독립 효과로 해석하지 않습니다.

## 저장과 백업

계약은 `experiment_workspace/data/workspace.sqlite`에 새 버전으로 누적 저장됩니다.
원본 결과·설정·기존 문서는 수정하지 않습니다. 다른 창의 오래된 버전으로 저장하면 409를
반환해 최신 내용을 덮어쓰지 못하게 합니다. 판정을 저장할 때는 실제 episode 근거와
관측·해석·한계를 요구합니다. 데이터 폴더는 git에서 제외합니다.

화면의 **계약과 이력 내보내기**는 모든 계약과 버전을 JSON으로 내려받습니다. 복원은 서버를
종료한 상태에서 백업한 SQLite 파일을 같은 경로에 두는 방식입니다. JSON 가져오기 UI는
아직 없으며 JSON 내보내기는 외부 분석·보관용입니다.

```bash
python3 experiment_workspace/server.py --port 8766 --data-dir /tmp/finance-ontology-review
```

서버는 `127.0.0.1`에서만 열립니다. `.env`나 임의의 저장소 파일을 제공하지 않으며,
등록한 자료만 원본 조회 API로 읽습니다. 원격 공유·인증·배포가 필요한 경우 별도 설계가 필요합니다.

## 검증

```bash
python3 -m pytest tests/test_experiment_workspace.py -q
node --check experiment_workspace/static/app.js
python3 scripts/analysis/check_chart_provenance.py
```

브라우저 검증은 Node 22 이상과 Chrome의 DevTools 포트를 사용합니다. 테스트는 계약을
저장하므로 **반드시 임시 데이터 디렉터리의 별도 서버**에 연결합니다.

```bash
python3 experiment_workspace/server.py --port 8766 --data-dir /tmp/finance-ontology-browser-state
google-chrome --headless=new --remote-debugging-port=9225 --user-data-dir=/tmp/finance-ontology-browser about:blank
node tests/workspace_browser_smoke.mjs http://127.0.0.1:8766 http://127.0.0.1:9225
```

실제 FIBO 비교 값과 원본의 일치, 검색, 기록 조회, 근거 연결, 저장·새로고침·이력,
새 계약, 내보내기, 발표 화면과 모바일 넘침을 검사합니다. 스크린샷은 `/tmp/finance-ontology-*.png`에
기록합니다. 모델 API나 그래프 데이터베이스를 호출하지 않습니다.
