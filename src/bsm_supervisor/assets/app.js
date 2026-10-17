'use strict';
let token = '', cursor = 0, busy = false, session = 0, submitting = false;
let controller = new AbortController();
function lockConsole() {
  session++; controller.abort(); controller = new AbortController();
  token=''; cursor=0; busy=false; submitting=false;
  if(document.body)document.body.dataset.connected='false';
  el('console-connection').textContent='Locked';el('connection-chip').dataset.state='locked';
  for(const id of ['sidebar-version','sidebar-installation','summary-service','summary-version','summary-backups','summary-attention','summary-service-detail','summary-version-detail','summary-backups-detail','summary-attention-detail'])el(id).textContent='';
  if(typeof recoveryLock==='function')recoveryLock();
  if(typeof adminLock==='function')adminLock();
  el('console').hidden=true; el('logout').hidden=true; el('login').hidden=false;
  for(const id of ['events','operations','jobs','schedules','service','selected','notice','error','autostart-summary']) el(id).textContent='';
  for(const id of ['installation','checkpoint']) el(id).replaceChildren();
  if(typeof el('token').focus==='function')el('token').focus();
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
function actionLabel(action) {
  return ({'service:start':'Start BSM','service:stop':'Stop BSM','service:graceful-stop':'Graceful stop','service:restart':'Restart BSM','service:unit-refresh':'Refresh managed unit','supervisor:install':'Install supervisor','supervisor:update-preflight':'Supervisor update checks'})[action] || String(action).replaceAll(':',' · ').replaceAll('-',' ');
}
function renderOperationRows(items, container=el('operations')) {
  if(!items.length){container.textContent='No operations recorded. Service and maintenance history will appear here.';return;}
  const opened=new Set([...container.querySelectorAll('details[open]')].map(row=>row.dataset.id));
  container.replaceChildren(...items.map(item=>{
    const row=document.createElement(item.detail?'details':'div'), heading=document.createElement(item.detail?'summary':'div');
    row.className='row';row.dataset.id=String(item.id);if(item.detail)row.open=opened.has(String(item.id));
    heading.className='operation-summary';
    const id=document.createElement('span'),label=document.createElement('span'),badge=document.createElement('span');
    id.className='operation-id';id.textContent='#'+item.id;label.className='operation-label';label.textContent=actionLabel(item.action);
    badge.className='status-badge';badge.dataset.state=item.state;badge.textContent=item.state==='running'&&item.detail?'Needs inspection':String(item.state).replaceAll('_',' ');
    heading.append(id,label,badge);row.append(heading);
    if(item.started_at){const time=document.createElement('time');time.className='operation-time';time.dateTime=item.started_at;time.textContent=new Date(item.started_at).toLocaleString();row.append(time);}
    if(item.detail){const detail=document.createElement('p');detail.className='operation-detail';detail.textContent=item.detail;row.append(detail);}
    const inspect=document.createElement('button');inspect.className='action-button';inspect.textContent='Inspect #'+item.id;inspect.setAttribute('data-recovery-ack','');inspect.onclick=()=>{el('recovery').value=String(item.id);inspectOperation();};row.append(inspect);
    return row;
  }));
}
function renderWorkspace(state) {
  const service=state.service||{},selected=state.selection,checkpoints=state.checkpoints||[];
  const pending=(state.operations||[]).filter(item=>['running','interrupted'].includes(item.state));
  const stateName=service.error?'unavailable':service.ActiveState||'unknown';
  const labels={active:'Running',inactive:'Stopped',failed:'Failed',activating:'Starting',deactivating:'Stopping',unavailable:'Unavailable',unknown:'Unknown'};
  el('summary-service').textContent=labels[stateName]||stateName;el('summary-service').dataset.state=stateName;
  el('summary-service-detail').textContent=service.MainPID&&service.MainPID!=='0'?'Process '+service.MainPID:'Managed systemd service';
  el('summary-version').textContent=selected?selected.version:'Not selected';
  el('summary-version-detail').textContent=selected?'Active installation selection':'Choose an installed build';
  el('summary-backups').textContent=String(checkpoints.filter(item=>!item.invalid).length);
  el('summary-backups-detail').textContent=checkpoints.some(item=>item.invalid)?'Some checkpoints need inspection':'Saved recovery points';
  el('summary-attention').textContent=pending.length?String(pending.length)+' to inspect':'All clear';
  el('summary-attention').dataset.state=pending.length?'attention':'clear';
  el('summary-attention-detail').textContent=pending.length?'Review operation history':'No pending interruptions';
  el('sidebar-installation').textContent=selected?'BSM '+selected.version:'No version selected';
  el('sidebar-version').textContent='Supervisor '+(state.supervisor?.version||'unknown');
}
async function refresh() {
  if (!token || busy) return;
  busy = true;
  const currentSession=session;
  try {
    const state = await request('state');
    if(currentSession!==session) return;
    el('service').dataset.state=state.service.error ? 'unavailable' : state.service.ActiveState;
    el('service').textContent=state.service.error ? 'Unavailable' : (state.service.ActiveState || 'unknown')+' · '+(state.service.SubState || 'unknown')+(state.service.MainPID && state.service.MainPID!=='0' ? ' · PID '+state.service.MainPID : '');
    el('autostart-summary').textContent=state.service.error?'Cannot read the managed service. Run diagnostics to inspect it.':state.autostart?.reason || '';
    el('selected').textContent=state.selection ? 'Selected: '+state.selection.id+' · '+state.selection.version : 'No version selected';
    options('installation',state.installations,item=>item.id+' · '+item.version);
    options('checkpoint',state.checkpoints,item=>item.id+(item.invalid?' · invalid':''));
    renderOperationRows(state.operations);renderWorkspace(state);
    if(olderBefore===null && state.operations.length)olderBefore=state.operations[state.operations.length-1].id;
    const pendingOperation=state.operations.find(item=>['running','interrupted'].includes(item.state));
    el('notice').textContent=pendingOperation ? 'Operation #'+pendingOperation.id+' needs inspection. Check its details and current service state before acknowledging in Recovery.' : '';
    const opened=new Set([...el('jobs').querySelectorAll('details[open]')].map(row=>row.dataset.id));
    el('jobs').replaceChildren(...state.jobs.map(item=>{
      const row=document.createElement('details'), summary=document.createElement('summary'), output=document.createElement('pre');
      row.className='row'; if(['running','interrupted'].includes(item.state) && item.execution_state!=='inactive') row.dataset.running='true'; row.dataset.id=item.id; row.open=opened.has(item.id); summary.textContent=actionLabel(item.action)+' · '+item.state+' · '+item.id.slice(0,8);summary.title=item.id;
      const identity=document.createElement('p');identity.className='job-identity';identity.textContent='Job '+item.id;
      row.append(summary,identity,output); if(['running','interrupted'].includes(item.state)){const acknowledge=document.createElement('button');acknowledge.className='action-button';acknowledge.textContent='Acknowledge after inspection';acknowledge.setAttribute('data-recovery-ack','');acknowledge.onclick=async()=>{if(!confirm('Confirm service and operation inspection? This only acknowledges console bookkeeping.'))return;try{await request('jobs/'+item.id+'/acknowledge',{confirmed:true});await refresh();}catch(error){el('error').textContent=error.message;}};row.append(acknowledge);} row.addEventListener('toggle',async()=>{if(row.open) {try {output.textContent=(await request('jobs/'+item.id)).output;} catch(error) {output.textContent=error.message;}}}); return row;
    }));
    if(typeof renderAdmin==='function')renderAdmin(state);
    el('schedules').textContent=state.schedules.length ? JSON.stringify(state.schedules,null,2) : 'No schedules configured.';
    const progress=await request('events?after='+cursor);
    if(currentSession!==session) return;
    for(const event of progress) {cursor=event.id; el('events').textContent += '#'+event.operation_id+' · '+event.state+(event.detail?' · '+event.detail:'')+'\n';}
    el('events').textContent=el('events').textContent.slice(-20000);
    const firstUnlock=el('console').hidden;
    el('login').hidden=true; el('console').hidden=false; el('logout').hidden=false;
    if(firstUnlock&&typeof el('page-title').focus==='function')el('page-title').focus({preventScroll:true});
    if(document.body)document.body.dataset.connected='true';
    el('console-connection').textContent='Connected';el('connection-chip').dataset.state='connected';
  } catch(error) {if(error.name!=='AbortError' && currentSession===session) {el('error').textContent=error.message;if(token){el('console-connection').textContent='Unavailable';el('connection-chip').dataset.state='unavailable';}}} finally {if(currentSession===session) {busy=false;updateButtons();}}
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

let inspectedOperation=null, inspectedUpdate=null, olderBefore=null;
function recoveryLock(){inspectedOperation=null;inspectedUpdate=null;olderBefore=null;el('history-more').hidden=false;for(const id of ['recovery-report','older-history','update-report','deployment-info'])el(id).textContent='';}
async function recoveryRequest(task){const currentSession=session;try{await task();}catch(error){if(error.name!=='AbortError'&&currentSession===session)el('error').textContent=error.message;}}
async function inspectOperation(){return recoveryRequest(async()=>{const id=el('recovery').value.trim();if(!/^[1-9][0-9]{0,14}$/.test(id))throw new Error('Enter an operation ID from history.');inspectedOperation=await request('recovery/operations/'+id);el('recovery-report').textContent=JSON.stringify(inspectedOperation,null,2);});}
el('operation-inspect').onclick=inspectOperation;
el('operation-acknowledge').onclick=()=>recoveryRequest(async()=>{const id=el('recovery').value.trim();if(!inspectedOperation||String(inspectedOperation.operation.id)!==id)throw new Error('Inspect this operation first.');if(!confirm(inspectedOperation.effect))return;await request('recovery/operations/'+id+'/acknowledge',{confirmed:true,digest:inspectedOperation.digest});inspectedOperation=null;el('recovery-report').textContent='Interruption acknowledged. No data restored and no service restarted.';await refresh();});
el('history-more').onclick=()=>recoveryRequest(async()=>{const report=await request('history'+(olderBefore?'?before='+olderBefore:''));const section=document.createElement('div');renderOperationRows(report.operations,section);el('older-history').append(section);olderBefore=report.next_before;el('history-more').hidden=!olderBefore;});
el('update-inspect').onclick=()=>recoveryRequest(async()=>{inspectedUpdate=await request('recovery/update');el('update-report').textContent=JSON.stringify(inspectedUpdate,null,2);});
el('update-cancel').onclick=()=>recoveryRequest(async()=>{if(!inspectedUpdate?.cancellable)throw new Error('Inspect the pending update first. Only an uninstalled preparing update can be cancelled.');if(!confirm(inspectedUpdate.effect))return;await request('recovery/update/cancel',{confirmed:true,digest:inspectedUpdate.digest});inspectedUpdate=null;el('update-report').textContent='Update plan archived. No rollback or service restart performed.';await refresh();});
