# 금융 업무 의미 매핑 파일럿 v1

실험 전 고정한 프로토콜: `configs/ontology_mapping_pilot_v1.yaml`. 이슈: `AIsummit26-uqa`.

## 의도와 변경점

완전한 물리 스키마에 명시적인 업무 용어 매핑을 추가하면, 금융 요청을 실행 가능한
쿼리로 옮길 때 정확도와 모델·DB 비용이 어떻게 달라지는가?

`physical_schema`와 `business_mapping`은 동일한 모델, endpoint, 데이터, 바인딩,
물리 스키마의 이름·타입·관계 방향, 출력 열·단위·정렬, guardrail, 실행 예산을 사용한다.
두 조건의 차이는 후자에만 `ontology/business_request_finbench.mapping.yaml`의 고정된
업무 용어 사전을 추가하는 것이다. 요청별 Gold 쿼리·정답·검증기 피드백은 제공하지 않는다.
검색 알고리즘이나 동적 용어 선택의 효과를 측정하지 않는다.

이는 로컬 FinBench 의미 매핑과 일부 FIBO 용어의 효과를 측정하는 새 조건이다.
전체 FIBO 추론·준수, 금융기관 운영 성과, 이전 full/retrieved FIBO 조건의 재현을 주장하지 않는다.
성공적으로 지원되는 요청 여섯 개만 사용한다. 미지원 의미의 거절과 법적 지배 관계의
판정은 별도 실험으로 남는다.

## 입력과 기대 출력

`configs/agentic_request_schema_contracts_v2.yaml`에서 아래 요청을 선택했다.
기존 v2 파일은 수정하지 않았다. 사용자 파라미터와 데이터 선택 규칙은 새 프로토콜에서 고정한다.

| 요청 | 명시한 입력 | 기대 출력 |
| --- | --- | --- |
| inbound_amount_band | 기본 계좌, min_amount=10000 | count, total_amount_krw |
| high_risk_rail_and_medium | 기본 계좌, risk_weight=3 | rail, transactions; 거래량 내림차순·rail 오름차순 |
| shared_medium_access | 기본 계좌, result_limit=10; harness limit=10 | 서로 다른 shared_account, 번호 오름차순 |
| loan_applicant_facilities | 신청 경로가 있는 최소 acct_no를 고정된 쿼리로 선택 | loan, principal_krw; loan 오름차순 |
| repayment_ratio_policy | repaid_ratio=0.75; 조건을 만족하는 최소 acct_no 선택 | loan, principal_krw, repaid_krw; 상환액·loan 오름차순 |
| corporate_investor_account_exposure | 투자자와 소유 계좌가 있는 최소 company_id 선택 | investor, company_accounts; 계좌 수 내림차순·investor 오름차순 |

그래프는 `finbenchsf1v2`, workspace는 `default`, 기본 계좌는 108이다. 기업 ID는
이 typed 그래프의 `Company:N` 형식을 사용한다. 기존 v2 카탈로그의 `^C[0-9]+$`와
다르므로 새 프로토콜에 명시적인 parameter override를 선언했다. 원본 그래프와 기존
실험 결과는 변경하지 않는다.

DB-only 준비 단계는 실제 바인딩, Gold의 열·값·순서, 선택 쿼리와 인덱스 영수증을
`preflight.json`에 저장한다. 준비 단계 출력은 실제 실행 근거이며 새 모델 결과가 아니다.

## 고정된 실행·판정 규칙

- 모델: MARA `gemma-4-31B-it`, temperature=0, reasoning_effort 미지정, 출력 상한 1500 tokens.
- 여섯 요청 × 두 조건 × 두 반복 = 24 episodes. 두 번째 반복은 조건 순서를 반대로 한다.
- 모델 호출 최대 24회. SDK retry, transport retry, semantic repair 모두 0이다.
- 호출별 prompt payload 최대 10,000 UTF-8 bytes, 전체 최대 240,000 bytes.
  이 값은 입장 제어 예산이며 토큰 사용량으로 보고하지 않는다. 서버 usage의 tokens를 별도로 저장한다.
- completion 최대 36,000 tokens, 모델 요청 timeout 90초, DB 쿼리 timeout 2초.
- row cap은 기본 50, shared_medium_access는 10. 전체 실행 시간 상한 1800초.
- 계획의 leaf EstimatedRows 상한 5000. 기준 개체 probe는 NodeIndexSeek와 추정 2행 이하를 모두 요구한다.
- 전체 노드·관계 타입별 counts가 SF1 snapshot manifest와 일치해야 한다.
- 각 Gold는 공유 guardrail·plan gate를 통과하고 비어 있거나 전부 0인 결과가 아니어야 한다.

정답은 **정확한 열 이름·타입·값·순서**를 기준으로 판정한다. guardrail 거절과 잘못된
출력은 실패 표본에 포함한다. transport 오류, 서버 receipt 누락은 무효 표본으로 보존하고
실행을 중단한다. 무효 표본을 정답률의 조용한 0으로 계산하지 않는다.

12개 유효한 A/B 쌍이 모두 완성되어야 판정을 내린다.

- **후속 확인 실험으로 진행:** 처리군의 개선 2쌍 이상, 회귀 0쌍, 전체 prompt+completion
  토큰이 대조군의 1.5배 이하.
- **이 설정을 기각:** 처리군의 정답이 더 적거나, 정답 증가 없이 토큰 비율이 1.5배 초과.
- **그 외:** 판단 보류. 작은 탐색 파일럿이므로 유의성이나 일반적인 모델 순위를 주장하지 않는다.

## 실행과 증거 보존

실행기는 `scripts/benchmarks/run_ontology_mapping_pilot.py`다. 기본 동작은 DB-only 준비이며
유료 모델 호출은 `--execute`에서만 수행한다. 이미 존재하는 출력은 `--resume`을 요구하며,
프로토콜·입력 파일·snapshot·endpoint·dependency hash가 달라지면 resume을 거부한다.
유료 실행의 source commit은 clean이어야 한다.

SEOCHO는 커밋 `c3a1871cfe83f89b0f203a01603c00e7ef1126cb`의 `src/seocho` 아카이브를
별도 디렉터리에 풀어 사용한다. 기존 sibling 작업 트리의 미커밋 코드를 참조하지 않는다.
모든 Python 파일을 아카이브와 대조하며, dependency source 아카이브도 run에 보존한다.
모델 endpoint의 GPU 구성은 미공개이며 manifest의 로컬 accelerator는 DB/클라이언트 호스트의 정보다.

각 run은 manifest, endpoint, protocol, snapshot manifest, dependency source, DB-only Gold,
호출 전 attempts, 실제 prompts와 반환 행을 포함한 conversations, per-episode samples를 남긴다.
서버의 전체 usage, finish_reason, inference-id, 반환 model 이름도 표본의 일부다.

모델 호출 직후 프로세스가 중단되어 attempt만 남으면 자동 재시작하지 않고 감사를 요구한다.
재시도로 이중 과금하거나 과거 결과를 덮어쓰지 않는다. 집계는 원본 표본으로 재생성하며,
모델 토큰과 DB hits, 서버 시간과 클라이언트 시간을 별도의 분모로 유지한다.
