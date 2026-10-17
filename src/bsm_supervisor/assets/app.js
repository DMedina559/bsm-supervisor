'use strict';
let token = '', cursor = 0, busy = false, session = 0, submitting = false;
let controller = new AbortController();
function lockConsole() {
  session++; controller.abort(); controller = new AbortController();
  token=''; cursor=0; busy=false; submitting=false;
  if(typeof adminLock==='function')adminLock();
  el('console').hidden=true; el('logout').hidden=true; el('login').hidden=false;
  for(const id of ['events','operations','jobs','schedules','service','selected','notice','error','autostart-summary']) el(id).textContent='';
  for(const id of ['installation','checkpoint']) el(id).replaceChildren();
}
function updateButtons() {
  if(typeof updateAdminButtons==='function')updateAdminButtons();
  document.querySelectorAll('[data-action]').forEach(button=>{button.disabled=submitting || !!el('jobs').querySelector('[data-running]');});
}
const el = id => document.getElementById(id);
async function request(path, value) {
  const currentSession=session;
  const response = await fetch('/api/' + path, {method: value ? 'POST' : 'GET', headers: {Authorization: 'Bearer ' + token, ...(value ? {'Content-Type':'application/json'} : {})}, ...(value ? {body:JSON.stringify(value)} : {}), cache:'no-store', credentials:'omit', signal:controller.signal});
  const result=await response.json();
  if(currentSession!==session) throw new DOMException('Session ended','AbortError');
  if (!response.ok) {
    if(response.status===401) {lockConsole(); el('error').textContent='Console token rejected. Reconnect to continue.'; throw new DOMException('Authentication ended','AbortError');}
    throw new Error(result.error || (response.status===401 ? 'Console token rejected.' : 'Request failed. Inspect operation history.'));
  }
  return result;
}
function options(id, items, label) {
  const select = el(id), previous = select.value;
  select.replaceChildren(...items.map(item => {const option = document.createElement('option'); option.value = item.id; option.textContent = label(item); return option;}));
  if (items.some(item => item.id === previous)) select.value = previous;
}
function rows(id, items, label) {if(!items.length) {el(id).textContent='No operations recorded.';return;} el(id).replaceChildren(...items.map(item => {const row=document.createElement('div'); row.className='row'; row.textContent=label(item); return row;}));}
async function refresh() {
  if (!token || busy) return;
  busy = true;
  const currentSession=session;
  try {
    const state = await request('state');
    if(currentSession!==session) return;
    el('service').dataset.state=state.service.error ? 'unavailable' : state.service.ActiveState;
    el('service').textContent=state.service.error ? 'Service status unavailable: '+state.service.error : (state.service.ActiveState || 'unknown')+' · '+(state.service.SubState || 'unknown')+(state.service.MainPID && state.service.MainPID!=='0' ? ' · PID '+state.service.MainPID : '');
    el('autostart-summary').textContent=state.autostart?.reason || '';
    el('selected').textContent=state.selection ? 'Selected: '+state.selection.id+' · '+state.selection.version : 'No version selected';
    options('installation',state.installations,item=>item.id+' · '+item.version);
    options('checkpoint',state.checkpoints,item=>item.id+(item.invalid?' · invalid':''));
    rows('operations',state.operations,item=>'#'+item.id+' · '+item.action+' · '+item.state+(item.detail?' · '+item.detail:''));
    const pendingOperation=state.operations.find(item=>['running','interrupted'].includes(item.state));
    el('notice').textContent=pendingOperation ? 'Operation #'+pendingOperation.id+' needs inspection. Check its details and current service state before acknowledging in Recovery.' : '';
    const opened=new Set([...el('jobs').querySelectorAll('details[open]')].map(row=>row.dataset.id));
    el('jobs').replaceChildren(...state.jobs.map(item=>{
      const row=document.createElement('details'), summary=document.createElement('summary'), output=document.createElement('pre');
      row.className='row'; if(['running','interrupted'].includes(item.state)) row.dataset.running='true'; row.dataset.id=item.id; row.open=opened.has(item.id); summary.textContent=item.id+' · '+item.action+' · '+item.state;
      row.append(summary,output); if(['running','interrupted'].includes(item.state)){const acknowledge=document.createElement('button');acknowledge.className='action-button';acknowledge.textContent='Acknowledge after inspection';acknowledge.setAttribute('data-recovery-ack','');acknowledge.onclick=async()=>{if(!confirm('Confirm service and operation inspection? This only acknowledges console bookkeeping.'))return;try{await request('jobs/'+item.id+'/acknowledge',{confirmed:true});await refresh();}catch(error){el('error').textContent=error.message;}};row.append(acknowledge);} row.addEventListener('toggle',async()=>{if(row.open) {try {output.textContent=(await request('jobs/'+item.id)).output;} catch(error) {output.textContent=error.message;}}}); return row;
    }));
    if(typeof renderAdmin==='function')renderAdmin(state);
    el('schedules').textContent=state.schedules.length ? JSON.stringify(state.schedules,null,2) : 'No schedules configured.';
    const progress=await request('events?after='+cursor);
    if(currentSession!==session) return;
    for(const event of progress) {cursor=event.id; el('events').textContent += '#'+event.operation_id+' · '+event.state+(event.detail?' · '+event.detail:'')+'\n';}
    el('events').textContent=el('events').textContent.slice(-20000);
    el('login').hidden=true; el('console').hidden=false; el('logout').hidden=false;
  } catch(error) {if(error.name!=='AbortError' && currentSession===session) el('error').textContent=error.message;} finally {if(currentSession===session) {busy=false;updateButtons();}}
}
el('login').addEventListener('submit', event=>{event.preventDefault(); const credential=el('token').value.trim(); lockConsole(); token=credential; el('token').value=''; refresh();});
el('logout').onclick=lockConsole;
el('refresh').onclick=refresh;
document.querySelectorAll('[data-action]').forEach(button=>button.onclick=async()=>{
  if(submitting) return;
  const currentSession=session;
  el('error').textContent='';
  const action=button.dataset.action, value={action};
  if(['install','upgrade'].includes(action)) value.target=el('version').value.trim();
  else if(action==='activate') value.target=el('installation').value;
  else if(['checkpoint-verify','restore-plan','checkpoint-restore'].includes(action)) value.target=el('checkpoint').value;
  else if(['acknowledge','upgrade-resume','upgrade-cancel','upgrade-restore','restore-recover'].includes(action)) value.target=el('recovery').value;
  if(!['start','install','checkpoint-verify','restore-plan'].includes(action)) {
    if(!confirm('Run '+action+'? This may interrupt BSM. Offline actions also require confirmation that BSM is not running outside its managed service.')) return;
    value.confirmed=true;
  }
  if(action==='upgrade') {if(!confirm('Allow BSM database migrations? Recovery may require restoring a checkpoint and discarding newer data.')) return; value.accept_migrations=true;}
  if(['checkpoint-restore','upgrade-restore'].includes(action)) {if(!confirm('Replace saved data and discard changes made since this checkpoint?')) return; value.restore_data=true;}
  submitting=true; updateButtons();
  try {const job=await request('actions',value); if(currentSession!==session) return; el('notice').textContent='Queued '+job.action+' · '+job.id; await refresh();} catch(error) {if(error.name!=='AbortError' && currentSession===session) el('error').textContent=error.message;} finally {if(currentSession===session) {submitting=false;updateButtons();}}
});
setInterval(refresh,3000);
