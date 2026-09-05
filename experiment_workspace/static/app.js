"use strict";
const $ = s => document.querySelector(s);
const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pretty = x => x == null ? "원본에 미기록" : JSON.stringify(x, null, 2);
const num = x => typeof x === "number" ? x.toLocaleString("ko-KR", {maximumFractionDigits: 2}) : "미기록";
const source = path => `/api/source?${new URLSearchParams({path})}`;
const linkSource = (path, label) => `<a class="text-link" target="_blank" rel="noopener" href="${esc(source(path))}">${esc(label || path)}</a>`;
const badge = (label, tone="neutral") => `<span class="badge ${tone}">${esc(label)}</span>`;
const pre = x => `<pre>${esc(typeof x === "string" ? x : pretty(x))}</pre>`;
const labels = {overview:"연구 개요", cases:"금융 업무 사례", contracts:"실험 설계", evidence:"실행 근거 · 비교", talk:"발표 구성"};
const verdicts = {unreviewed:"검토 전", adopt:"채택", reject:"기각", inconclusive:"판단 보류"};
const armNames = {physical_schema:"물리 스키마", business_mapping:"업무 의미 매핑 추가", physical_only:"물리 스키마만", compiled_fibo:"전체 FIBO 매핑", retrieved_fibo:"검색한 FIBO 매핑", direct_single:"단일 에이전트", multi_typed:"역할 분리 · typed", multi_envelope:"역할 분리 · envelope"};
const titles = {in_transfer_total:"입금 건수와 총액", customer_inflow_summary:"고객 계좌 입금 요약", two_hop_reach:"송금 경로의 도달 범위", owner_portfolio:"계좌 소유자의 보유 계좌", recent_page:"최근 송금 내역", ubo_chain:"기업 계좌 뒤의 투자자", ubo_depth:"다수 계좌를 보유한 소유자", guarantee_chain:"연쇄 보증 관계", rail_mix:"지급 채널별 거래 구성", structuring_fanin:"반복 입금 계좌 탐색", loan_layering:"대출금 이동 경로", inbound_amount_band:"일정 금액 이상의 입금", outgoing_cross_border_exposure:"국경 간 송금 노출", shared_medium_access:"동일 접속 기기 사용 계좌", blocked_medium_exposure:"차단 기기 접속 이력", high_risk_rail_and_medium:"위험 지급 채널 조회", common_owner_device_link:"소유자와 기기가 같은 계좌", loan_applicant_facilities:"계좌 소유자의 대출 신청", repayment_ratio_policy:"상환 비율 미달 대출", guarantor_of_applicant:"신청자의 보증 관계"};
const state = {catalog:null, dirty:false, contract:null, run:null, runKey:null, route:"", version:0};

async function api(path, body) {
  const response = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json", "X-Workspace-Request":"1"}, body:JSON.stringify(body)} : {});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "요청을 완료하지 못했습니다.");
  return data;
}
let toastTimer;
function toast(message) { $("#toast").textContent = message; $("#toast").classList.add("show"); clearTimeout(toastTimer); toastTimer=setTimeout(() => $("#toast").classList.remove("show"),5000); }
function heading(eyebrow, title, subtitle, action="") { return `<div class="page-heading"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1><p class="sub">${esc(subtitle)}</p></div>${action}</div>`; }
function contractBadge(c) { return c.conclusion !== "unreviewed" ? badge(verdicts[c.conclusion], "green") : badge(c.readiness.complete ? "필수 항목 작성됨" : `설계 중 · ${c.readiness.missing.length}항목 필요`, "amber"); }
function defaultRun() { const runs=state.catalog.runs; return runs.find(r=>r.name.includes("fibo_schema_confirmatory")) || runs.find(r=>r.family==="ontology") || runs[0]; }
function cards() { return state.catalog.contracts.map((c,i)=>`<a class="card research-card" href="#contracts?id=${encodeURIComponent(c.id)}"><div class="card-top"><span class="card-id">EXPERIMENT 0${i+1}</span>${contractBadge(c)}</div><h3>${esc(c.title)}</h3><p>${esc(c.decision)}</p><div class="card-bottom"><span>연결한 근거 ${c.evidence.length}건 · v${c.version}</span><span>실험 계약 열기 ↗</span></div></a>`).join(""); }

function overview() {
  const {project, contracts, cases, runs, import_errors}=state.catalog;
  const ready=contracts.filter(c=>c.readiness.complete).length;
  $("#main").innerHTML = heading("Your research, connected", "나의 연구를 한눈에", "무엇을 알아보려는지, 무엇을 관측했는지, 어떤 결정을 내렸는지 연결합니다.", `<a class="button" href="#contracts">실험 설계 열기 ↗</a>`) +
  `<section class="hero"><div><div class="eyebrow">FINBENCH × ONTOLOGY × FINANCIAL AI</div><h2>${esc(project.title)}</h2><p>${esc(project.subtitle)}.<br>업무 요청 하나에서 시작해 의미의 정확성과 실행 비용을 함께 살펴봅니다.</p><a class="button" href="#cases">금융 업무 사례 살펴보기 <span>→</span></a></div><div class="hero-chain"><div class="chain-step"><span class="step-num">01</span><div><strong>의도와 업무 요청</strong><small>어떤 결정을 위한 실험인가</small></div></div><div class="chain-rule"></div><div class="chain-step"><span class="step-num">02</span><div><strong>입력 · 변경점 · 기대 결과</strong><small>무엇을 고정하고 무엇을 바꾸는가</small></div></div><div class="chain-rule"></div><div class="chain-step"><span class="step-num">03</span><div><strong>실행 근거와 나의 판정</strong><small>어떤 이점과 한계를 확인했는가</small></div></div></div></section>
  <div class="stats"><div class="stat"><div><label>금융 업무 요청</label><small>suite별 원본 항목</small></div><strong>${cases.length}</strong></div><div class="stat"><div><label>연결된 실행 묶음</label><small>조건별 report</small></div><strong>${runs.length}</strong></div><div class="stat"><div><label>필수 설계 항목 작성</label><small>실행 검증과 별도</small></div><strong>${ready}<span class="muted"> / ${contracts.length}</span></strong></div></div>
  <div class="section-top"><h2>이번 연구의 실험 질문</h2><small>각 실험은 하나의 설계 결정으로</small></div><div class="card-grid">${cards()}</div>
  <div class="section-top"><h2>근거를 읽는 방법</h2><a class="text-link" href="#evidence">실행 근거 열기 ↗</a></div>
  <div class="two-col"><div class="panel"><span class="label">ONTOLOGY EFFECT</span><h3>의미 문맥의 효과를 직접 비교</h3><p class="small">물리 스키마만 제공한 조건과 전체·검색 FIBO 매핑 조건을 같은 run, 요청, 반복, 그래프에서 나란히 확인합니다.</p></div><div class="panel"><span class="label">ENGINEERING TRADE-OFFS</span><h3>모델 비용과 DB 비용을 함께 확인</h3><p class="small">정답 여부와 토큰, DB hits, 실행 시간을 함께 읽습니다. 역할 분리나 repair 실험의 결과는 ontology의 독립 효과와 구분합니다.</p></div></div>
  <div class="note">${esc(project.scope)}</div>${import_errors.length ? `<div class="error">가져오지 못한 자료 ${import_errors.length}건 ${pre(import_errors)}</div>` : ""}`;
}

function casesPage(params) {
  const cases=state.catalog.cases;
  const selected=cases.find(c=>c.catalog_key===params.get("id")) || cases[0];
  $("#main").innerHTML=heading("Business cases", "금융 업무에서 출발하기", "요청의 뜻을 물리 스키마, 기대 출력, 검증 조건으로 따라갑니다.")+
    `<div class="toolbar"><label class="grow">사례 검색<input id="case-search" placeholder="입금, owner, repayment, AML…" type="search"></label><label>요청 목록<select id="case-suite"><option value="">모든 suite</option>${[...new Set(cases.map(c=>c.suite))].map(s=>`<option>${esc(s)}</option>`).join("")}</select></label></div><div class="case-grid"><div class="case-list" id="case-list"></div><div id="case-detail"></div></div>`;
  function list() {
    const search=$("#case-search").value.toLowerCase(), suite=$("#case-suite").value;
    const filtered=cases.filter(c=>(!suite || c.suite===suite) && `${titles[c.id]||""} ${JSON.stringify(c)}`.toLowerCase().includes(search));
    $("#case-list").innerHTML=filtered.map(c=>`<button class="case-option ${c.catalog_key===selected?.catalog_key ? "selected":""}" data-case="${esc(c.catalog_key)}">${badge(c.planned ? "추가 실험용 계약" : "기존 suite", c.planned ? "amber":"neutral")}<strong>${esc(titles[c.id] || c.id)}</strong><small>${esc(c.id)} · ${esc(c.difficulty || c.family || "")}</small></button>`).join("") || `<div class="empty">검색 결과가 없습니다.</div>`;
    document.querySelectorAll("[data-case]").forEach(b=>b.onclick=()=>{ const c=cases.find(x=>x.catalog_key===b.dataset.case); detail(c); document.querySelectorAll(".case-option").forEach(x=>x.classList.toggle("selected", x===b)); });
  }
  function detail(c) {
    if(!c) return;
    const semantic=c.semantic_terms || [c.fibo_anchor || "요청의 schema_facets 참고"];
    const physical=c.physical || {schema_facets:c.schema_facets, gold_query:c.gold_query};
    $("#case-detail").innerHTML=`<article class="panel case-detail"><div class="card-top"><span class="label">${esc(c.domain || c.request_type || c.family || "FINANCIAL CASE")}</span>${badge(c.planned ? "실행 전 바인딩·Gold 확인 필요":"현재 suite 명세")}</div><h2>${esc(titles[c.id] || c.id)}</h2><span class="label">USER INPUT · 원문</span><p>${esc(c.question)}</p>${c.real_world_case ? `<p class="small">${esc(c.real_world_case)}</p>`:""}<div class="flow"><div><small>업무 의미</small><b>${esc(semantic.join(" · "))}</b></div><span>→</span><div><small>실행 가능한 표면</small><b>${esc((c.physical?.relationships || c.schema_facets || ["Gold Cypher 참조"]).join(" · "))}</b></div></div><div class="two-col"><div><span class="label">INPUT · 파라미터 명세</span>${pre(c.parameters || c.params || {})}</div><div><span class="label">EXPECTED OUTPUT · 명세</span>${pre(c.result || {shape:c.shape || "Gold RETURN 절 참조", keys:c.keys || c.column || null})}</div></div><span class="label">정답 검증 조건</span>${c.verification ? `<ul>${c.verification.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="small">참조 쿼리의 결과와 비교합니다. 실제 실행 당시의 scorer 및 Gold 기록은 실행 근거에서 확인하세요.</p>`}<details><summary>물리 스키마와 참조 Cypher</summary>${pre(physical)}${pre(c.gold_query)}</details><div class="note">현재 파일의 명세입니다. 과거 실행의 입력과 결과는 해당 실행의 manifest와 원본 기록을 기준으로 확인합니다.</div>${linkSource(c.source,"요청 명세 원본 ↗")} <a class="button" href="#evidence?question=${encodeURIComponent(c.id)}">이 요청의 실행 찾기 →</a></article>`;
  }
  $("#case-search").oninput=list; $("#case-suite").onchange=list; list(); detail(selected);
}

const fields={title:["실험 제목","무엇을 확인할 것인가"],decision:["내릴 설계 결정","결과가 어떤 선택을 바꾸는가"],hypothesis:["검증할 가설","이점과 손해 가능성을 함께 적습니다"],input_spec:["실험 Input","요청·데이터·바인딩·평가 표본"],expected_output:["기대 Output","출력 형태와 검증 가능한 조건"],intervention:["바꾸는 한 가지","온톨로지 문맥, 매핑, 검증 권한 등"],baseline:["대조 조건 A","변경 전"],treatment:["비교 조건 B","변경 후"],fixed_conditions:["고정 조건","모델·endpoint·데이터·row cap·반복"],acceptance:["판정 기준","개선 최소폭·허용 오류 수준을 실행 전에 구체화하세요"],cost_budget:["비용 상한","예: 총 호출·토큰·시간·금액의 수치 상한. 아직 정하지 않았다면 비워두세요"],stop_rule:["중단·무효 조건","언제 종료하거나 측정을 무효로 할 것인가"],observation:["관측한 사실","원본에서 확인한 내용만 적습니다"],interpretation:["나의 해석","원인 가설과 채택 이유를 구분합니다"],limitation:["한계와 주장 범위","미검증 사항·회귀·추가 비용"]};
function formField(k,c) { const [label,hint]=fields[k]; return `<label class="field">${label}<span>${hint}</span>${k==="title" ? `<input name="${k}" value="${esc(c[k])}" maxlength="20000">` : `<textarea name="${k}" maxlength="20000" rows="3" placeholder="${esc(hint)}">${esc(c[k])}</textarea>`}</label>`; }
function contractsPage(params) {
  const c=structuredClone(state.catalog.contracts.find(c=>c.id===params.get("id")) || state.catalog.contracts[0]);
  state.contract=c;
  const section=(title,keys)=>`<section class="form-section"><h2>${title}</h2>${keys.map(k=>formField(k,c)).join("")}</section>`;
  $("#main").innerHTML=heading("Experiment contract", "의도를 실행 가능한 설계로", "가설·입출력·판정 기준을 같은 계약에 기록하고, 실제 근거와 연결합니다.", `<button id="new-contract" class="button">새 실험 계약 +</button>`)+
    `<div class="contract-select">${state.catalog.contracts.map(x=>`<a href="#contracts?id=${x.id}" class="${x.id===c.id ? "active":""}">${esc(x.title)}</a>`).join("")}</div><form id="contract-form"><div class="contract-layout"><div>${section("01 / 왜 이 실험을 하는가",["title","decision","hypothesis"])}${section("02 / 무엇이 들어오고 나가는가",["input_spec","expected_output"])}${section("03 / 무엇을 비교하는가",["intervention","baseline","treatment","fixed_conditions"])}${section("04 / 어떤 결과로 결정하는가",["acceptance","cost_budget","stop_rule"])}${section("05 / 관측과 해석",["observation","interpretation","limitation"])}<section class="form-section"><label class="field">나의 판정<span>판정 저장에는 원본 근거·관측·해석·한계가 필요합니다. 자동으로 효과를 입증하지 않습니다.</span><select name="conclusion">${Object.entries(verdicts).map(([k,v])=>`<option value="${k}" ${c.conclusion===k ? "selected":""}>${v}</option>`).join("")}</select></label><div class="save-bar"><button class="button primary" type="submit">계약 버전 저장</button><span id="save-status" class="small">v${c.version} · ${c.saved_at ? new Date(c.saved_at).toLocaleString("ko-KR") : "초기 설계 초안"}</span></div><p id="save-error" role="alert"></p></section></div><aside class="contract-aside"><div class="panel"><span class="label">DESIGN READINESS</span><h3>실행 전에 확인할 항목</h3><div id="readiness"></div><p class="small">필수 항목 작성 여부입니다. 실행·Gold 검증이나 실행 승인을 대신하지 않습니다.</p><button type="submit" class="button primary">계약 버전 저장</button></div><div class="panel"><h3>연결된 실행 근거</h3><div id="contract-evidence"></div><a class="text-link" href="#evidence">실행 기록에서 연결하기 ↗</a></div><div class="panel"><h3>변경 이력</h3><div id="history">불러오는 중…</div></div></aside></div></form>`;
  $("#new-contract").onclick=async()=>{
    if(state.dirty){toast("현재 계약을 먼저 저장하세요.");return;}
    const draft=Object.fromEntries(Object.keys(fields).map(k=>[k,""]));
    Object.assign(draft,{id:"new",version:0,title:"새 실험",conclusion:"unreviewed",evidence:[]});
    try{const saved=await api("/api/contracts",draft);state.catalog.contracts.push(saved);location.hash=`contracts?id=${saved.id}`;toast("새 실험 계약을 만들었습니다.");}catch(e){toast(e.message);}
  };
  function drawReadiness() {
    const missing=Object.keys(fields).slice(0,12).filter(k=>!$("#contract-form").elements[k].value.trim());
    $("#readiness").innerHTML=missing.length ? `${badge(`${missing.length}개 항목을 채워주세요`,"amber")}<ul>${missing.map(k=>`<li>${fields[k][0]}</li>`).join("")}</ul>` : `${badge("필수 항목 작성됨","green")}<p class="small">실제 바인딩·Gold·표본·수치 기준을 별도로 검증하세요.</p>`;
  }
  function drawEvidence() {
    $("#contract-evidence").innerHTML=c.evidence.map((r,i)=>`<div class="evidence-ref"><button class="table-button" type="button" data-ref="${i}">${esc(r.episode)}</button><br><button type="button" class="remove-ref" data-remove="${i}">연결 해제</button></div>`).join("") || `<p class="small">아직 연결한 근거가 없습니다.</p>`;
    document.querySelectorAll("[data-ref]").forEach(b=>b.onclick=()=>showEpisode(c.evidence[Number(b.dataset.ref)].run,c.evidence[Number(b.dataset.ref)].episode));
    document.querySelectorAll("[data-remove]").forEach(b=>b.onclick=()=>{c.evidence.splice(Number(b.dataset.remove),1);state.dirty=true;drawEvidence();});
  }
  $("#contract-form").oninput=()=>{state.dirty=true;$("#save-status").textContent="저장하지 않은 변경 내용";drawReadiness();};
  $("#contract-form").onsubmit=async event=>{
    event.preventDefault(); const form=event.currentTarget;
    const payload={...c,...Object.fromEntries(new FormData(form))};
    form.querySelectorAll("button[type=submit]").forEach(b=>b.disabled=true);
    try {
      const saved=await api("/api/contracts",payload);
      state.catalog.contracts=state.catalog.contracts.map(x=>x.id===saved.id ? saved:x);
      state.dirty=false; toast(`계약 v${saved.version}을 저장했습니다.`); contractsPage(new URLSearchParams({id:saved.id}));
    } catch(error) { $("#save-error").textContent=error.message; $("#save-error").className="error"; }
    finally { form.querySelectorAll("button[type=submit]").forEach(b=>b.disabled=false); }
  };
  drawReadiness();drawEvidence();
  api(`/api/history?id=${encodeURIComponent(c.id)}`).then(rows=>{
    if(state.contract?.id!==c.id || !$("#history")) return;
    $("#history").innerHTML=rows.map(r=>`<details class="history-entry"><summary>v${r.version} · ${esc(new Date(r.saved_at).toLocaleString("ko-KR"))}</summary>${pre(r)}</details>`).join("") || `<p class="small">저장하면 새 버전이 쌓입니다. 기존 버전은 보존됩니다.</p>`;
  }).catch(e=>toast(e.message));
}

function partition(s) { return JSON.stringify([s.sf,s.database,s.anchor]); }
function partitionName(s) {return `${s.sf == null ? "SF 미기록" : `SF${s.sf}`} · ${s.database || "DB 미기록"} · anchor ${s.anchor ?? "미기록"}`;}
function total(rows,field) { const values=rows.map(s=>s[field]).filter(v=>typeof v==="number");return values.length ? `${num(values.reduce((a,b)=>a+b,0))}${values.length===rows.length ? "" : ` (${values.length}/${rows.length}건 기록)`}`:"미기록"; }
async function evidencePage(params) {
  const version=state.version;
  const runs=state.catalog.runs;
  const run=runs.find(r=>r.key===params.get("run")) || (params.get("question") ? runs.find(r=>r.family==="ontology") : null) || defaultRun();
  $("#main").innerHTML=heading("Evidence explorer", "실행 근거를 나란히 읽기", "동일 실행·요청·반복·그래프 조건 안에서 입력과 결과를 비교합니다.")+
    `<div class="toolbar"><label class="grow">실행 묶음<select id="run-select">${runs.map(r=>`<option value="${r.key}" ${r.key===run?.key ? "selected":""}>${r.family==="ontology" ? "[Ontology]":"[Agent 계약]"} ${esc(r.name)} · ${esc(r.endpoint.model_name)} · ${r.sample_count}건</option>`).join("")}</select></label><button class="button" id="refresh-evidence">기록 새로고침 ↻</button></div><div id="run-content" class="loading">실행 기록을 읽는 중…</div>`;
  $("#run-select").onchange=event=>{location.hash=`evidence?run=${event.target.value}`;};
  $("#refresh-evidence").onclick=async()=>{try{state.catalog=await api("/api/catalog");await evidencePage(params);toast("현재 원본을 다시 읽었습니다.");}catch(e){toast(e.message);}};
  if(!run) {$("#run-content").innerHTML=`<div class="empty">연결 가능한 실행이 없습니다. results/episodes/의 원본 기록이 필요합니다.</div>`;return;}
  try {
    const data=await api(`/api/run?key=${run.key}`);
    if(version!==state.version) return;
    state.run=data;state.runKey=run.key;
    drawRun(data,params);
  }catch(e){if($("#run-content"))$("#run-content").innerHTML=`<div class="error">${esc(e.message)}</div>`;}
}
function drawRun(data,params) {
  const {meta,samples}=data;
  const partitions=[...new Map(samples.map(s=>[partition(s),s])).entries()];
  const questions=[...new Set(samples.map(s=>s.question_id))];
  const wanted=params.get("question");
  const questionExists=!wanted || questions.includes(wanted);
  $("#run-content").className="";
  $("#run-content").innerHTML=`<div class="meta-grid"><div><small>MODEL / EFFORT</small><b>${esc(meta.endpoint.model_name)} / ${esc(meta.endpoint.reasoning_effort ?? "미지정")}</b></div><div><small>ENDPOINT</small><b>${esc(meta.endpoint.provider)} · ${esc(meta.endpoint.base_url)}</b></div><div><small>COMMIT</small><b>${esc(meta.commit?.slice(0,12) || "미기록")}</b></div><div><small>RAW SAMPLES</small><b>${samples.length}건 · ${linkSource(meta.source,"report 원본 ↗")}</b></div></div><div class="flags">${meta.flags.map(f=>badge(f,"amber")).join("")}${badge(meta.family==="ontology" ? "의미 문맥 비교":"agent 계약 비교")}</div><p class="run-note">${esc(meta.receipt_note)} ${meta.family!=="ontology" ? "이 실행은 ontology의 독립 효과를 측정하는 비교가 아닙니다.":"매핑의 이점과 회귀를 같은 요청에서 함께 확인하세요."}</p>
  ${meta.protocol ? `<details><summary>이번 실험의 고정된 설계 · ${esc(meta.run_status)}</summary>${pre({intent:meta.protocol.intent,decision:meta.protocol.decision,budget:meta.protocol.budget,cases:meta.protocol.cases})}</details>` : ""}
  ${!questionExists ? `<div class="note amber">선택한 요청 ${esc(wanted)}은 이 실행에 없습니다. 다른 실행 묶음을 선택하세요. 유사한 이름의 요청을 같은 측정으로 대체하지 않습니다.</div>`:""}
  <div class="toolbar"><label class="grow">그래프·anchor 조건<select id="scope-filter">${partitions.map(([key,s])=>`<option value="${esc(key)}">${esc(partitionName(s))}</option>`).join("")}</select></label><label>업무 요청<select id="question-filter"><option value="">모든 요청</option>${!questionExists ? `<option value="${esc(wanted)}" selected>${esc(wanted)} (이 실행에 없음)</option>`:""}${questions.map(q=>`<option value="${esc(q)}" ${wanted===q ? "selected":""}>${esc(titles[q] || q)}</option>`).join("")}</select></label><label>반복<select id="repeat-filter"><option value="">모든 반복</option>${[...new Set(samples.map(s=>s.repeat))].sort().map(r=>`<option value="${r}">repeat ${r}</option>`).join("")}</select></label></div><div id="comparison"></div><div class="section-top"><h2>사례별 실행 기록</h2><small>클릭하면 실제 입력 → 쿼리 → 결과 → 판정</small></div><div id="sample-table"></div>`;
  function filter() {
    const scope=$("#scope-filter").value, q=$("#question-filter").value, repeat=$("#repeat-filter").value;
    const rows=samples.filter(s=>partition(s)===scope && (!q || q===s.question_id) && (repeat==="" || String(s.repeat)===repeat));
    const arms=meta.arms;
    const pairKey=s=>JSON.stringify([s.question_id,s.repeat]);
    const counts=new Map();
    rows.forEach(s=>{const key=pairKey(s);if(!counts.has(key))counts.set(key,new Map());const count=counts.get(key);count.set(s.arm,(count.get(s.arm)||0)+1);});
    const paired=rows.filter(s=>arms.length>1 && arms.every(a=>counts.get(pairKey(s)).get(a)===1) && rows.filter(x=>pairKey(x)===pairKey(s)).every(x=>x.valid!==false));
    $("#comparison").innerHTML=`<div class="compare-intro"><h2>같은 조건의 장단점 비교</h2><span class="filter-summary">선택 ${rows.length}건 · 완전한 비교 ${paired.length}건</span></div><div class="table-wrap"><table><thead><tr><th>조건</th><th>정답 / 판정 기록</th><th>오류</th><th>Prompt tokens 합계</th><th>Completion tokens 합계</th><th>DB hits 합계</th></tr></thead><tbody>${arms.map(arm=>{
      const r=paired.filter(s=>s.arm===arm), scored=r.filter(s=>typeof s.correct==="boolean");
      return `<tr><td>${esc(armNames[arm] || arm)}<br><small class="muted">${esc(arm)} · ${r.length}건</small></td><td>${r.filter(s=>s.correct===true).length} / ${scored.length}</td><td>${r.filter(s=>s.error).length}</td><td class="numeric">${total(r,"prompt_tokens")}</td><td class="numeric">${total(r,"completion_tokens")}</td><td class="numeric">${total(r,"db_hits")}</td></tr>`;
    }).join("")}</tbody></table></div><p class="table-caption">모든 arm에 동일 요청·반복이 정확히 한 건씩 있는 표본만 비교합니다. ${rows.length-paired.length}건은 무효·누락·중복 또는 비교 조건 부족으로 비교에서 제외하고 아래에 보존합니다. 오류 표본은 포함하며 미기록 비용을 0으로 채우지 않습니다. 기존 측정값의 기술 통계입니다.</p>`;
    const sorted=[...rows].sort((a,b)=>a.question_id.localeCompare(b.question_id) || a.repeat-b.repeat || a.arm.localeCompare(b.arm));
    $("#sample-table").innerHTML=sorted.length ? `<div class="table-wrap"><table><thead><tr><th>요청 / 반복</th><th>조건</th><th>원본 판정</th><th>모델 호출</th><th>DB hits</th><th>DB ms</th></tr></thead><tbody>${sorted.map(s=>`<tr><td><button class="table-button" data-episode="${esc(s.episode_id)}">${esc(titles[s.question_id] || s.question_id)}</button><br><small class="muted">${esc(s.question_id)} · r${s.repeat}</small></td><td>${esc(armNames[s.arm] || s.arm)}</td><td>${badge(s.error ? "오류" : s.correct===true ? "정답" : s.correct===false ? "오답":"판정 없음",s.error ? "amber":s.correct===true ? "green":"red")}</td><td class="numeric">${num(s.model_calls)}</td><td class="numeric">${num(s.db_hits)}</td><td class="numeric">${num(s.db_ms)}</td></tr>`).join("")}</tbody></table></div>`:`<div class="empty">이 조건에 해당하는 기록이 없습니다.</div>`;
    document.querySelectorAll("[data-episode]").forEach(b=>b.onclick=()=>showEpisode(meta.key,b.dataset.episode));
  }
  ["#scope-filter","#question-filter","#repeat-filter"].forEach(id=>$(id).onchange=filter);filter();
}

async function showEpisode(key,id) {
  const dialog=$("#episode-dialog");if(!dialog.open) dialog.showModal();
  $("#episode-content").innerHTML=`<div class="loading">실행 원본을 읽는 중…</div>`;
  try {
    const data=await api(`/api/episode?${new URLSearchParams({key,episode:id})}`);
    const s=data.sample, c=data.conversation, d=s.decisions || {}, executions=s.executions || [];
    const stages=c.stages || [];
    const request=c.question || stages.find(x=>x.role==="planner")?.user || stages.find(x=>x.role==="executor")?.user?.split("\n\n")[0];
    const cypher=s.cypher || d.initial_cypher || c.cypher || executions[0]?.cypher;
    const params=executions[0]?.params || c.bound_params;
    const actualRows=executions.map(e=>({phase:e.phase, row_count:e.row_count ?? (typeof e.rows === "number" ? e.rows : null), completeness:e.completeness}));
    const ledgerFields=[['prompt_tokens','Prompt tokens'],['completion_tokens','Completion tokens'],['reasoning_tokens','Reasoning tokens'],['model_calls','모델 호출'],['db_hits','DB hits'],['db_ms','DB ms'],['server_total_latency_ms','서버 total ms'],['wall_ms','클라이언트 wall ms']];
    $("#episode-content").innerHTML=`<article class="episode-body"><div class="eyebrow">${esc(id)}</div><h2>${esc(titles[s.question_id] || s.question_id)}</h2><div class="episode-meta">${badge(armNames[s.arm] || s.arm)}${badge(data.meta.endpoint.model_name)}${badge(s.sf==null ? data.meta.graph.database || "SF 미기록":`SF${s.sf}`)}${badge(`repeat ${s.repeat}`)}${badge(s.correct===true ? "원본: 정답":s.correct===false ? "원본: 오답":"원본: 판정 미기록",s.correct ? "green":"amber")}</div><div class="two-col"><section class="panel"><h3>01 / 실제 Input</h3><span class="label">당시 사용자 요청</span>${pre(request)}<span class="label">harness 바인딩 · 실행 영수증</span>${pre(params)}${!params ? `<p class="small">개별 바인딩 미기록. run의 그래프 설명: ${esc(pretty(data.meta.graph))}</p>`:""}</section><section class="panel"><h3>02 / 온톨로지·에이전트 전달 내용</h3><span class="label">SEMANTIC CONTEXT / QUERY INTENT</span>${pre(c.semantic_context === "" ? "이 조건에는 추가 의미 문맥이 없습니다." : c.semantic_context ?? d.query_intent)}<p class="small">모델이 생성한 계획은 실행 가능한 스키마나 정답을 보증하지 않습니다.</p></section></div><section class="panel"><h3>03 / 생성한 Cypher</h3>${pre(cypher)}${s.error ? `<div class="error">${esc(s.error)}</div>`:""}</section><div class="two-col"><section class="panel"><h3>04 / 실제 Output 기록</h3>${pre(data.observed_output?.record ?? (actualRows.length ? actualRows : {row_count:s.rows ?? null}))}<p class="small">${data.observed_output ? esc(data.observed_output.meaning) : "row_count는 행 개수입니다. 이 기록에는 실제 행 값이 없으므로 Gold를 실제 답변으로 대신 표시하지 않습니다."}</p></section><section class="panel"><h3>05 / 기대값과 verifier 판정</h3><span class="label">평가기 GOLD · 실제 답변과 별도</span>${pre(s.score?.gold)}<span class="label">VERIFIER</span>${pre(d.verify_decision ?? (s.verifier_pass == null ? null : {pass:s.verifier_pass, reason_codes:s.verifier_reason_codes}))}</section></div><div class="section-top"><h3>모델과 데이터베이스의 비용 기록</h3></div><div class="meta-grid">${ledgerFields.map(([field,label])=>`<div><small>${label}</small><b>${num(s[field])}</b></div>`).join("")}</div><p class="run-note">미기록 값은 0이 아닙니다. 서버 시간과 클라이언트 시간은 각각의 측정 기준을 유지합니다.</p><details><summary>전체 sample과 대화 원본 보기</summary>${pre(s)}${pre(c)}</details><div class="source-list">${data.sources.map(p=>`<a target="_blank" rel="noopener" href="${esc(source(p))}">${esc(p.split("/").at(-1))} ↗</a>`).join("")}</div><div class="pin-bar"><select id="pin-contract" aria-label="근거를 연결할 실험 계약">${state.catalog.contracts.map(c=>`<option value="${c.id}">${esc(c.title)}</option>`).join("")}</select><button class="button primary" id="pin-episode">이 기록을 실험 근거에 연결</button></div><p class="small">기록의 연결은 가설을 입증하거나 판정을 자동으로 바꾸지 않습니다.</p></article>`;
    $("#pin-episode").onclick=async()=>{
      if(state.dirty) {toast("편집 중인 계약을 먼저 저장하세요. 입력 내용이 보존됩니다.");return;}
      const cid=$("#pin-contract").value, contract=state.catalog.contracts.find(x=>x.id===cid);
      if(contract.evidence.some(r=>r.run===key&&r.episode===id)){toast("이미 연결된 근거입니다.");return;}
      const button=$("#pin-episode");button.disabled=true;
      try{const saved=await api("/api/contracts",{...contract,evidence:[...contract.evidence,{run:key,episode:id}]});state.catalog.contracts=state.catalog.contracts.map(c=>c.id===cid ? saved:c);toast(`근거를 연결하고 v${saved.version}을 저장했습니다.`);}catch(e){toast(e.message);}finally{button.disabled=false;}
    };
  }catch(e){$("#episode-content").innerHTML=`<div class="error">${esc(e.message)}</div>`;}
}

function talkPage() {
  const {project,contracts,terms}=state.catalog;
  $("#main").innerHTML=heading("From evidence to story", "근거가 있는 발표 만들기", "금융 업무에서 시작해 온톨로지의 이점과 비용, 적용 한계를 설명합니다.")+
  `<div class="talk-title"><div class="eyebrow">WORKING TITLE · 제목 초안</div><h2>${esc(project.english_title)}</h2><p>${esc(project.title)}<br>${esc(project.subtitle)}</p></div><div class="two-col"><section class="panel"><h2>발표의 흐름</h2>${[
    ["금융 업무의 한 요청","입금 집계, 소유 관계, 상환 비율처럼 무엇을 답해야 하는지 먼저 보여줍니다."],
    ["업무 의미와 데이터의 간격","거래라는 말이 TRANSFER 관계가 되고, 상환액이 REPAY 합계가 되는 과정을 보여줍니다."],
    ["동일 요청의 변경 전후","물리 스키마와 의미 매핑 조건의 정확도·모델 비용·DB 비용을 함께 비교합니다."],
    ["도움이 된 조건과 손해 본 조건","성공 사례, 회귀 사례, 미지원 의미를 함께 보여주고 채택 범위를 설명합니다."],
  ].map(([title,body],i)=>`<div class="story-step"><span>0${i+1}</span><div><h3>${title}</h3><p>${body}</p></div></div>`).join("")}</section><section class="panel"><h2>내가 기록한 판정</h2>${contracts.map(c=>`<div class="mapping-row"><div class="card-top"><strong>${esc(c.title)}</strong>${badge(verdicts[c.conclusion],c.conclusion==="unreviewed" ? "amber":"green")}</div><p class="judgment">${esc(c.observation || "관측 내용을 아직 기록하지 않았습니다.")}</p><p class="judgment">${esc(c.interpretation || "해석을 작성하고 원본 근거를 연결하면 발표의 주장으로 검토할 수 있습니다.")}</p><small class="muted">연결한 근거 ${c.evidence.length}건 · v${c.version}</small><br><a class="text-link" href="#contracts?id=${c.id}">계약과 판단 근거 열기 ↗</a></div>`).join("")}</section></div><div class="section-top"><h2>의미 매핑의 적용 범위</h2>${linkSource("ontology/business_request_finbench.mapping.yaml","매핑 원본 ↗")}</div><section class="panel">${Object.entries(terms).map(([name,t])=>`<div class="mapping-row"><strong>${esc(name)}</strong> ${badge(t.status, t.status==="unsupported" ? "red":t.status==="mapped_with_limitation" ? "amber":"neutral")}<p>${esc((t.aliases || []).join(" · "))}</p><p>${esc(t.limitation || t.reason || t.compiler_note || `의미 상태: ${t.semantic?.status || "미기록"}`)}</p><details><summary>물리 매핑과 의미 정의</summary>${pre(t)}</details></div>`).join("")}</section><div class="note">${esc(project.scope)}</div>`;
}

async function route() {
  const next=location.hash.slice(1) || "overview";
  if(state.dirty && next!==state.route && !confirm("저장하지 않은 계약 변경이 있습니다. 변경을 버리고 이동할까요?")) {history.replaceState(null,"",`#${state.route}`);return;}
  state.dirty=false;state.route=next;state.contract=null;state.version++;
  const [rawPage,query=""]=next.split("?");const page=labels[rawPage] ? rawPage:"overview";
  const params=new URLSearchParams(query);
  $("#breadcrumb").textContent=labels[page];
  document.querySelectorAll("[data-nav]").forEach(a=>{a.classList.toggle("active",a.dataset.nav===page);if(a.dataset.nav===page)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");});
  ({overview,cases:casesPage,contracts:contractsPage,evidence:evidencePage,talk:talkPage})[page](params);
  window.scrollTo(0,0);
}
$("#close-dialog").onclick=()=>{$("#episode-dialog").close();if(state.route.startsWith("contracts")&&!state.dirty)contractsPage(new URLSearchParams(state.route.split("?")[1]||""));};
window.addEventListener("hashchange",route);
window.addEventListener("beforeunload",event=>{if(state.dirty){event.preventDefault();event.returnValue="";}});
api("/api/catalog").then(data=>{state.catalog=data;route();}).catch(error=>{$("#main").innerHTML=`<div class="error">저장소 기록을 불러오지 못했습니다: ${esc(error.message)}<br>서버 로그를 확인한 뒤 새로고침하세요.</div>`;});
