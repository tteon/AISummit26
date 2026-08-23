# vast.ai 운용 인수인계 (agent handoff)

이 문서는 이 프로젝트에서 GPU를 빌려 쓰는 방식의 전부다. 여기 적힌 규칙은 전부 한 번씩
돈이나 데이터를 잃고 확정된 것이므로, 순서와 브래킷 하나까지 그대로 따른다.
(사람 독자용 배경: `docs/testbed.md`. 자동화 전체 파이프라인: `testbed/vast_run.sh`.)

## 0. 철칙 — 과금 규율

**finish → copy local → verify the copy → destroy.** 파기 전에 모든 아티팩트가 로컬에
비어 있지 않게 존재하는지 확인한 뒤에만 `destroy` 한다. 반대 순서는 데이터 손실이다.

- **`stop`은 안전한 주차가 아니다.** 정지한 인스턴스는 재시작이 거부될 수 있다
  ("Required resources are currently unavailable" — 실제로 겪었고 부분 프로파일을 잃었다).
  쉴 거면 파기하고, 다시 필요하면 새로 빌린다. 63GB 가중치 다운로드는 몇 분이면 끝난다.
- `destroy`는 **대화형 확인 프롬프트**를 띄운다. 스크립트에서는 `yes | vastai destroy
  instance <id>` 로 확인까지 넘기고, 반드시 `vastai show instances --raw` 로
  `remaining: NONE`을 재확인한다. (한 번은 "Aborted."가 조용히 나오고 인스턴스가
  살아 있었다.)
- 지금까지의 실측 단가: 1×H200 $3.29–3.94/hr (Virginia/Saudi). 문법 A/B + e2e 2회 런
  ≈ 2.6 GPU-h ≈ $9. 라우팅 판정 런 ≈ 1.1 GPU-h.

## 1. 자격 증명

- API 키: 리포 루트 `.env`의 `VASTAI_API_KEY` → `vastai set api-key <값>` 으로 1회 등록
  (값을 echo하지 말 것). 계정: jeongiitae6@gmail.com.
- SSH 키: `~/.ssh/id_ed25519`, 계정에 **인스턴스 생성 전** 등록되어 있어야 한다
  (`vastai create ssh-key ~/.ssh/id_ed25519.pub`). 키는 생성 시점에 박히므로, 잊었으면
  새 인스턴스다.
- 확인: `vastai show user --raw` 로 username/credit이 나오면 준비된 것.

## 2. 오퍼 검색 — 필드명 함정

```bash
vastai search offers "gpu_name=H200 num_gpus=1 verified=true rentable=true \
  direct_port_count>=1 disk_space>=200 dph<=4.0 reliability>0.98 \
  inet_down>=500 duration>1" -o dph --raw
```

- **쿼리 필드명과 결과 필드명이 다르다.** 가격 필터는 `dph`, 결과 JSON의 가격은
  `dph_total`. 틀린 필드명은 **에러 없이 무시**되므로(무필터 검색의 1등이 $8/hr일 수
  있다), 고른 오퍼에서 `dph_total <= 상한`을 **코드로 재확인**한다.
- `inet_down>=500`: 63GB 가중치 기준. 실측 24Gbps 호스트는 3분, 5.7Gbps도 문제없었다.

## 3. 생성과 기동

```bash
vastai create instance <offer_id> --image vllm/vllm-openai:v0.27.1 \
  --disk 200 --ssh --direct --onstart-cmd 'tail -f /dev/null' --raw
# → JSON의 new_contract 가 instance id
```

- 이미지는 `vllm/vllm-openai:v0.27.1` 고정. (범용 이미지에서 py3.11 array 버그·gcc/nvcc
  부재로 이미 한 번 시간을 태웠다.)
- running 대기: `actual_status` 폴링. `exited|offline|unknown_error`는 **절대 running이
  되지 않는다** — 즉시 파기하고 다른 오퍼로.
- ssh 접점: `vastai show instance <id> --raw` 의 `ssh_host`/`ssh_port`
  (또는 `vastai ssh-url`).

## 4. 원격 명령의 함정 두 개

- **원격에서 데몬을 띄우면 ssh 자체가 안 돌아온다.** `setsid nohup <cmd> > log 2>&1
  < /dev/null &` 로 완전히 분리해도 세션이 걸릴 수 있다 — 우리 쪽 timeout(exit 143)은
  정상이며, 성공 여부는 **재접속해서 확인**한다.
- 그 확인은 `pgrep -f "vllm[ ]serve"` — **패턴에 브래킷 필수.** 브래킷 없으면 pgrep/pkill이
  자기 셸을 잡는다(로컬에서 두 번 자해했다).

## 5. vLLM 서빙 (이 프로젝트의 표준)

```bash
scp -P <port> testbed/serve_vllm.sh root@<host>:/root/
ssh ... 'cd /root && VLLM_MODEL=openai/gpt-oss-120b PREFIX_CACHING=on \
  KV_CACHE_METRICS=on KV_CACHE_METRICS_SAMPLE=1.0 TOOL_PARSER=openai \
  setsid nohup bash serve_vllm.sh > serve_boot.log 2>&1 < /dev/null &'
```

- **`TOOL_PARSER=openai` 필수** (gpt-oss). 없으면 `tool_choice:auto`가 200으로 받아지고
  finish_reason=stop·빈 content·tool_calls 없음 — 에피소드 전부가 0-trip으로 조용히
  0점이 된다. **52 에피소드 한 런을 통째로 날리고 배운 것.**
- readiness는 `/v1/models` 폴링 (serve_vllm.sh가 함). "프로세스가 떠 있다" ≠ "응답한다".

## 6. 하이브리드 패턴 (권장): vLLM만 원격, 나머지는 로컬

하네스·DozerDB·seocho·OTel 스택은 로컬에 두고 vLLM만 터널로 쓴다. **결과물이 처음부터
로컬에 쌓여서 copy-verify 단계가 서버측 메트릭 몇 개로 줄어든다.**

```bash
setsid nohup ssh -i ~/.ssh/id_ed25519 -p <port> root@<host> \
  -L 8100:localhost:8000 -N -o ExitOnForwardFailure=yes > tunnel.log 2>&1 < /dev/null &
ss -tln | grep 8100   # 반드시 리스너 확인
```

- **로컬 8000은 RTX 3070의 vLLM(Qwen 1.5B)이 쓰고 있을 수 있다 → 8100을 쓴다.**
  바인드 실패는 조용하다: 8000이 열려 있길래 터널인 줄 알았는데 로컬 Qwen이었고,
  벤치가 엉뚱한 모델로 갈 뻔했다. 터널 후 `/v1/models`의 **모델명까지** 확인할 것.

## 7. 유료 런 전 프리플라이트 (매번, 순서대로)

돈 나가기 전에 전부 통과해야 한다. 각각이 조용히 실패하는 부류다.

1. **툴콜**: 더미 tool로 curl → `finish_reason: tool_calls` 확인.
2. **문법 존중**: sentinel 문법 프로브, `max_tokens=2048` + **containment** 판정
   (reasoning 채널이 ~70토큰을 먼저 먹고, harmony가 `<|end|>`를 붙인다).
   MARA류 엔드포인트는 문법을 200 OK로 무시한다 — 프로브 없는 A/B는 false null.
3. **토큰 캡**: `SEOCHO_TEXT2CYPHER_MAX_TOKENS=4000` (기본 2000은 reasoning이 잘라먹는다).
4. **하드웨어 샘플러 선기동**: 원격 `nvidia-smi` 1초 CSV + 로컬 CPU/cgroup 샘플러.
   런의 observability(OTLP→Tempo/Prometheus)는 공짜로 오지만 하드웨어 뷰는 아니다.
5. 문법 param 목록 = **실행기가 받는 것**(`workspace_id, limit, acct_no`). 하네스 별칭
   `$a`를 넣었다가 28건 ParameterMissing.

## 8. 종료 절차

```bash
# 1) 서버측 스냅샷 회수
curl -s http://127.0.0.1:8100/metrics | grep -E "prefix_cache|prompt_tokens" > metrics_final.txt
scp -P <port> root@<host>:/root/gpu_samples.csv .
scp -P <port> root@<host>:/root/vllm_flags.json .
# 2) 모든 아티팩트 로컬 존재+비어있지 않음 확인 (루프로 [ -s ] 체크)
# 3) 파기 + 재확인
yes | vastai destroy instance <id>
vastai show instances --raw   # → 반드시 빈 배열
# 4) 터널/로컬 샘플러 정리: pkill -f "8100:localhost:800[0]" (브래킷!)
```

아티팩트는 `results/runs/<runid>/`에 커밋한다: instance JSON(머신 스펙 — 숫자를 만든
기계는 파기 후 사라지므로 먼저 기록), gpu_samples.csv, metrics 스냅샷, vllm_flags.json.
`*.nsys-rep`/`*.ncu-rep` 원본은 gitignore(75–237MB로 push가 거부된 적 있음) — 파생 CSV만.

## 9. 지금까지의 인스턴스 이력

| 일자 | id | 용도 | 시간/비용 | 산출물 |
|---|---|---|---|---|
| 08-21 | 48312627 | prefix 캐시 스윕 (H200) | — | `results/runs/20260821T1500Z_*` |
| 08-22 | 48356748 | 문법 A/B + e2e ±grammar | ~2.6h/$9 | `results/runs/20260822T_h200_grammar_e2e/` |
| 08-22 | 48370506 | FIBO 스위트 3모드 판정 | ~1.1h | `results/runs/20260822T_h200_fibo_routing/` |

전부 파기 확인됨. 관련 메모리: `rented-gpu-billing-discipline`, `gpu-run-wiring-lessons`.
