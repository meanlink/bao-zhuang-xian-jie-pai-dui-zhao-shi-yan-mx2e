import {jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {chart} from '../components/common/chart.js';
import {table} from '../components/common/table.js';
import {status} from '../components/common/status.js';
import {JobEventName} from '../constants/enums.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
function eventTimeline(events){
 if(!events.length)return '';
 return '<section class="panel"><div class="section-title"><h2>任务经过</h2><span>领取 · 失联 · 接手 · 取消全程留痕</span></div><ul class="timeline job-events">'+events.map(e=>'<li class="event-'+esc(e.event)+'"><div><strong>'+esc(JobEventName[e.event]||e.event)+'</strong><span class="event-meta">第 '+(e.attempt||0)+' 次领取 · '+esc(e.actor)+'</span>'+(e.event==='reclaimed'&&e.detail?.previous_worker?'<span class="event-meta">原领取者：'+esc(e.detail.previous_worker)+'</span>':'')+(e.event==='lost'?'<span class="event-meta">租约到期未续租</span>':'')+(e.event==='stale_result_rejected'?'<span class="event-meta">旧领取者迟交，当前为第 '+esc(e.detail?.current_attempt)+' 次领取（'+esc(e.detail?.current_worker)+'）</span>':'')+(e.detail?.error?'<span class="event-meta">'+esc(e.detail.error)+'</span>':'')+'</div><span>'+date(e.at)+'</span></li>').join('')+'</ul></section>';
}
function runningMeta(j){
 if(j.status!=='running')return '';
 return '<span class="lease-meta">领取端 '+esc(j.worker_id||'-')+' · 第 '+j.attempt+' 次领取 · 心跳 '+date(j.heartbeat_at)+'</span>';
}
export async function results(id){
 workspace.jobs=await jobs.list();const j=id?await jobs.get(id):(workspace.jobs.find(j=>j.id===workspace.selectedJob)||workspace.jobs[0]);
 if(j&&!workspace.jobs.some(x=>x.id===j.id))workspace.jobs.push(j);
 if(!j)return heading('04 / REVIEW','结果复核','尚无分析结果。')+'<div class="empty">先到分析工作台，选择批次并提交一次计算。<a href="#/workbench">前往工作台 ↗</a></div>';
 workspace.selectedJob=j.id;const r=j.result||{};
 const canCancel=(session.user.role==='admin'||session.user.role==='analyst')&&(j.status==='queued'||j.status==='running');
 const events=await jobs.events(j.id);
 return heading('04 / REVIEW','结果复核与业务解释',j.name,'<button class="button" id="refresh-results">刷新结果 ↻</button>')
 +'<div class="result-selector"><label>分析记录<select id="select-job">'+workspace.jobs.map(x=>'<option value="'+x.id+'" '+(x.id===j.id?'selected':'')+'>'+esc(x.name)+' · '+esc(x.status)+'</option>').join('')+'</select></label>'+status(j.status)+'<span>'+date(j.created_at)+'</span>'+runningMeta(j)+'<span class="approval">'+(j.approved?'已获业务批准':'待负责人复核')+'</span>'+(canCancel?'<button class="small" id="cancel-job">取消任务</button>':'')+'</div>'
 +(j.status==='completed'
   ?'<section class="result-lead"><p class="eyebrow">COMPUTED FINDING</p><h2>'+esc(r.summary)+'</h2><div class="metrics">'+(r.metrics||[]).map(m=>'<div><span>'+esc(m.label)+'</span><strong>'+esc(fmt(m.value))+'<small>'+esc(m.unit||'')+'</small></strong></div>').join('')+'</div></section><section class="panel">'+chart(r.chart)+'</section><div class="two-columns"><section class="panel"><div class="section-title"><h2>业务解释</h2><span>由计算结果生成</span></div><ol class="questions">'+(r.findings||[]).map(v=>'<li>'+esc(v)+'</li>').join('')+'</ol>'+(r.warnings||[]).map(v=>'<p class="warning">'+esc(v)+'</p>').join('')+'</section><section class="method-sheet compact"><p class="eyebrow">REPRODUCIBILITY</p><h3>计算口径与参数</h3><p>'+esc(r.method)+'</p><pre>'+esc(JSON.stringify(j.params,null,2))+'</pre>'+(session.user.role==='admin'&&!j.approved?'<button class="primary" id="approve-job">批准此业务结论</button>':'')+'<p class="hint">仅批准结果可进入定时分发档案。</p></section></div><section class="panel"><div class="section-title"><h2>结果明细</h2><span>'+(r.table?.rows?.length||0)+' 行</span></div>'+table(r.table?.columns||[],r.table?.rows||[])+'</section>'
   :'<section class="panel empty">'+(j.status==='failed'?'计算失败：'+esc(j.error)+'。请返回工作台调整参数或数据。':j.status==='cancelled'?'任务已取消，不再产出结果；如需该口径的比较请重新发起。':'后台正在执行计算。点击刷新结果查看最新进展。')+'<a href="#/workbench">返回工作台 ↗</a></section>')
 +eventTimeline(events);
}
export function bindResults(refresh){
 document.querySelector('#refresh-results')?.addEventListener('click',refresh);
 document.querySelector('#select-job')?.addEventListener('change',e=>{workspace.selectedJob=Number(e.target.value);location.hash='#/results/'+e.target.value;});
 document.querySelector('#approve-job')?.addEventListener('click',async()=>{try{await jobs.approve(workspace.selectedJob);notify('已批准，可创建分发计划');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelector('#cancel-job')?.addEventListener('click',async()=>{try{await jobs.cancel(workspace.selectedJob);notify('任务已取消，不再产出结果');await refresh();}catch(e){notify(e.message,true);}});
}
