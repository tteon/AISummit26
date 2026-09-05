// Run against an isolated workspace server, never the user's saved contracts.
// node tests/workspace_browser_smoke.mjs http://127.0.0.1:8766 http://127.0.0.1:9225
import assert from "node:assert/strict";
import {writeFile} from "node:fs/promises";

const base=process.argv[2] || "http://127.0.0.1:8766";
const debug=process.argv[3] || "http://127.0.0.1:9225";
const tabs=await (await fetch(`${debug}/json/list`)).json();
const tab=tabs.find(t=>t.type==="page");
const socket=new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
const pending=new Map();let sequence=0;const errors=[];
socket.onmessage=event=>{
  const message=JSON.parse(event.data);
  if(message.id){const p=pending.get(message.id);pending.delete(message.id);message.error ? p.reject(new Error(message.error.message)) : p.resolve(message.result);}
  else if(message.method==="Runtime.exceptionThrown")errors.push(message.params.exceptionDetails.text+": "+message.params.exceptionDetails.exception?.description);
};
function send(method,params={}){return new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});}
function nextEvent(method){return new Promise((resolve,reject)=>{const timeout=setTimeout(()=>{socket.removeEventListener('message',listen);reject(new Error('Timed out: '+method));},15000);function listen(event){const message=JSON.parse(event.data);if(message.method===method){clearTimeout(timeout);socket.removeEventListener('message',listen);resolve(message.params);}}socket.addEventListener('message',listen);});}
async function evaluate(expression){const r=await send("Runtime.evaluate",{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);return r.result.value;}
async function waitFor(expression){await evaluate(`new Promise((resolve,reject)=>{const end=Date.now()+15000;const check=()=>{if(${expression})resolve(true);else if(Date.now()>end)reject(new Error('Timed out waiting for UI'));else setTimeout(check,80)};check()})`);}
async function route(hash,ready){await evaluate(`location.hash=${JSON.stringify(hash)}`);await waitFor(ready);}
async function screenshot(name){const shot=await send("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});await writeFile(`/tmp/finance-ontology-${name}.png`,Buffer.from(shot.data,"base64"));}

try{
  await send("Runtime.enable");await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride",{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  const loaded=nextEvent("Page.loadEventFired");await send("Page.navigate",{url:base});await loaded;
  await waitFor("document.querySelector('.hero')");
  assert.ok(await evaluate("document.body.innerText.includes('금융 AI에 온톨로지를 적용하면')"));
  await screenshot("overview");
  await route("cases","document.querySelector('#case-search')");
  await evaluate("document.querySelector('#case-search').value='repayment';document.querySelector('#case-search').dispatchEvent(new Event('input'))");
  assert.ok(await evaluate("document.querySelectorAll('[data-case]').length>0"));
  await route("evidence","document.querySelector('[data-episode]')");
  assert.ok(await evaluate("document.querySelector('#comparison').innerText.includes('완전한 비교')"));
  // Validate the displayed comparison against the original source, independently of report.summary.
  const actual=await evaluate(`(async()=>{
    const catalog=await (await fetch('/api/catalog')).json();
    const key=document.querySelector('#run-select').value;
    const meta=catalog.runs.find(r=>r.key===key);
    const raw=await (await fetch('/api/source?'+new URLSearchParams({path:meta.source}))).json();
    return {table:document.querySelector('#comparison').innerText,rows:raw.samples,arms:meta.arms};
  })()`);
  for(const arm of actual.arms){const total=actual.rows.filter(s=>s.arm===arm).reduce((n,s)=>n+s.prompt_tokens,0);assert.ok(actual.table.includes(total.toLocaleString('ko-KR')),`missing prompt total ${arm}`);}
  await screenshot("comparison");
  await evaluate("document.querySelector('[data-episode]').click()");
  await waitFor("document.querySelector('#pin-episode')");
  assert.ok(await evaluate("document.querySelector('#episode-content').innerText.includes('기대값과 verifier 판정')"));
  await evaluate("document.querySelector('#pin-episode').click()");
  await waitFor("document.querySelector('#toast').innerText.includes('저장했습니다') || document.querySelector('#toast').innerText.includes('이미 연결')");
  await evaluate("document.querySelector('#close-dialog').click()");
  await route("contracts?id=ontology-context","document.querySelector('#contract-form')");
  assert.ok(await evaluate("document.querySelector('#contract-evidence').innerText.includes('연결 해제')"));
  const observation="브라우저 검증: 비교 결과와 원본의 토큰 합계가 일치함.";
  await evaluate(`document.querySelector('[name=observation]').value=${JSON.stringify(observation)};document.querySelector('[name=observation]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#contract-form').requestSubmit()`);
  await waitFor("document.querySelector('#save-status').innerText.includes('v') && !document.querySelector('#save-status').innerText.includes('저장하지')");
  const reloaded=nextEvent("Page.loadEventFired");await send("Page.reload");await reloaded;
  await waitFor("document.querySelector('[name=observation]')");
  assert.equal(await evaluate("document.querySelector('[name=observation]').value"),observation);
  await evaluate("document.querySelector('#new-contract').click()");
  await waitFor("document.querySelector('[name=title]')?.value==='새 실험'");
  const exported=await (await fetch(base+"/api/export")).json();
  assert.ok(exported.contracts.length>=4);
  assert.ok(exported.history["ontology-context"].length>=2);
  // Agent envelope rows must be displayed as actual output, separately from Gold.
  const catalog=await (await fetch(base+"/api/catalog")).json();
  const agent=catalog.runs.find(r=>r.source.includes("20260903T_mara_stage_c_default_v1/models/gemma"));
  if(agent){
    await route(`evidence?run=${agent.key}&question=customer_inflow_summary`,"document.querySelector('[data-episode]')");
    await evaluate("[...document.querySelectorAll('[data-episode]')].find(b=>b.dataset.episode.endsWith('multi_envelope')).click()");
    await waitFor("document.querySelector('#pin-episode')");
    assert.ok(await evaluate("document.querySelector('#episode-content').innerText.includes('검증기 입력의 결과 행')"));
    await screenshot("episode");
    await evaluate("document.querySelector('#close-dialog').click()");
  }
  const pilot=catalog.runs.find(r=>r.name==="20260905T_mapping_pilot_v1");
  if(pilot){
    await route(`evidence?run=${pilot.key}&question=high_risk_rail_and_medium`,"document.querySelector('#protocol-outcome')");
    assert.ok(await evaluate("document.querySelector('#protocol-outcome').innerText.includes('판단 보류')"));
    await evaluate("[...document.querySelectorAll('[data-episode]')].find(b=>b.dataset.episode.endsWith('business_mapping')).click()");
    await waitFor("document.querySelector('#pin-episode')");
    assert.ok(await evaluate("document.querySelector('#episode-content').innerText.includes('OPEN_BANKING')"));
    assert.ok(await evaluate("document.querySelector('#episode-content').innerText.includes('PROFILE')"));
    await screenshot("pilot-output");
    await evaluate("document.querySelector('#close-dialog').click()");
    await route(`evidence?run=${pilot.key}&question=loan_applicant_facilities`,"document.querySelector('#question-filter')?.value==='loan_applicant_facilities' && document.querySelector('[data-episode]')");
    assert.ok(await evaluate("document.querySelector('#scope-filter').selectedOptions[0].textContent.includes('anchor 1')"));
    assert.equal(await evaluate("document.querySelectorAll('[data-episode]').length"),4);
    await screenshot("pilot-protocol");
  }
  await route("talk","document.querySelector('.talk-title')");
  assert.ok(await evaluate("document.querySelector('main').innerText.includes('mapped_with_limitation')"));
  await send("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await route("overview","document.querySelector('.hero')");
  assert.ok(await evaluate("document.documentElement.scrollWidth<=window.innerWidth"),"mobile overflow");
  await screenshot("mobile");
  assert.deepEqual(errors,[]);
  process.stdout.write("PASS: navigation, case search, source-derived comparison, episode I/O, evidence linking, contract save/reload/history, new contracts, export, talk, mobile layout; no browser exceptions.\n");
}finally{socket.close();}
