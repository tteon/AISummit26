# 업무 의미 매핑 파일럿 v1 관측 결과

새 실행 `20260905T_mapping_pilot_v1`의 사전 판정은 **판단 보류**다. 업무 매핑을 추가했을 때
고정 입력과 실행 규칙을 통과한 Gold 일치는 늘었지만 토큰 예산 기준을 충족하지 못했다.

[사전 설계](ontology_mapping_pilot_v1.md) · [고정 프로토콜](../configs/ontology_mapping_pilot_v1.yaml) ·
[원본 보고서](../results/episodes/ontology_mapping/20260905T_mapping_pilot_v1/report.json) ·
[DB 재검증](../results/episodes/ontology_mapping/20260905T_mapping_pilot_v1/audit/audit.json).

## 사전에 정한 질문과 이번 판정

MARA gemma-4-31B-it, temperature 0, SF1에서 같은 요청 여섯 개를 각 조건으로 두 번 실행했다.
모델 호출 24회, 모두 유효, repair 및 retry 0회였다. 결과는 독립적인 12개 업무 사례가 아닌
**여섯 고정 입력의 두 반복**이다. 처리군에만 전체 업무 용어 사전을 추가했다.

| 관측값 | 물리 스키마 | 물리 스키마 + 업무 매핑 |
| --- | ---: | ---: |
| 실행 규칙 준수 및 Gold 일치 | 4/12 | 8/12 |
| Prompt tokens | 8,422 | 19,966 |
| Completion tokens | 1,126 | 1,178 |
| 전체 tokens | 9,548 | 21,144 |
| PROFILE까지 실행된 요청 | 6/12 | 8/12 |
| guardrail에서 거절된 요청 | 6/12 | 4/12 |
| 실행된 PROFILE의 DB hits 합계 | 498 | 588 |

완료된 A/B 쌍 12개 중 개선 4개, 회귀 0개였다.
전체 토큰 비율은 **2.2145배**다. 사전 진행 조건의 비용 상한 1.5배를 넘지만
정답 증가가 있으므로 기각 조건도 충족하지 않는다. 따라서 후속 확인 실험으로 바로 진행하는 판정이 아닌
`inconclusive`가 맞다. 실행 후 기준을 바꾸지 않았다.

DB hits는 다른 수의 요청이 실행된 합계다. 거절된 표본의 0은 PROFILE 미실행을 뜻하며
사전 검증·EXPLAIN 등 전체 DB 작업량의 0이 아니다. 이 합계로 DB 효율 향상/퇴보를 주장하지 않는다.
서버 지연과 클라이언트 지연도 서로 다른 이름과 원래 usage로 보존하며 지연 효과는 주장하지 않는다.
토큰은 서버 usage의 측정 단위이며 금액 비용으로 환산하지 않았다.

## 요청별 이점과 실패 원인

| 요청 | 물리 스키마 | 업무 매핑 | 해석 |
| --- | ---: | ---: | --- |
| `inbound_amount_band` | 0/2 | 2/2 | 매핑 조건만 기준 계좌를 MATCH 속성에 지정했다. 대조군은 WHERE로 지정해 고정 규칙에서 거절되었다. 의미 방향 해석의 개선으로 단정하지 않는다. |
| `high_risk_rail_and_medium` | 0/2 | 2/2 | 대조군은 Channel.label/share, 매핑 조건은 Channel.code와 USES_CHANNEL.tx_count를 사용했다. 매핑 조건의 실제 행만 Gold와 일치했다. |
| `shared_medium_access` | 2/2 | 2/2 | 양쪽 모두 같은 쿼리와 결과를 냈다. 이 입력에서는 추가 문맥의 정답 이점이 없었다. |
| `loan_applicant_facilities` | 0/2 | 0/2 | 양쪽 모두 owner 노드의 workspace 지정 규칙을 위반했다. 매핑 조건에는 기준 계좌 지정 규칙 위반도 있었다. |
| `repayment_ratio_policy` | 2/2 | 2/2 | 양쪽 결과는 같지만 대조군은 개별 r.amount, 매핑은 sum(r.amount)를 사용했다. 이 입력의 한 행은 의미 차이와 정렬 동률을 구별하지 못한다. |
| `corporate_investor_account_exposure` | 0/2 | 0/2 | 양쪽 모두 workspace 지정 규칙에서 거절되었다. 대조군에는 _workspace_id를 라벨처럼 쓴 오류도 있었다. 법적 지배 관계를 검증한 사례가 아니다. |

입금 개선 두 쌍은 실행 규칙 준수, 지급 채널 개선 두 쌍은 반환 필드 선택 차이로 관측했다.
따라서 네 개선을 모두 온톨로지의 독립적인 의미 추론 개선으로 셀 수 없다. 특히 상환의 두 조건을
일반적으로 올바른 쿼리라고 주장할 수 없다. 정답은 이번 바인딩에서의 Gold 일치만 뜻한다.

## 실제 Input → Output 예시

계좌 `108`, `risk_weight=3`, `workspace_id=default`, `limit=50`에서 지급 채널과 거래 건수를 요청했다.
기대 열은 `rail`, `transactions`, 정렬은 거래 건수 내림차순과 rail 오름차순이다.

- 물리 스키마 결과: `오픈뱅킹 즉시이체 / open-banking instant transfer: 12`, `선불·상품권 / prepaid & gift certificate: 2`.
- 업무 매핑 결과 및 Gold: `OPEN_BANKING: 3`, `PREPAID_GIFT: 1`.

출처는 두 조건의 `high_risk_rail_and_medium:r0` 표본과 `conversations.jsonl`의
`observed_output.rows`다. Gold를 실제 출력 대신 표시하지 않는다. 두 반복 모두 같은 차이를 보였다.
두 쿼리의 PROFILE DB hits는 각각 50으로 같았고, DB 재실행에서도 확인했다.

## 재현성과 보존

모델 측정 commit은 `c28c674219418bf814a62ead3d45e07508420b78`, clean 상태다.
manifest에 decoder, 컨테이너 이미지와 적용 설정, 로컬 하드웨어, endpoint를 기록했다.
모델 서버 GPU는 미공개이며 manifest의 RTX 3070은 DB/클라이언트 호스트다.
프로토콜·스키마·매핑·snapshot·실행 코드·고정 SEOCHO dependency의 hash를 보존했다.

플랫폼 계약 v1은 첫 모델 호출 전에 저장했고, v2는 관측·해석·한계와 12개 표본 링크를 추가했다.
두 JSON snapshot이 실행 폴더의 `platform_contract_before.json`, `platform_contract_after.json`이다.
기존 계약 필드의 실행 기준은 변경하지 않았다. 로컬 SQLite의 두 revision도 보존된다.
DB-only 준비의 별도 디렉터리는 `20260905T_mapping_preflight_v1`이며 유료 측정 표본으로 합산하지 않는다.

검증 결과:

- 원본 24개 표본의 attempts, messages, 파라미터, usage, 출력, 점수, 집계가 일치했다.
- snapshot 타입별 counts, 실제 anchor, index point lookup, Gold를 다시 확인했다.
- 기존에 실행 허용된 고유 쿼리 6종을 한 번씩 다시 실행했다. 해당 원본 14개 실행 표본의
  결과 행·열·DB hits가 모두 일치했다. 거절된 10개 표본은 실행하지 않았다.
- `--resume` 검증에서 `existing=24 new=0`을 확인했다. DB 재검증의 모델 호출은 0회다.
- 관련 Python 테스트 18개, 브라우저의 계약 저장·입출력·판정·anchor 전환·모바일 검사,
  기존 chart provenance 검사가 통과했다.

원본 측정은 그대로 두고 재검증 표본은 `audit/replay_samples.jsonl`, 재검증 환경은
`audit/manifest.json`에 별도로 저장했다. 재검증 지연을 원래 모델 실험과 합산하지 않는다.

## 후속 설계

`AIsummit26-gwr`는 의미 해석과 실행 규칙 준수를 분리하고, 합산·정렬·여러 기준 개체를
구별할 수 있는 입력을 사전에 고정하는 작업이다. `AIsummit26-bi6`는 그 계약을 바탕으로
전체 업무 사전과 필요한 용어만 제공하는 조건의 정확도·선택 비용·토큰을 비교하는 작업이다.
이 파일은 작업 추적을 대체하지 않으며 실제 상태와 의존성은 bd에서 관리한다.

이번 결과로 전체 FIBO/OWL 추론, 법적 지배 관계, 실제 금융기관의 성과 또는 온톨로지의
일반적 우월성을 주장하지 않는다. 발표에서는 구체적인 필드 선택 개선, 실행 계약의 실패,
전체 매핑의 문맥 비용을 함께 보여주는 사례로 사용할 수 있다.
