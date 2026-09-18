const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={partial:'部分完成',complete:'已完成',failed:'处理失败',insufficient_evidence:'证据不足',pending:'等待处理',observed:'已观测',unknown:'待确认',low_confidence:'待复核'};
const badge=s=>`<span class="badge ${['partial','complete','failed','observed','insufficient_evidence'].includes(s)?s:''}">${esc(labels[s]||s||'待确认')}</span>`;
let current={},selected=null,busy=false;
function notify(message=''){$('notice').hidden=!message;$('notice').textContent=message;}
async function request(url,options){const r=await fetch(url,options);const d=await r.json();if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'请求失败，请检查输入或稍后重试');return d;}
function view(name){['home','import','voice-test','analysis','models'].forEach(n=>$(n).hidden=n!==name);$('breadcrumb').textContent={home:'分析记录',import:'导入录音','voice-test':'语音对话测试',analysis:'录音分析',models:'模型管理'}[name];['home','import','voice-test','models'].forEach(n=>{const b=$('nav-'+n);if(b)b.classList.toggle('active',name===n);});notify();}
async function home(){view('home');$('runs').innerHTML='<div class="empty" role="status">正在读取分析记录…</div>';try{const d=await request('/api/runs');$('count').textContent=`所有记录 · ${d.runs.length}`;$('runs').innerHTML=d.runs.length?d.runs.map(r=>`<button class="run" data-run="${esc(r.run_id)}"><span class="run-icon">≋</span><span class="run-text"><strong>${esc(r.device||'未命名设备')}</strong><small>${esc(new Date(r.created*1000).toLocaleString('zh-CN'))} · ${esc(r.run_id)}</small></span>${badge(r.status)}<span class="quiet">→</span></button>`).join(''):'<div class="panel empty"><strong>你的第一份分析，从这里开始</strong>导入一段对话录音，建立可追溯的评测记录。</div>';$('runs').querySelectorAll('[data-run]').forEach(b=>b.onclick=()=>openRun(b.dataset.run));}catch(e){$('runs').innerHTML='';notify(e.message);}}
function importView(){view('import');}
function voiceTestView(){view('voice-test');vtTab('fixed');}
function vtTab(name){document.querySelectorAll('[data-vt-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.vtTab===name)));$('vt-fixed').hidden=name!=='fixed';$('vt-free').hidden=name!=='free';}
function choose(f){if(!f)return;if(!/\.(wav|mp3|m4a)$/i.test(f.name)){notify('请选择 WAV、MP3 或 M4A 文件');return;}selected=f;$('file-label').textContent=f.name;$('file-size').textContent=`${(f.size/1024/1024).toFixed(1)} MB · 已选择，准备分析`;notify();}
$('file').onchange=e=>choose(e.target.files[0]);$('drop').ondragover=e=>{e.preventDefault();$('drop').classList.add('drag');};$('drop').ondragleave=()=>$('drop').classList.remove('drag');$('drop').ondrop=e=>{e.preventDefault();$('drop').classList.remove('drag');choose(e.dataTransfer.files[0]);$('file').required=false;};
const fields={device:'设备名称',hardware:'硬件版本',firmware:'固件版本',model:'AI 模型',prompt:'提示词版本',supplier:'供应商',environment:'测试环境',notes:'备注'};
$('fields').innerHTML=Object.entries(fields).map(([k,v])=>`<label>${v}<input name="${k}" maxlength="4000" placeholder="填写${v}"></label>`).join('');
$('upload-form').onsubmit=async e=>{e.preventDefault();if(!selected||busy)return;busy=true;$('submit').disabled=true;$('submit').textContent='正在分析…';$('progress').textContent='正在保存、转换与分析录音，请保持页面打开。';notify();const fd=new FormData($('upload-form'));fd.set('file',selected);try{const d=await request('/api/analyze',{method:'POST',body:fd});render(d);}catch(e){notify(e.message);}finally{busy=false;$('submit').disabled=false;$('submit').textContent='开始分析 →';$('progress').textContent='录音将保存在当前服务的数据目录中。';}};
async function openRun(id){try{render(await request('/api/runs/'+encodeURIComponent(id)));}catch(e){notify(e.message);}}
function render(d){current=d;view('analysis');$('analysis-title').textContent=d.profile?.device||'录音分析';$('run-id').textContent=d.run_id;$('status').innerHTML=badge(d.status);$('audio').src=d.audio_url||'/api/runs/'+encodeURIComponent(d.run_id)+'/audio';$('summary').innerHTML=[['语音片段',d.transcript?.segments?.length||d.fused_segments.length||d.acoustic_segments.length],['说话人聚类',(d.speaker_segments||[]).length],['对话轮次',d.turns.length],['已观测指标',d.metrics.filter(m=>m.status==='observed'&&m.value!=null).length],['待复核发现',d.findings.length]].map(([l,n])=>`<div class="stat">${l}<b>${n}</b></div>`).join('');tab('segments');if(d.reason)notify(d.reason);}
const metricNames={first_speech_latency_ms:'首次语音时延',feedback_latency_ms:'首次反馈时延',meaningful_response_latency_ms:'有效回答时延',turn_gap_ms:'轮次间隔',overlap_duration_ms:'重叠时长',overlap_ratio:'重叠比例',barge_in_stop_latency_ms:'打断停止时延',barge_in_success:'打断成功',false_endpoint_candidate:'错误端点（候选）',false_endpoint:'错误端点'};
const roleNames={tester:'测试者',device:'AI 设备',unknown:'角色待确认'};
const roleMethods={explicit_evidence:'用户人工指定',semantic_attribution:'历史机器提议（不用于新分析）',human_attribution:'人工复核',none:'等待人工确认'};
function roleEvidence(a){
  // A model's own confidence is not a calibrated accuracy; never show it as one.
  const bits=[roleMethods[a.method]||a.method||'无依据'];
  if(a.confidence_basis==='uncalibrated_model_self_report')bits.push('模型自评置信度，非校准正确率');
  else if(a.confidence_basis==='explicit_user_evidence')bits.push('显式证据');
  else if(a.confidence_basis==='human_review')bits.push('人工复核');
  if(a.needs_review)bits.push('待复核');
  return bits.join(' · ');
}
function attributionNote(d){
  const doc=d.attribution||{};const items=doc.attributions||[];
  if(!items.length)return '';
  const rows=items.map(a=>`<div class="segment"><div><strong>${esc(String(a.speaker_id).split(':').pop())}</strong> → ${esc(roleNames[a.role]||a.role)}<p>${esc(roleEvidence(a))}</p><p>${esc(a.reason||'')}</p></div></div>`).join('');
  const conflicts=(doc.conflicts||[]).map(c=>`<p>聚类 ${esc(String(c.speaker_id).split(':').pop())} 存在历史角色冲突；新分析只接受用户保存的人工角色。</p>`).join('');
  return '<h3>角色归属</h3><p>请由用户根据音频与转写证据确认每个聚类是测试者、AI 设备或未知。完成并保存前，不生成后续指标和正式测试报告。</p>'+conflicts+rows;
}
function diarizationNote(d){
  const stage=d.stages?.diarization||{};const scope=d.diarization_scope||{};const count=(d.speaker_segments||[]).length;
  if(!count)return '<p>说话人聚类：'+esc(labels[stage.status]||stage.status||'未运行')+' · '+esc(stage.reason||'尚未获得说话人分离证据')+'</p>';
  const natives=[...new Set((d.speaker_segments||[]).map(s=>s.native_speaker_id||'未知'))];
  return '<p>说话人聚类：'+esc(labels[stage.status]||stage.status)+' · 聚类数 '+count+' · 服务原生标签 '+esc(natives.join('、'))+
    (scope.invocation_id?' · 依据调用 '+esc(scope.invocation_id):'')+
    '</p><p>聚类只说明“哪些片段属于同一说话人”，不代表已确认谁是测试者、谁是设备。角色需人工复核或显式证据。</p>';
}
function audioQaNote(d){
  // Facts measured on the canonical artifact. These are never a recognition,
  // accuracy or acceptance verdict, so no pass/fail wording is used. The stage
  // state and reason are rendered even when no measurements are readable, so an
  // abstention (or a Run whose QA document is missing) is never silently dropped.
  const qa=d.audio_qa||{};const m=qa.measurements||null;const conditions=(m&&m.conditions)||[];const stage=d.stages?.audio_qa||{};
  if(!m&&!stage.status&&!stage.reason)return '';
  const facts=m&&m.duration_ms?`时长 ${(m.duration_ms/1000).toFixed(1)} s · ${esc(m.encoding||'')} · ${esc(m.channels??'')} 声道 · ${esc(m.sample_rate_hz??'')} Hz`:'';
  const rows=conditions.map(c=>`<li>${esc(c.condition_id)}：${esc(c.status)} — ${esc(c.basis||'')}<small>${esc(c.limitation||'')}</small></li>`).join('');
  const header=[facts,stage.status?esc(labels[stage.status]||stage.status):''].filter(Boolean).join(' · ');
  return '<h3>音频质量检查</h3><p>'+(header||'未获得音频质量测量')+'</p>'+
    (stage.reason?'<p>'+esc(stage.reason)+'</p>':'')+
    (rows?'<ul>'+rows+'</ul>':'')+
    (!m?'<p>本记录没有可读取的音频质量测量值。</p>':'')+
    '<p>以上只报告测量事实与有效性条件，不构成识别质量、准确率或验收结论。</p>';
}
function alignmentNote(d){
  // Acoustic boundaries and ASR speaker spans are independent evidence. This panel
  // reports the recorded overlap/coverage facts behind every assignment or abstention,
  // so a blank metric table is explainable instead of looking like dropped data. It
  // never turns a coverage measurement into an accuracy or acceptance claim.
  const a=d.alignment||{};const g=d.metrics_gap||{};const diag=a.diagnostics||{};
  if(!a.document_id&&!((g.reasons||[]).length))return '';
  const ms=v=>v==null?'—':(Number(v)/1000).toFixed(1)+' s';
  const pct=v=>v==null?'—':(Number(v)*100).toFixed(1)+'%';
  let html='<h3>声学片段与说话人跨度对齐</h3>';
  html+='<p>对齐状态：'+esc(labels[a.status]||a.status||'未运行')+(a.reason?' · '+esc(a.reason):'')+'</p>';
  if(a.policy)html+='<p>策略 '+esc(a.policy.policy_version||'')+' · 依据 '+esc(a.policy.overlap_basis||'')+'。声学边界与 ASR 时间戳互不替代，未匹配与冲突状态均保留。</p>';
  if(diag.acoustic_segment_count!=null){
    const low=diag.low_energy||{};
    html+='<div class="table-wrap"><table><thead><tr><th>对齐事实</th><th>值</th></tr></thead><tbody>'+
      `<tr><td>未匹配 acoustic 时长</td><td>${ms(diag.unmatched_acoustic_ms)}（${pct(diag.unmatched_acoustic_ratio)}）</td></tr>`+
      `<tr><td>未匹配 speaker 时长</td><td>${ms(diag.unmatched_speaker_ms)}（${pct(diag.unmatched_speaker_ratio)}）</td></tr>`+
      `<tr><td>冲突片段</td><td>${(diag.conflicted_acoustic_segment_ids||[]).length}</td></tr>`+
      `<tr><td>低能量片段（其中未匹配）</td><td>${low.segment_count??0}（${ms(low.unmatched_ms)}）</td></tr>`+
      '</tbody></table></div>';
  }
  const clusters=diag.per_cluster||[];
  if(clusters.length)html+='<p>逐聚类覆盖：'+clusters.map(c=>esc(String(c.speaker_id).split(':').pop())+' '+pct(c.coverage_ratio)).join(' · ')+'</p>';
  const reasons=g.reasons||[];
  if(reasons.length&&g.status!=='observed')html+='<h4>为何没有指标</h4><ul>'+reasons.map(r=>`<li>${esc(r.code)}（涉及 ${r.count} 个片段）：${esc(r.detail)}</li>`).join('')+'</ul>';
  html+='<p>以上为覆盖率测量事实，用于定位低音量设备缺口，不构成识别准确率或验收结论。</p>';
  return html;
}
function tab(name){document.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===name)));const d=current;let html='';if(name==='segments'){const segments=d.transcript?.segments?.length?d.transcript.segments:(d.fused_segments.length?d.fused_segments:d.acoustic_segments);const byNative=new Map((d.speaker_segments||[]).map(s=>[String(s.native_speaker_id),s]));const roles=new Map(((d.attribution||{}).attributions||[]).map(a=>[a.speaker_id,a.role]));const clusterOf=s=>byNative.get(String(s.speaker_id));html='<h2>转写与语音片段</h2><p>转写时间是 ASR 估计，非精确声学边界；说话人聚类与角色判定是两件事。</p>'+diarizationNote(d)+attributionNote(d)+alignmentNote(d)+segments.map(s=>{const c=clusterOf(s);const role=(c&&roles.get(c.speaker_id))||s.speaker_role||'unknown';const roleInfo=c?roles.get(c.speaker_id):null;const cluster=c?'<small>聚类 '+esc(String(c.speaker_id).split(':').pop())+'（服务原生标签 '+esc(String(c.native_speaker_id))+'）</small>':'';const review=s.role_attribution?.needs_review?' <small>待复核</small>':'';return `<div class="segment"><button data-time="${Number(s.start_ms)/1000}">${(Number(s.start_ms)/1000).toFixed(2)} – ${(Number(s.end_ms)/1000).toFixed(2)} s</button><div><strong>${esc(roleNames[role]||'角色待确认')}</strong>${review}${cluster}${s.text_attribution==='ambiguous_spans_speakers'?'<small>该句跨越多个说话人，未归属给任一角色</small>':''}<p>${esc(s.text||'暂无转写文本')}</p></div></div>`;}).join('');if(!segments.length)html+='<div class="empty">没有可用的语音片段，请查看报告中的处理状态。</div>';}
if(name==='metrics')html='<h2>评测指标</h2><p>缺少可靠证据时不显示数值，也不将其计为通过。</p><div class="table-wrap"><table><thead><tr><th>指标</th><th>结果</th><th>状态</th></tr></thead><tbody>'+d.metrics.map(m=>`<tr><td>${esc(metricNames[m.name]||m.name)}</td><td>${m.value==null?'—':esc(m.value)+' '+esc(m.unit||'')}</td><td>${badge(m.status)}</td></tr>`).join('')+'</tbody></table></div>'+(d.metrics.length?'':'<div class="empty">暂无可计算指标</div>'+alignmentNote(d));
if(name==='findings')html='<h2>发现与语义评估</h2>'+d.findings.map(f=>`<div class="segment"><div><strong>${esc(f.title||f.dimension||'待复核发现')}</strong><p>${esc(f.description||f.reason||'')}</p>${badge(f.status)}</div></div>`).join('')+(d.findings.length?'':'<p>暂无可确认的问题结论。这不代表设备已通过评测。</p>')+d.judge_results.map(j=>`<div class="segment"><div><strong>${esc(j.dimension)} · ${esc(j.decision)}</strong><p>${esc(j.reason)}</p>${badge(j.status)}</div></div>`).join('');
if(name==='report')html=(d.stages?.asr&&d.stages.asr.status!=='complete'?'<p>ASR：'+esc(labels[d.stages.asr.status]||d.stages.asr.status)+' · '+esc(d.stages.asr.reason||'')+'</p><button class="text-button" id="retry-asr">重试转写（可能再次计费）</button>':'')+audioQaNote(d)+'<h2>完整报告</h2><button class="text-button" id="download">下载 Markdown ↓</button><pre>'+esc(d.report_md||'分析报告尚未生成。原始导入证据已保留。')+'</pre>';$('detail').innerHTML=html;if($('retry-asr'))$('retry-asr').onclick=async()=>{if(busy)return;busy=true;$('retry-asr').disabled=true;try{render(await request('/api/runs/'+encodeURIComponent(d.run_id)+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({retry_asr:true})}));}catch(e){notify(e.message);}finally{busy=false;if($('retry-asr'))$('retry-asr').disabled=false;}};$('detail').querySelectorAll('[data-time]').forEach(b=>b.onclick=()=>{$('audio').currentTime=Number(b.dataset.time);$('audio').play().catch(()=>notify('暂时无法播放音频，请检查音频是否已成功标准化。'));});if($('download'))$('download').onclick=()=>{const url=URL.createObjectURL(new Blob([d.report_md],{type:'text/markdown;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=d.run_id+'.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};}
request('/health').then(d=>$('version').textContent='版本 '+d.version).catch(()=>$('version').textContent='服务未连接');home();
