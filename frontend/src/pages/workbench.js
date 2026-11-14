import {datasets,jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {status} from '../components/common/status.js';
import {esc,notify,date} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
function queuePanel(list){
 const active=list.filter(j=>j.status==='queued'||j.status==='running');
 const canWrite=session.user.role!=='viewer';
 return '<section class="panel queue-panel"><div class="section-title"><h2>排队与计算中</h2><span>多端接力 · 失联自动接手</span></div>'+(active.length?'<div class="table-wrap"><table><thead><tr><th>任务</th><th>状态</th><th>领取端 / 次数</th><th>提交时间</th><th></th></tr></thead><tbody>'+active.map(j=>'<tr><td>'+esc(j.name)+'</td><td>'+status(j.status)+'</td><td>'+(j.worker_id?esc(j.worker_id)+' · 第 '+j.attempt+' 次':'等待领取')+'</td><td>'+date(j.created_at)+'</td><td>'+(canWrite?'<button class="small cancel-queued" data-id="'+j.id+'">取消</button>':'')+'</td></tr>').join('')+'</tbody></table></div>':'<p class="hint">当前没有排队或计算中的比较。</p>')+'<p class="hint">计算端失联后租约到期自动由其他端接手；取消为终态，重启也不会复活。</p></section>';
}
export async function workbench(){
 workspace.datasets=await datasets.list();
 workspace.jobs=await jobs.list();
 const m=workspace.meta;
 return heading('03 / ANALYSIS LAB',m.workbench_title,'选择业务批次和分析口径。参数、输入指纹与计算结论随任务保存，便于复核和复现。')
 +'<div class="workbench-grid"><section class="panel"><form id="job-form"><label>分析任务名称<input name="name" required minlength="2" value="'+esc(m.name)+' · 复核试算"></label><label>数据批次<select name="dataset_id" required>'+workspace.datasets.map(d=>'<option value="'+d.id+'">'+esc(d.name)+' · '+d.rows+' 条</option>').join('')+'</select></label><div class="parameter-grid">'+m.parameters.map(p=>'<label>'+esc(p.label)+(p.type==='select'?'<select name="param-'+esc(p.key)+'">'+p.options.map(o=>'<option value="'+esc(o.value)+'" '+(o.value==p.default?'selected':'')+'>'+esc(o.label)+'</option>').join('')+'</select>':'<input type="number" step="any" name="param-'+esc(p.key)+'" value="'+esc(p.default)+'" min="'+esc(p.min??'')+'" max="'+esc(p.max??'')+'" required>')+'</label>').join('')+'</div><button type="submit" class="primary" '+(session.user.role==='viewer'||!workspace.datasets.length?'disabled':'')+'>'+esc(m.action_label||'执行分析')+' ↗</button><p class="hint">提交后由后台任务计算。观察员可查看参数和结果，无法提交。</p></form></section><section class="method-sheet"><p class="eyebrow">METHOD / BUSINESS RULE</p><h2>'+esc(m.method)+'</h2><p>'+esc(m.tagline)+'</p><h3>业务判读边界</h3><ul>'+m.limitations.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul><div class="method-trace"><span>输入</span><strong>'+esc(m.dataset_name)+'</strong><span>输出</span><strong>可复核结果 / 参数版本 / 分发归档</strong></div></section></div>'
 +queuePanel(workspace.jobs);
}
export function bindWorkbench(){
 submit(document.querySelector('#job-form'),async f=>{
  const params={};
  for(const p of workspace.meta.parameters){const value=f.get('param-'+p.key);params[p.key]=p.type==='number'?Number(value):value;}
  const j=await jobs.create({name:f.get('name'),dataset_id:Number(f.get('dataset_id')),params});
  workspace.selectedJob=j.id;notify('分析任务已提交');location.hash='#/results/'+j.id;
 });
 document.querySelectorAll('.cancel-queued').forEach(btn=>btn.addEventListener('click',async()=>{
  try{await jobs.cancel(Number(btn.dataset.id));notify('任务已取消，不再产出结果');location.hash='#/results/'+btn.dataset.id;}
  catch(e){notify(e.message,true);}
 }));
}
