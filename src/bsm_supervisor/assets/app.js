'use strict';
let token = '', cursor = 0, busy = false, session = 0, submitting = false;
let controller = new AbortController();
let inspectedUpgrade = null, upgradeInspection = 0, journalLoad = 0, followedJob = null;
let inspectedInstallation = null, installationInspection = 0, currentInstallation = null;
function lockConsole() {
  session++; controller.abort(); controller = new AbortController();
  token=''; cursor=0; busy=false; submitting=false; followedJob=null;
  clearUpgrade();
  el('upgrade-history').replaceChildren();
  el('upgrade-inspect').disabled=true;
  clearJournal();
  closeInstallation();
  el('bsm-git-ref').value='';
  el('upgrade-same-version').checked=false;
  el('release-allow-same').checked=false;
  el('service-wait').checked=false;
  el('available-release').replaceChildren();
  el('available-release').disabled=true;
  el('release-status').textContent='';
  el('reload-releases').disabled=false;
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
  document.querySelectorAll('[data-action]').forEach(button=>{button.disabled=submitting || !!el('jobs').querySelector('[data-running]') ||
    (upgradeRecoveryActions.includes(button.dataset.action) &&
      !inspectedUpgrade?.recovery.includes(button.dataset.action.slice(8)));});
}
const el = id => document.getElementById(id);
const upgradeRecoveryActions = ['upgrade-cancel','upgrade-resume','upgrade-restore'];
async function request(path, value) {
  const currentSession=session;
  const response = await fetch('/api/' + path, {method: value ? 'POST' : 'GET', headers: {Authorization: 'Bearer ' + token, ...(value ? {'Content-Type':'application/json'} : {})}, ...(value ? {body:JSON.stringify(value)} : {}), cache:'no-store', credentials:'omit', signal:controller.signal});
  let result={};
  try {
    const raw=await response.text();
    if(raw) result=JSON.parse(raw);
  } catch {}
  if(currentSession!==session) throw new DOMException('Session ended','AbortError');
  if (!response.ok) {
    if(response.status===401) {lockConsole(); el('error').textContent='Console token rejected. Reconnect to continue.'; throw new DOMException('Authentication ended','AbortError');}
    throw new Error(result.error || (response.status===401 ? 'Console token rejected.' : 'Request failed. Inspect operation history.'));
  }
  return result;
}
function options(id, items, label) {
  const select = el(id), previous = select.value;
  if (!items.length) {
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = 'None available';
    empty.disabled = true;
    select.replaceChildren(empty);
    return;
  }
  select.replaceChildren(...items.map(item => {const option = document.createElement('option'); option.value = item.id; option.textContent = label(item); return option;}));
  if (items.some(item => item.id === previous)) select.value = previous;
}
function actionLabel(action) {
  return ({'upgrade':'Upgrade BSM','stop':'Stop BSM Service','permissions-repair':'Repair BSM Permissions','install-git':'Install BSM From Git','upgrade-installation':'Upgrade BSM to Git Build','installation-archive-failed':'Move Failed BSM Build Aside','upgrade-cancel':'Cancel BSM Upgrade','upgrade-resume':'Resume BSM Upgrade','upgrade-restore':'Restore Upgrade Checkpoint','service:start':'Start BSM','service:stop':'Stop BSM','service:graceful-stop':'Graceful stop','service:restart':'Restart BSM','service:unit-refresh':'Refresh managed unit','supervisor:install':'Install supervisor','supervisor:update-preflight':'Supervisor update checks'})[action] || String(action).replaceAll(':',' · ').replaceAll('-',' ');
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
    const inspect=document.createElement('button');inspect.type='button';inspect.className='action-button';inspect.textContent='Inspect #'+item.id;inspect.setAttribute('data-recovery-ack','');inspect.onclick=()=>{el('recovery').value=String(item.id);el('recovery').focus?.();inspectOperation();};row.append(inspect);
    return row;
  }));
}
function createJobRow(id) {
  const row=document.createElement('details');
  row.className='job-card';
  row.dataset.id=id;

  const summary=document.createElement('summary');
  summary.className='job-heading';
  summary.title=id;
  const title=document.createElement('span');
  title.className='job-title';
  const action=document.createElement('strong');
  const time=document.createElement('time');
  time.className='job-time';
  title.append(action,time);
  const status=document.createElement('span');
  status.className='status-badge';
  summary.append(title,status);

  const body=document.createElement('div');
  body.className='job-body';
  const identity=document.createElement('p');
  identity.className='job-identity';
  identity.textContent='Job '+id;
  const output=document.createElement('pre');
  output.className='log';
  output.tabIndex=0;
  output.setAttribute('aria-label','Output for job '+id);
  const acknowledge=document.createElement('button');
  acknowledge.type='button';
  acknowledge.className='action-button';
  acknowledge.textContent='Acknowledge after inspection';
  acknowledge.setAttribute('data-recovery-ack','');
  acknowledge.onclick=()=>recoveryRequest(async()=>{
    if(!confirm('Confirm service and operation inspection? This clears the console job record.')) return;
    await request('jobs/'+id+'/acknowledge',{confirmed:true});
    await refresh();
  });
  const actions=document.createElement('div');
  actions.className='buttons job-actions';
  actions.append(acknowledge);
  body.append(identity,output,actions);
  row.append(summary,body);

  let loading=false, loadedState;
  async function loadOutput() {
    if(!row.open || loading) return;
    const state=row.dataset.state;
    if(loadedState===state && state!=='running' && state!=='interrupted') return;
    loading=true;
    try {
      const report=await request('jobs/'+id);
      if(output.textContent!==report.output) output.textContent=report.output || 'No output yet.';
      loadedState=state;
      if(row.dataset.state===state) renderJobLinks(row, {action:row.dataset.action,target:row.dataset.target},state,report);
    } catch(error) {
      if(error.name!=='AbortError') output.textContent=error.message;
    } finally {
      loading=false;
    }
  }
  row.addEventListener('toggle',loadOutput);
  return row;
}
function renderJobs(items) {
  const list=el('jobs');
  const existing=new Map([...list.querySelectorAll('.job-card')].map(row=>[row.dataset.id,row]));
  let previous=null;
  for(const item of items) {
    const row=existing.get(item.id) || createJobRow(item.id);
    existing.delete(item.id);
    const pending=['running','interrupted'].includes(item.state);
    const active=pending && item.execution_state!=='inactive';
    const state=pending && !active ? 'interrupted' : item.state;
    row.toggleAttribute('data-running',active);
    const stateChanged=row.dataset.state!==state;
    row.dataset.state=state;
    row.dataset.action=item.action;
    row.dataset.target=item.target || '';
    row.querySelector('strong').textContent=actionLabel(item.action);
    const time=row.querySelector('time');
    if(item.created_at) {
      time.dateTime=item.created_at;
      time.textContent=new Date(item.created_at).toLocaleString();
    }
    const status=row.querySelector('.status-badge');
    status.dataset.state=state;
    status.textContent=state.charAt(0).toUpperCase()+state.slice(1);
    row.querySelector('button').hidden=!pending || active;
    if(stateChanged) renderJobLinks(row,item,state);
    if(!row.parentNode) {
      // Insert each new job next to its immediate predecessor in the sorted list.
      if(previous) previous.after(row);
      else list.prepend(row);
    }
    previous=row;
    if(row.open && (active || stateChanged)) row.dispatchEvent(new Event('toggle'));
    if(followedJob===item.id) {
      followedJob=null;
      row.open=true;
      row.scrollIntoView?.({block:'nearest'});
      row.querySelector('summary').focus();
    }
  }
  for(const obsolete of existing.values()) obsolete.remove();
}

function focusOverview(id) {
  if(typeof chooseSection==='function') chooseSection('overview');
  const node=el(id);
  node.scrollIntoView?.({block:'nearest'});
  node.focus?.({preventScroll:true});
}
function focusUpgrade(id) {
  focusOverview('upgrade-history');
  const select=el('upgrade-history');
  if(id && [...select.options].some(option=>option.value===String(id))) select.value=String(id);
  if(select.value) return el('upgrade-inspect').onclick();
}
function renderJobLinks(row,item,state,report={}) {
  for(const link of row.querySelectorAll('[data-job-link]')) link.remove();
  if(!['failed','interrupted'].includes(state)) return;
  const actions=row.querySelector('.job-actions');
  function link(label,callback) {
    const button=document.createElement('button');
    button.type='button';
    button.className='action-button';
    button.textContent=label;
    button.setAttribute('data-recovery-ack','');
    button.setAttribute('data-job-link','');
    button.onclick=callback;
    actions.append(button);
  }
  if(item.action==='upgrade' || item.action==='upgrade-installation' || upgradeRecoveryActions.includes(item.action)) {
    link('Review Upgrade Recovery',()=>focusUpgrade(report.workflow_id ||
      (upgradeRecoveryActions.includes(item.action) && item.target)));
  }
  if(item.action==='upgrade-installation' || item.action.startsWith('installation-')) {
    link('Inspect Build and Logs',()=>inspectInstallation(item.target));
  } else if(item.action==='install' && item.target && !['stable','prerelease'].includes(item.target.toLowerCase())) {
    link('Inspect Build and Logs',()=>inspectInstallation('release-'+item.target));
  }
  if(report.operation_id) link('Inspect Operation',()=>{
    focusOverview('recovery');
    el('recovery').value=String(report.operation_id);
    return inspectOperation();
  });
  else link('Review Operation History',()=>focusOverview('operations'));
  if(item.action==='graceful-stop') link('Go to Service Controls',()=>focusOverview('stop-service'));
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
    currentInstallation=state.selection?.id || null;
    updateInstallationControls();
    options('checkpoint',state.checkpoints,item=>item.id+(item.invalid?' · invalid':''));
    renderOperationRows(state.operations);renderWorkspace(state);
    renderUpgrades(state.workflows || []);
    const pendingUpgrade=(state.workflows || []).find(item=>item.state==='running');
    if(pendingUpgrade) {
      el('summary-attention').textContent='Upgrade #'+pendingUpgrade.id;
      el('summary-attention').dataset.state='attention';
      el('summary-attention-detail').textContent='Review BSM upgrades';
    }
    if(olderBefore===null) {
      if(state.operations.length===100) olderBefore=state.operations[state.operations.length-1].id;
      el('history-more').hidden=olderBefore===null;
    }
    const pendingOperation=state.operations.find(item=>['running','interrupted'].includes(item.state));
    el('notice').textContent=pendingUpgrade
      ? 'Upgrade #'+pendingUpgrade.id+' is pending. Inspect it under BSM upgrades.'
      : pendingOperation ? 'Operation #'+pendingOperation.id+' needs inspection. Check its details and current service state before acknowledging in Recovery.' : '';
    renderJobs(state.jobs);
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
el('error').onclick=()=>{el('error').textContent='';};
el('login').addEventListener('submit', event=>{event.preventDefault(); const credential=el('token').value.trim(); if(!credential){el('error').textContent='Enter a console token to connect.'; return;} lockConsole(); token=credential; el('token').value=''; refresh();});
el('logout').onclick=lockConsole;
el('refresh').onclick=refresh;
el('installation').onchange=updateInstallationControls;
function updateInstallationControls() {
  const id=el('installation').value;
  const git=!!id?.startsWith('git-');
  el('upgrade-installation').hidden=!git || id===currentInstallation;
  el('upgrade-same-option').hidden=el('upgrade-installation').hidden;
  el('inspect-installation').hidden=!id || !/^(git-|release-)/.test(id);
}
function closeInstallation() {
  installationInspection++;
  inspectedInstallation=null;
  el('installation-inspector').hidden=true;
  delete el('installation-inspector').dataset.id;
  el('installation-summary').textContent='';
  el('installation-log').textContent='';
}
function inspectInstallation(id) {
  if(!id) return;
  return recoveryRequest(async()=>{
    closeInstallation();
    const inspection=installationInspection;
    el('installation-inspector').hidden=false;
    el('installation-inspector').dataset.id=id;
    el('installation-summary').textContent='Loading '+id+'…';
    focusOverview('reload-installation');
    for(const name of ['archive-failed-installation','remove-inspected-installation','build-upgrade-recovery']) el(name).hidden=true;
    let report;
    try {
      report=await request('installations/'+encodeURIComponent(id));
    } catch(error) {
      if(inspection!==installationInspection || error.name==='AbortError') return;
      el('installation-summary').textContent=error.message;
      return;
    }
    if(inspection!==installationInspection) return;
    inspectedInstallation=report;
    const summary=[id,report.registered?.version || (report.directory_exists?'Unregistered build':'No build directory'),report.blocker];
    el('installation-summary').textContent=summary.filter(Boolean).join(' · ');
    el('installation-log').textContent=report.output || 'No build log available.';
    el('build-upgrade-recovery').hidden=!report.pending_upgrade;
    el('archive-failed-installation').hidden=!report.can_archive;
    el('remove-inspected-installation').hidden=!report.registered || id===currentInstallation;
    updateButtons();
  });
}
el('reload-installation').onclick=()=>inspectInstallation(el('installation-inspector').dataset.id);
el('build-upgrade-recovery').onclick=()=>focusUpgrade(inspectedInstallation?.pending_upgrade);
el('inspect-installation').onclick=()=>inspectInstallation(el('installation').value);
el('operation-installation-inspect').onclick=()=>inspectInstallation(inspectedOperation?.installation_id);
el('upgrade-installation-inspect').onclick=()=>inspectInstallation(inspectedUpgrade?.installation_id);
document.querySelectorAll('[data-action]').forEach(button=>button.onclick=async()=>{
  if(submitting) return;
  const currentSession=session;
  el('error').textContent='';
  const action=button.dataset.action, value={action};
  if(['install','upgrade'].includes(action)) {
    value.target=el('version').value.trim();
    if(action==='upgrade' && el('release-allow-same').checked) value.allow_same=true;
  }
  else if(['start','restart'].includes(action) && el('service-wait').checked) value.wait=true;
  else if(action==='install-git') {
    value.target=el('bsm-git-ref').value.trim();
    value.ref_kind=el('bsm-git-kind').value;
    if(!value.target) {el('error').textContent='Enter a Git branch, tag, or commit.'; return;}
    if(!confirm('Build '+value.ref_kind+' '+value.target+' from the official BSM repository? This runs its build code locally.')) return;
    value.trust_source=true;
  }
  else if(action==='upgrade-installation') {
    value.target=el('installation').value;
    value.allow_same=el('upgrade-same-version').checked;
  }
  else if(['installation-archive-failed','installation-remove'].includes(action)) {
    if(!inspectedInstallation) return;
    value.target=inspectedInstallation.id;
  }
  else if(action==='activate') value.target=el('installation').value;
  else if(['checkpoint-verify','restore-plan','checkpoint-restore'].includes(action)) value.target=el('checkpoint').value;
  else if(upgradeRecoveryActions.includes(action)) {
    if(!inspectedUpgrade?.recovery.includes(action.slice(8))) return;
    value.target=String(inspectedUpgrade.workflow.id);
  }
  else if(['acknowledge','restore-recover'].includes(action)) value.target=el('recovery').value;
  if(!['start','install','install-git','checkpoint-verify','restore-plan'].includes(action)) {
    const prompt=action==='upgrade-cancel'
      ? 'Cancel upgrade #'+value.target+'? The supervisor may reselect and restart the original BSM version.'
      : action==='stop'
        ? 'Stop BSM through systemd? If a failed graceful stop is pending, inspect and acknowledge its operation first.'
      : action==='installation-archive-failed'
        ? 'Move '+value.target+' aside so it can be rebuilt? Stop BSM first. The original directory will be kept.'
        : action==='installation-remove'
          ? 'Permanently remove '+value.target+'? Stop BSM first.'
          : 'Run '+action+'? This may interrupt BSM. Offline actions also require confirmation that BSM is not running outside its managed service.';
    if(!confirm(prompt)) return;
    if(currentSession!==session) return;
    value.confirmed=true;
  }
  if(['upgrade','upgrade-installation'].includes(action)) {if(!confirm('Allow BSM database migrations? Recovery may require restoring a checkpoint and discarding newer data.')) return; if(currentSession!==session) return; value.accept_migrations=true;}
  if(['checkpoint-restore','upgrade-restore'].includes(action)) {if(!confirm('Replace saved data and discard changes made since this checkpoint?')) return; if(currentSession!==session) return; value.restore_data=true;}
  submitting=true; updateButtons();
  try {const job=await request('actions',value); if(currentSession!==session) return; el('notice').textContent='Queued '+job.action+' · '+job.id;
    if(action==='upgrade' || action.startsWith('upgrade-')) clearUpgrade();
    if(action.startsWith('upgrade') || action.startsWith('install') || ['start','restart','graceful-stop','stop'].includes(action)) followedJob=job.id;
    if(action.startsWith('installation-')) closeInstallation();
    await refresh();} catch(error) {if(error.name!=='AbortError' && currentSession===session) el('error').textContent=error.message;} finally {if(currentSession===session) {submitting=false;updateButtons();}}
});
setInterval(refresh,3000);

let inspectedOperation=null, inspectedUpdate=null, olderBefore=null;
function recoveryLock(){el('operation-installation-inspect').hidden=true;inspectedOperation=null;inspectedUpdate=null;olderBefore=null;el('history-more').hidden=false;for(const id of ['recovery-report','older-history','update-report','deployment-info'])el(id).textContent='';}
async function recoveryRequest(task){const currentSession=session;try{await task();}catch(error){if(error.name!=='AbortError'&&currentSession===session)el('error').textContent=error.message;}}
async function inspectOperation(){return recoveryRequest(async()=>{const id=el('recovery').value.trim();if(!/^[1-9][0-9]{0,14}$/.test(id))throw new Error('Enter an operation ID from history.');inspectedOperation=await request('recovery/operations/'+id);el('recovery-report').textContent=JSON.stringify(inspectedOperation,null,2);el('operation-installation-inspect').hidden=!inspectedOperation.installation_id;});}
el('operation-inspect').onclick=inspectOperation;
el('operation-acknowledge').onclick=()=>recoveryRequest(async()=>{const id=el('recovery').value.trim();if(!inspectedOperation||String(inspectedOperation.operation.id)!==id)throw new Error('Inspect this operation first.');if(!confirm(inspectedOperation.effect))return;await request('recovery/operations/'+id+'/acknowledge',{confirmed:true,digest:inspectedOperation.digest});inspectedOperation=null;el('operation-installation-inspect').hidden=true;el('recovery-report').textContent='Interruption acknowledged. No data restored and no service restarted.';await refresh();});
el('history-more').onclick=()=>recoveryRequest(async()=>{const report=await request('history'+(olderBefore?'?before='+olderBefore:''));const section=document.createElement('div');renderOperationRows(report.operations,section);el('older-history').append(section);olderBefore=report.next_before;el('history-more').hidden=!olderBefore;});
el('update-inspect').onclick=()=>recoveryRequest(async()=>{inspectedUpdate=await request('recovery/update');el('update-report').textContent=JSON.stringify(inspectedUpdate,null,2);});
el('update-cancel').onclick=()=>recoveryRequest(async()=>{if(!inspectedUpdate?.cancellable)throw new Error('Inspect the pending update first. Only an uninstalled preparing update can be cancelled.');if(!confirm(inspectedUpdate.effect))return;await request('recovery/update/cancel',{confirmed:true,digest:inspectedUpdate.digest});inspectedUpdate=null;el('update-report').textContent='Update plan archived. No rollback or service restart performed.';await refresh();});

function clearUpgrade() {
  upgradeInspection++;
  inspectedUpgrade=null;
  el('upgrade-summary').textContent='';
  el('upgrade-steps').replaceChildren();
  el('upgrade-recovery').hidden=true;
  el('upgrade-installation-inspect').hidden=true;
}
function renderUpgrades(workflows) {
  const select=el('upgrade-history'), previous=select.value;
  const upgrades=workflows.filter(item=>item.kind==='upgrade').map(item=>({...item,id:String(item.id)}));
  const ordered=[...upgrades.filter(item=>item.state==='running'), ...upgrades.filter(item=>item.state!=='running')];
  options('upgrade-history',ordered,item=>'#'+item.id+' · '+(item.state==='running'?'Pending':item.state)+(item.detail?' · '+item.detail:''));
  el('upgrade-inspect').disabled=!ordered.length;
  if(previous!==select.value || (inspectedUpgrade && !upgrades.some(item=>
    item.id===select.value && item.state===inspectedUpgrade.workflow.state))) clearUpgrade();
}
el('upgrade-history').onchange=clearUpgrade;
el('upgrade-inspect').onclick=()=>recoveryRequest(async()=>{
  const id=el('upgrade-history').value;
  if(!id) return;
  clearUpgrade();
  const inspection=upgradeInspection;
  const report=await request('upgrades/'+id);
  if(inspection!==upgradeInspection || el('upgrade-history').value!==id) return;
  inspectedUpgrade=report;
  el('upgrade-installation-inspect').hidden=!report.installation_id;
  const versions=report.original_version && report.target_version
    ? report.original_version+' → '+report.target_version : '';
  el('upgrade-summary').textContent=[
    'Upgrade #'+id, versions, report.phase || 'No upgrade plan recorded',
    report.workflow.detail || report.last_error, report.checkpoint_id && 'Checkpoint '+report.checkpoint_id,
  ].filter(Boolean).join(' · ');
  renderOperationRows(report.steps,el('upgrade-steps'));
  el('upgrade-recovery').hidden=!report.recovery.length;
  for(const action of ['resume','cancel','restore']) {
    el('upgrade-'+action).hidden=!report.recovery.includes(action);
  }
  updateButtons();
});

function clearJournal() {
  journalLoad++;
  el('service-log-output').textContent='';
  el('service-logs-status').textContent='';
  el('service-logs-reload').disabled=false;
}
async function loadJournal() {
  clearJournal();
  const load=journalLoad;
  el('service-logs-reload').disabled=true;
  el('service-logs-status').textContent='Loading recent logs…';
  try {
    const report=await request('logs/'+el('service-log-source').value);
    if(load!==journalLoad) return;
    el('service-log-output').textContent=report.output;
    el('service-logs-status').textContent=report.output?'Showing recent journal entries.':'No journal entries found.';
  } catch(error) {
    if(load===journalLoad && error.name!=='AbortError') el('service-logs-status').textContent=error.message;
  } finally {
    if(load===journalLoad) el('service-logs-reload').disabled=false;
  }
}
el('service-log-source').onchange=loadJournal;
el('service-logs-reload').onclick=loadJournal;
el('service-logs-open').onclick=()=>{
  el('service-logs-panel').scrollIntoView?.({block:'nearest'});
  el('service-log-source').focus();
  return loadJournal();
};

el('reload-releases').onclick=async()=>{
  const activeSession=session;
  const button=el('reload-releases');
  button.disabled=true;
  el('release-status').textContent='Loading compatible releases…';
  try {
    const releases=await request('releases');
    const selector=el('available-release');
    const placeholder=document.createElement('option');
    placeholder.value='';
    placeholder.textContent='Choose a version';
    selector.replaceChildren(placeholder);
    for(const [label,prerelease] of [['Stable',false],['Prerelease and development',true]]) {
      const group=document.createElement('optgroup');
      group.label=label;
      for(const release of releases.filter(item=>item.prerelease===prerelease)) {
        const option=document.createElement('option');
        option.value=release.version;
        option.textContent=release.version;
        group.append(option);
      }
      if(group.children.length) selector.append(group);
    }
    selector.disabled=!releases.length;
    el('release-status').textContent=releases.length?'':'No compatible releases found.';
  } catch(error) {
    if(activeSession===session && error.name!=='AbortError') el('release-status').textContent=error.message;
  } finally {
    if(activeSession===session) button.disabled=false;
  }
};
el('available-release').onchange=()=>{
  if(el('available-release').value) el('version').value=el('available-release').value;
};
