const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
class Element {
  constructor(id = '') { this.id = id; this.listeners = {}; this.children = []; this.style = {};
    this.value = ''; this.disabled = false; this.clientWidth = 400; this.clientHeight = 300; }
  addEventListener(kind, callback) { this.listeners[kind] = callback; }
  setAttribute() {}
  getContext() { return {clearRect(){}, drawImage(){}, beginPath(){}, lineTo(){}, moveTo(){},
    closePath(){}, stroke(){}, fill(){}, set strokeStyle(_v){}, set lineWidth(_v){}, set fillStyle(_v){}}; }
  append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  get nextSibling() { if (!this.parentNode) return null; const i=this.parentNode.children.indexOf(this);
    return this.parentNode.children[i+1]||null; }
  insertBefore(item, next) { if(item.parentNode){const old=item.parentNode.children.indexOf(item);if(old>=0)item.parentNode.children.splice(old,1)}item.parentNode = this; const i = this.children.indexOf(next);
    this.children.splice(i < 0 ? this.children.length : i, 0, item); }
  getBoundingClientRect() { return {left: 0, top: 0, width: 400, height: 300}; }
}
for (const id of ['#task','#image','#status','#annotator','#target','#image-size','#progress',
  '#finish','#undo','#remove-object','#clear','#save','#next','#previous','#tasks']) elements.set(id,new Element(id));
const tasks = ['a','b'].map(task_id => ({task_id,annotation_target:'outer boundary',width:1200,height:900,has_mask:true}));
const images = {}, annotations = {}, context = {Promise,Math,JSON,encodeURIComponent,console,
  document:{querySelector:s=>elements.get(s),createElement:()=>new Element()},
  localStorage:{getItem:()=>'',setItem(){}},window:{innerWidth:800,innerHeight:700,confirm:()=>false},
  Image:class { set src(url) { this.id=decodeURIComponent(url.split('/').pop()); }
    get naturalWidth(){return 1200} get naturalHeight(){return 900}
    decode(){return new Promise(resolve=>{images[this.id]=resolve})} },
  fetch:(url,options={})=>{
    if(url==='/api/session') return Promise.resolve({json:async()=>({tasks})});
    if(url==='/api/annotation') { context.savedPayload=JSON.parse(options.body);
      return new Promise(resolve=>{context.finishSave=()=>resolve({ok:true,json:async()=>({revision:2})})}); }
    const id=decodeURIComponent(url.split('/').pop());
    return new Promise(resolve=>{annotations[id]=resolve});
  }};
const canvas=elements.get('#image');
canvas.addEventListener=Element.prototype.addEventListener;
new Element('annotation-panel').append(canvas);
canvas.width=300;canvas.height=150;
const api=vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=async()=>{for(let i=0;i<12;i++) await Promise.resolve()};
(async()=>{
  await tick(); assert.ok(images.a,'initial image request started');
  images.a(); await tick(); assert.ok(annotations.a,'initial saved-mask request started');
  const selectingB=api.selectTask('b'); images.b(); await tick(); assert.ok(annotations.b,'second saved-mask request started');
  annotations.b({ok:true,status:200,json:async()=>({polygons:[[[0.1,0.1],[0.7,0.1],[0.7,0.7]]]})});
  await selectingB; annotations.a({ok:true,status:200,json:async()=>({polygons:[[[0.2,0.2],[0.9,0.2],[0.9,0.9]]]})});
  await tick(); assert.equal(api.state.current,'b'); assert.equal(api.state.preview.id,'b');
  assert.equal(api.state.polygons[0][0][0],0.1,'late response cannot replace task B masks');
  const panel=canvas.parentNode.parentNode,zoom=panel.children[0].children.find(item=>item.type==='range');
  zoom.value='2';zoom.listeners.input();assert.equal(canvas.style.width,'2400px','zoom resizes the source-coordinate canvas');
  const pointer=canvas.listeners.pointerdown;
  for (const [x,y] of [[10,10],[100,10],[100,100]]) pointer({clientX:x,clientY:y});
  assert.equal(api.state.dirty,true,'new vertices mark the task dirty');
  assert.equal(await api.selectTask('a'),false,'navigation can be cancelled with unsaved contours');
  assert.equal(api.state.current,'b');
  elements.get('#finish').listeners.click(); elements.get('#annotator').value='reviewer-test';
  const saving=elements.get('#save').listeners.click(); await tick();
  assert.equal(api.state.saving,true); assert.equal(await api.selectTask('a'),false);
  context.finishSave(); await saving; assert.equal(context.savedPayload.task_id,'b');
  assert.equal(canvas.style.maxWidth,'none'); assert.equal(typeof elements.get('#image').getContext,'function');
  console.log('annotation UI stale-load, save-target, dirty-edit and zoom checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
