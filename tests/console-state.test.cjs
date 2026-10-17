const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function harness() {
  const nodes=new Map(), buttons=[], pending=[];
  function element(){return {value:'',hidden:false,textContent:'',dataset:{},disabled:false,addEventListener(event,callback){this[event]=callback;},replaceChildren(){},querySelectorAll(){return [];},querySelector(){return null;}};}
  function el(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);}
  const state={service:{ActiveState:'active'},selection:null,installations:[],checkpoints:[],operations:[],jobs:[],schedules:[]};
  const context={AbortController,DOMException,document:{getElementById:el,querySelectorAll:()=>buttons,createElement:element},setInterval(){},confirm:()=>true,fetch:async(url,options)=>{
    if(url==='/api/state')return {ok:true,json:async()=>state};
    return new Promise(resolve=>pending.push(()=>resolve({ok:true,json:async()=>[]})));
  }};
  vm.createContext(context);vm.runInContext(fs.readFileSync('src/bsm_supervisor/assets/app.js','utf8'),context);
  return {el,pending,context};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('locking prevents a delayed response from reopening the console',async()=>{
 const h=harness();h.el('token').value='test-credential';h.el('login').submit({preventDefault(){}});await tick();
 assert.equal(h.pending.length,1);h.el('logout').onclick();h.pending.shift()();await tick();
 assert.equal(h.el('console').hidden,true);assert.equal(h.el('login').hidden,false);assert.equal(h.el('events').textContent,'');
});
test('a previous login cannot overwrite a newer session',async()=>{
 const h=harness();h.el('token').value='first';h.el('login').submit({preventDefault(){}});await tick();
 h.el('logout').onclick();h.el('token').value='second';h.el('login').submit({preventDefault(){}});await tick();
 h.pending[0]();await tick();assert.equal(h.el('console').hidden,true);
 h.pending[1]();await tick();assert.equal(h.el('console').hidden,false);
});
