'use strict';
let token = '', cursor = 0, busy = false;
const el = id => document.getElementById(id);
async function request(path, value) {
  const response = await fetch('/api/' + path, {method: value ? 'POST' : 'GET', headers: {Authorization: 'Bearer ' + token, ...(value ? {'Content-Type':'application/json'} : {})}, ...(value ? {body:JSON.stringify(value)} : {}), cache:'no-store', credentials:'omit'});
  if (!response.ok) throw new Error(response.status === 401 ? 'Console token rejected.' : 'Request blocked or failed. Check operation history and the container journal.');
  return response.json();
}
function options(id, items, label) {
  const select = el(id), previous = select.value;
  select.replaceChildren(...items.map(item => {const option = document.createElement('option'); option.value = item.id; option.textContent = label(item); return option;}));
  if (items.some(item => item.id === previous)) select.value = previous;
}
function rows(id, items, label) {el(id).replaceChildren(...items.map(item => {const row=document.createElement('div'); row.className='row'; row.textContent=label(item); return row;}));}
async function refresh() {
  if (!token || busy) return;
  busy = true;
  try {
    const state = await request('state');
    el('service').textContent=JSON.stringify(state.service,null,2);
    el('selected').textContent=state.selection ? 'Selected: '+state.selection.id+' · '+state.selection.version : 'No version selected';
    options('installation',state.installations,item=>item.id+' · '+item.version);
    options('checkpoint',state.checkpoints,item=>item.id+(item.invalid?' · invalid':''));
    rows('operations',state.operations,item=>'#'+item.id+' · '+item.action+' · '+item.state+(item.detail?' · '+item.detail:''));
    const opened=new Set([...el('jobs').querySelectorAll('details[open]')].map(row=>row.dataset.id));
    el('jobs').replaceChildren(...state.jobs.map(item=>{
      const row=document.createElement('details'), summary=document.createElement('summary'), output=document.createElement('pre');
      row.className='row'; row.dataset.id=item.id; row.open=opened.has(item.id); summary.textContent=item.id+' · '+item.action+' · '+item.state;
      row.append(summary,output); row.addEventListener('toggle',async()=>{if(row.open) {try {output.textContent=(await request('jobs/'+item.id)).output;} catch(error) {output.textContent=error.message;}}}); return row;
    }));
    el('schedules').textContent=JSON.stringify(state.schedules,null,2);
    const progress=await request('events?after='+cursor);
    for(const event of progress) {cursor=event.id; el('events').textContent += '#'+event.operation_id+' · '+event.state+(event.detail?' · '+event.detail:'')+'\n';}
    el('events').textContent=el('events').textContent.slice(-20000);
    el('login').hidden=true; el('console').hidden=false; el('logout').hidden=false; el('error').textContent='';
  } catch(error) {el('error').textContent=error.message;} finally {busy=false;}
}
el('login').addEventListener('submit', event=>{event.preventDefault(); token=el('token').value.trim(); el('token').value=''; refresh();});
el('logout').onclick=()=>{token=''; cursor=0; el('console').hidden=true; el('logout').hidden=true; el('login').hidden=false; for(const id of ['events','operations','jobs','schedules','service','selected']) el(id).textContent=''; for(const id of ['installation','checkpoint']) el(id).replaceChildren();};
el('refresh').onclick=refresh;
document.querySelectorAll('[data-action]').forEach(button=>button.onclick=async()=>{
  const action=button.dataset.action, value={action};
  if(['install','upgrade'].includes(action)) value.target=el('version').value;
  else if(action==='activate') value.target=el('installation').value;
  else if(['checkpoint-verify','restore-plan','checkpoint-restore'].includes(action)) value.target=el('checkpoint').value;
  else if(['acknowledge','upgrade-resume','restore-recover'].includes(action)) value.target=el('recovery').value;
  if(!['start','install','checkpoint-verify','restore-plan'].includes(action)) {
    if(!confirm('Run '+action+'? This may interrupt BSM. Offline actions also require confirmation that BSM is not running outside its managed service.')) return;
    value.confirmed=true;
  }
  if(action==='upgrade') {if(!confirm('Allow BSM database migrations? Recovery may require restoring a checkpoint and discarding newer data.')) return; value.accept_migrations=true;}
  if(action==='checkpoint-restore') {if(!confirm('Replace saved data and discard changes made since this checkpoint?')) return; value.restore_data=true;}
  try {const job=await request('actions',value); el('notice').textContent='Queued '+job.action+' · '+job.id; await refresh();} catch(error) {el('error').textContent=error.message;}
});
setInterval(refresh,3000);
