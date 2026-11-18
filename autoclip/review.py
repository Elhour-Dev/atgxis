"""Local review page: ranked clips with players, keep / trash / make-vertical buttons. Binds to 127.0.0.1 only."""
from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import Config
from .captions import CaptionsUnavailable, build_ass, cached_words, transcribe, words_in_window
from . import layout as lay
import html
from . import accounts, publish, settings, share        # noqa: F401  (publish registers the 'direct' sharing method)
from .cutter import export_clip, grab_frame, probe_size
from .describe import suggest as suggest_description
from .facecam import FacecamUnavailable, detect_facecam
from .layout import LayoutError, LayoutStore

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Clip review</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--card:#f4f4f6;--mut:#666;--acc:#6d28d9}
@media(prefers-color-scheme:dark){:root{--bg:#121214;--fg:#eee;--card:#1d1d21;--mut:#9a9aa3}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
header{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
h1{font-size:20px;margin:0 12px 0 0} select,button,input[type=number]{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid #8884;background:var(--card);color:var(--fg)}
button{cursor:pointer} button.primary{background:var(--acc);color:#fff;border-color:var(--acc)} button:disabled{opacity:.6;cursor:wait}
input[type=number]{width:74px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:16px}
.card{background:var(--card);border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:8px}
.card.keep{outline:2px solid #16a34a}.card.trash{opacity:.45}
video{width:100%;border-radius:8px;background:#000} .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.score{font-weight:700;color:var(--acc);font-size:18px}.mut{color:var(--mut);font-size:13px}
.chat{font-size:13px;background:#8881;border-radius:8px;padding:6px 8px;max-height:60px;overflow:auto}
.trim{background:#8881;border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:6px}
label{display:flex;gap:4px;align-items:center;font-size:14px}
.modal{position:fixed;inset:0;background:#000b;display:flex;align-items:flex-start;justify-content:center;overflow:auto;padding:16px;z-index:10}
.mbox{background:var(--bg);border-radius:14px;padding:14px;max-width:1180px;width:100%;display:flex;flex-direction:column;gap:10px}
.mcols{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start}
canvas{max-width:100%;border-radius:8px;touch-action:none;cursor:crosshair;background:#000}
.pv{height:560px;max-width:100%;border-radius:8px;background:#000;object-fit:contain}
.hint{font-size:13px;color:var(--mut)}
video.vert{max-height:360px;width:auto;align-self:flex-start}
.pw{position:relative;height:560px;aspect-ratio:9/16;background:#000;border-radius:8px;overflow:hidden;flex:none}
.pvimg{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;visibility:hidden}
.capbox{position:absolute;border:2px dashed #22c55e;background:#22c55e22;display:flex;align-items:center;justify-content:center;color:#fff;font:800 14px Arial,sans-serif;text-align:center;cursor:move;touch-action:none;overflow:hidden;box-sizing:border-box;text-shadow:0 0 4px #000,0 0 4px #000}
.caph{position:absolute;right:0;bottom:0;width:14px;height:14px;background:#22c55e;cursor:nwse-resize}
.acc{background:#8881;border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:6px}
.acc input[type=text],.acc input[type=password]{padding:6px;border-radius:6px;border:1px solid #8884;background:var(--bg);color:var(--fg)}
.acc ol{margin:4px 0 4px 18px;padding:0;font-size:13px;color:var(--mut)}
pre.share{white-space:pre-wrap;font:12px/1.4 system-ui;background:#8881;border-radius:6px;padding:6px;margin:4px 0;max-height:120px;overflow:auto}
details{background:#8881;border-radius:8px;padding:6px 8px}summary{cursor:pointer;font-size:14px}
details input[type=text],details textarea{width:100%;box-sizing:border-box;font:inherit;padding:6px;border-radius:6px;border:1px solid #8884;background:var(--bg);color:var(--fg);margin-top:6px}
</style>
<header><h1>Clip review</h1>
<select id=f><option value=new>To review</option><option value=keep>Kept</option><option value=trash>Trash</option><option value=all>All</option></select>
<select id=s><option value=score>Best score first</option><option value=created>Newest first</option></select>
<button id=folders>📁 Folders</button><button id=accts>🔗 Accounts</button><button id=purge>Delete trashed files</button><span class=mut id=count></span></header>
<div class=grid id=g></div>
<script>
let clips=[],sig='',shareInfo={providers:[],platforms:{}};const openPost=new Map(),openShare=new Map(),NL=String.fromCharCode(10);const trims=new Map();const g=document.getElementById('g');
const enc=id=>id.split('/').map(encodeURIComponent).join('/');
const fmt=t=>Math.floor(t/60)+':'+String(Math.floor(t%60)).padStart(2,'0');
const fileUrl=(c,f)=>'/files/'+enc(c.dir)+'/'+encodeURIComponent(f);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
async function load(force){
  const next=await (await fetch('/api/clips')).json();
  const ns=JSON.stringify(next.map(c=>[c.id,c.status,(c.exports||[]).length,c.shares||null]));
  const playing=[...document.querySelectorAll('video')].some(v=>!v.paused);
  clips=next;if(ns!==sig&&(force||!playing)){sig=ns;render()}}
function numInput(v,on){const i=el('input');i.type='number';i.step='0.1';i.min='0';i.value=v;i.oninput=()=>on(parseFloat(i.value));return i}
function render(){
  const f=document.getElementById('f').value,s=document.getElementById('s').value;
  let list=clips.filter(c=>f==='all'||c.status===f);
  list.sort((a,b)=>s==='score'?b.score-a.score:b.created.localeCompare(a.created));
  g.replaceChildren();document.getElementById('count').textContent=list.length+' clip(s)';
  for(const c of list){
    if(!trims.has(c.id)){const n=c.clip_len_s||30;let a=c.suggest_start_s,b=c.suggest_end_s;
      if(b-a>n+1){a=Math.min(Math.max(c.moment_s-12,0),Math.max(c.duration_s-n,0));b=Math.min(a+n,c.duration_s)}   // older, longer suggestions -> 30 s
      trims.set(c.id,{start:+a.toFixed(1),end:+b.toFixed(1),vertical:true,captions:false})}
    const t=trims.get(c.id);
    const card=el('div','card '+c.status);
    const v=el('video');v.controls=true;v.preload='metadata';v.src=fileUrl(c,c.file);
    v.onloadedmetadata=()=>{v.currentTime=t.start};
    v.ontimeupdate=()=>{if(!v.paused&&v.currentTime>t.end+0.2)v.currentTime=t.start};
    card.append(v);
    const r=el('div','row');r.append(el('span','score',Math.round(c.score)),el('span','mut',c.channel+' · '+c.created.replace('T',' ')+' · draft '+fmt(c.duration_s)+' · moment at '+fmt(c.moment_s)+(c.res?' · source '+c.res[0]+'x'+c.res[1]:'')));card.append(r);
    if(c.res&&c.res[1]<1000)card.append(el('div','hint','⚠ Recorded at '+c.res[1]+'p, so it cannot look sharp when enlarged to 1080x1920. Clips recorded after the update use the best quality the stream offers.'));
    const sp=c.spike;card.append(el('div','mut',sp.peak+' chatters at once (normal '+Math.round(sp.baseline)+') · '+sp.msgs+' msgs'+(c.loudness_jump_db!=null?' · audio '+(c.loudness_jump_db>0?'+':'')+c.loudness_jump_db+' dB':'')));
    if(sp.top&&sp.top.length)card.append(el('div','chat',sp.top.map(x=>x[0]+' ×'+x[1]).join('   ')));
    const tr=el('div','trim'),len=el('span','mut'),upd=()=>{len.textContent='= '+Math.max(t.end-t.start,0).toFixed(1)+' s'};
    const a=numInput(t.start,x=>{t.start=x;upd()}),b=numInput(t.end,x=>{t.end=x;upd()}),a_=a,b_=b;
    const setA=el('button',null,'◀ start = player');setA.onclick=()=>{t.start=+v.currentTime.toFixed(1);a.value=t.start;upd()};
    const setB=el('button',null,'end = player ▶');setB.onclick=()=>{t.end=+v.currentTime.toFixed(1);b.value=t.end;upd()};
    const shift=d=>{const L=c.duration_s,n=t.end-t.start;let a=Math.min(Math.max(t.start+d,0),Math.max(L-n,0));t.start=+a.toFixed(1);t.end=+(a+n).toFixed(1);a_.value=t.start;b_.value=t.end;v.currentTime=t.start};
    const left=el('button',null,'◀ 5 s');left.title='Slide the whole window 5 s earlier';left.onclick=()=>shift(-5);
    const right=el('button',null,'5 s ▶');right.title='Slide the whole window 5 s later';right.onclick=()=>shift(5);
    const snap=el('button',null,'30 s');snap.title='Make the clip exactly 30 s from the start point';snap.onclick=()=>{t.end=+Math.min(t.start+(c.clip_len_s||30),c.duration_s).toFixed(1);b.value=t.end;upd()};
    const r1=el('div','row');r1.append(el('span','mut','Trim'),left,right,a,setA,b,setB,snap,len);upd();
    const r2=el('div','row');
    for(const [k,label] of [['vertical','Vertical 9:16'],['captions','Captions']]){const l=el('label'),cb=el('input');cb.type='checkbox';cb.checked=t[k];cb.onchange=()=>{t[k]=cb.checked};l.append(cb,label);r2.append(l)}
    const ex=el('button','primary','Export clip');ex.onclick=async()=>{ex.disabled=true;ex.textContent='Encoding…';const ok=await post('/api/export',{id:c.id,start:t.start,end:t.end,vertical:t.vertical,captions:t.captions});ex.disabled=false;ex.textContent='Export clip';if(ok)load(true)};
    r2.append(ex);tr.append(r1,r2);card.append(tr);
    if((c.exports||[]).length){const e=el('div','row');e.append(el('span','mut','Exports:'));for(const x of c.exports){const l=el('a',null,(x.vertical?'9:16 ':'')+(x.captions?'CC ':'')+fmt(x.start)+'-'+fmt(x.end));l.href=fileUrl(c,x.file);l.target='_blank';
      const sf=el('button',null,'📂');sf.title='Show this file in its folder'+(x.copy?' (a copy is also in your final clips folder)':'');sf.onclick=()=>post('/api/reveal',{id:c.id,file:x.file});e.append(l,sf)}card.append(e)}
    let getText=()=>({title:'',body:''});
    {const d=c.description||{title:'',body:'',alts:[]},det=el('details');det.open=!!openPost.get(c.id);det.ontoggle=()=>openPost.set(c.id,det.open);getText=()=>({title:ti.value,body:ta.value});
      det.append(el('summary',null,'Post text: title + description ('+(d.category||'')+')'));
      var ti=el('input');ti.type='text';ti.value=d.title||'';var ta=el('textarea');ta.rows=4;ta.value=d.body||'';
      let timer;const save=()=>{clearTimeout(timer);timer=setTimeout(()=>post('/api/describe',{id:c.id,title:ti.value,body:ta.value}),600)};
      ti.oninput=save;ta.oninput=save;let alts=(d.alts||[]).slice();let ai=Math.max(alts.indexOf(d.title),0);const hint=el('div','hint');
      const fetchNew=async listen=>{hint.textContent=listen?'Listening to the clip (first time can take a minute)...':'Thinking up new titles...';
        const tt=trims.get(c.id)||{},r=await fetch('/api/describe/new',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.id,listen:!!listen,start:tt.start,end:tt.end})});
        if(!r.ok){hint.textContent=await r.text();return false}
        const nd=await r.json();alts=nd.alts||[];ai=0;if(alts.length)ti.value=alts[0];
        if(nd.body&&ta.value===(d.auto_body||d.body)){ta.value=nd.body}                 // only replace the description if you have not edited it
        d.auto_body=nd.auto_body;d.body=ta.value;hint.textContent=nd.note||(nd.used_speech?'Titles use what the streamer said.':'Titles use the chat reaction.');save();return true};
      const other=el('button',null,'↻ Other title');other.onclick=async()=>{other.disabled=true;
        if(alts.length>1&&ai+1<alts.length){ai++;ti.value=alts[ai];save()}else{await fetchNew(false)}other.disabled=false};
      const more=el('button',null,'🔄 New ideas');more.title='Make a completely new set of titles for this clip';more.onclick=async()=>{more.disabled=true;await fetchNew(false);more.disabled=false};
      const say=el('button',null,'🎧 From what is said');say.title='Listen to this clip (needs captions set up) and quote the words the streamer said in the titles';say.onclick=async()=>{say.disabled=true;await fetchNew(true);say.disabled=false};
      const cp=el('button',null,'Copy title + description');cp.onclick=async()=>{const t=ti.value+NL+NL+ta.value;
        try{await navigator.clipboard.writeText(t)}catch(e){ta.select();document.execCommand('copy')}cp.textContent='Copied ✓';setTimeout(()=>cp.textContent='Copy title + description',1500)};
      const dr=el('div','row');dr.append(other,more,say,cp);det.append(ti,ta,dr,hint);card.append(det)}
    if((c.exports||[]).length){
      const sd=el('details');sd.open=!!openShare.get(c.id);sd.ontoggle=()=>openShare.set(c.id,sd.open);sd.append(el('summary',null,'Share to TikTok / YouTube Shorts / Instagram Reels'));
      const fsel=el('select');const exs=c.exports.slice().reverse();for(const x of exs){const o=el('option',null,x.file+(x.vertical?' (9:16)':' (not vertical)'));o.value=x.file;fsel.append(o)}
      const vi=exs.findIndex(x=>x.vertical);if(vi>0)fsel.selectedIndex=vi;
      const psel=el('select');for(const p of shareInfo.providers){const o=el('option',null,p.label+(p.configured?'':' (not set up)'));o.value=p.name;o.disabled=!p.configured;psel.append(o)}
      if((shareInfo.providers.find(p=>p.name==='direct')||{}).configured)psel.value='direct';
      const boxes={},row=el('div','row');
      for(const [k,v] of Object.entries(shareInfo.platforms)){const l=el('label'),cb=el('input');cb.type='checkbox';cb.checked=true;boxes[k]=cb;l.append(cb,v.label);row.append(l)}
      const fl=el('label'),force=el('input');force.type='checkbox';fl.append(force,'post again if already posted');
      const st=el('div','mut');const stat=()=>{st.replaceChildren();for(const [k,v] of Object.entries(c.shares||{})){const line=el('div',null,(shareInfo.platforms[k]||{label:k}).label+': '+v.status+' '+(v.at||'').slice(11,16)+(v.message?' - '+v.message:''));
        if(v.url){const a2=el('a',null,'open');a2.href=v.url;a2.target='_blank';line.append(' ',a2)}
        if(v.status==='opened'||v.status==='draft'){const m=el('button',null,'Mark as posted');m.onclick=async()=>{await post('/api/share/mark',{id:c.id,platform:k,status:'done'});load(true)};line.append(' ',m)}
        if(v.status==='done'){const m=el('button',null,'Reset');m.onclick=async()=>{await post('/api/share/mark',{id:c.id,platform:k,status:'reset'});load(true)};line.append(' ',m)}
        st.append(line)}};stat();
      const go=el('button','primary','Share selected…');
      go.onclick=async()=>{
        const chosen=Object.keys(boxes).filter(k=>boxes[k].checked);if(!chosen.length){alert('Tick at least one platform.');return}
        const tx=getText(),texts={};for(const k of chosen)texts[k]={title:tx.title,body:tx.body};
        const prov=shareInfo.providers.find(p=>p.name===psel.value)||{label:psel.value,automatic:false};
        const names=chosen.map(k=>shareInfo.platforms[k].label).join(', ');
        if(!confirm('Share this clip?'+NL+NL+'File: '+fsel.value+NL+'Title: '+tx.title+NL+'To: '+names+NL+'Method: '+prov.label+NL+NL+(prov.automatic?'It is uploaded to your connected account(s) the moment you press OK.':'Nothing is posted or opened until you press OK.')))return;
        go.disabled=true;go.textContent='Working…';
        const send=async plats=>{const r=await fetch('/api/share',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.id,file:fsel.value,platforms:plats,texts,provider:psel.value,force:force.checked})});
          if(!r.ok){alert(await r.text());return null}return (await r.json()).results};
        if(prov.automatic){const res=await send(chosen);if(res){try{await navigator.clipboard.writeText(Object.values(res)[0].text)}catch(e){}alert(Object.entries(res).map(([k,v])=>shareInfo.platforms[k].label+': '+v.status+(v.message?' - '+v.message:'')+(v.url?NL+v.url:'')).join(NL+NL))}}
        else{for(const k of chosen){const res=await send([k]);if(!res)break;const r1=res[k];
            try{await navigator.clipboard.writeText(r1.text)}catch(e){}
            if(confirm(shareInfo.platforms[k].label+': page opened, file selected, caption copied to your clipboard.'+NL+NL+'Paste the caption, attach the video and press Post there.'+NL+'Press OK once it is posted (marks it done), or Cancel to leave it as "opened".'))await post('/api/share/mark',{id:c.id,platform:k,status:'done'})}}
        go.disabled=false;go.textContent='Share selected…';load(true)};
      sd.append(el('div','mut','Clip file:'),fsel,el('div','mut','Method:'),psel,row,fl,go,st);card.append(sd)}
    const lb=el('button',null,layouts[c.channel]?'Edit layout for '+c.channel+' ✓':'Set layout for '+c.channel+' (webcam + gameplay)');lb.onclick=()=>openLayout(c,v);card.append(lb);
    const vx=(c.exports||[]).filter(x=>x.vertical).slice(-1)[0];
    if(vx){const pvid=el('video','vert');pvid.controls=true;pvid.preload='metadata';pvid.src=fileUrl(c,vx.file);card.append(el('div','mut','Vertical preview (latest export):'),pvid)}
    const bt=el('div','row');
    for(const [label,st] of [['Keep','keep'],['Trash','trash'],['Undo','new']]){if(st===c.status||(st==='new'&&c.status==='new'))continue;
      const x=el('button',null,label);x.onclick=async()=>{await post('/api/status',{id:c.id,status:st});load(true)};bt.append(x)}
    const src=el('a',null,'Channel');src.href=c.source_url;src.target='_blank';bt.append(src);
    card.append(bt);g.append(card)}}
async function post(u,d){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok){alert(await r.text());return false}return true}

let layouts={};
async function loadShare(){try{shareInfo=await (await fetch('/api/share/providers')).json()}catch(e){}}
function openFolders(){
  fetch('/api/settings').then(r=>r.json()).then(S=>{
    const ov=el('div','modal'),box=el('div','mbox');ov.append(box);box.append(el('h2',null,'Folders'));
    const msg=el('div','hint');
    const mk=(title,hint,val,what,key)=>{const w=el('div'),r=el('div','row'),inp=el('input');inp.type='text';inp.value=val||'';inp.style.cssText='flex:1;min-width:260px;padding:6px;border-radius:6px;border:1px solid #8884;background:var(--bg);color:var(--fg)';
      const br=el('button',null,'Browse…');br.onclick=async()=>{br.disabled=true;const r2=await fetch('/api/browse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initial:inp.value})});br.disabled=false;const j=await r2.json();if(j.path)inp.value=j.path;else msg.textContent='No folder chosen (cancelled, or this PC has no folder dialog: paste the full path instead).'};
      const op=el('button',null,'Open');op.onclick=()=>post('/api/reveal',{what});r.append(inp,br,op);w.append(el('b',null,title),el('div','hint',hint),r);return [w,inp]};
    const [w1,i1]=mk('Final clips folder','Every finished (exported) clip is ALSO copied here, e.g. D:'+String.fromCharCode(92)+'Clips. Leave empty to keep them only with the drafts.',S.export_dir,'export','export_dir');
    const [w2,i2]=mk('Drafts + working folder','Drafts, layouts and the review page live here. Changing it takes effect the next time you start the program.',S.out_dir,'out','out_dir');
    const sv=el('button','primary','Save'),cl=el('button',null,'Close');
    sv.onclick=async()=>{const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({export_dir:i1.value,out_dir:i2.value})});
      msg.textContent=r.ok?(await r.json()).restart_needed?'Saved. Restart the clipper and this page to use the new drafts folder.':'Saved. New exports use it right away.':await r.text()};
    cl.onclick=()=>ov.remove();const bar=el('div','row');bar.append(sv,cl);box.append(w1,w2,bar,msg,el('div','hint','Settings file: '+S.settings_file));document.body.append(ov)})}
const STEPS={
youtube:['Open console.cloud.google.com, create a project, then APIs and Services > Library > enable YouTube Data API v3.','OAuth consent screen: type External, add your own Google account as a test user, then press Publish app (so the login does not expire after 7 days).','Credentials > Create credentials > OAuth client ID > type Desktop app. Copy the client ID and secret below.','Press Save and connect, pick your YouTube channel in the new tab and allow the upload permission. Google shows a not verified warning for your own app: choose Advanced > continue.'],
instagram:['In the Instagram app: Settings > Account type > switch to a Professional account (Creator is fine).','Open developers.facebook.com, create an app (type Business), add the product Instagram with Instagram login, add your account as an Instagram tester and accept the invite in the Instagram app.','In the app dashboard under Instagram, press Generate token for your account and paste it below.'],
tiktok:['Open developers.tiktok.com, create an app and add the products Login Kit and Content Posting API.','Add this redirect address in the app settings: REDIRECT. Enable the scopes video.upload and video.publish, and add your TikTok account as a test user.','Paste the client key and secret below and press Save and connect. TikTok only allows private direct posts for apps it has not audited, so Draft mode (the video waits in your TikTok inbox, you press Post) is the default.']};
async function openAccounts(){
  const S=await (await fetch('/api/accounts')).json();
  const ov=el('div','modal'),box=el('div','mbox');ov.append(box);box.append(el('h2',null,'Connect your accounts'));
  box.append(el('div','hint','Optional: only needed for automatic posting. The Share assistant works without any of this. Your logins are stored on this PC only (accounts.json in the autoclip settings folder) and are never shown again or sent anywhere except to the platform itself. Everything uses the free official APIs with developer apps you create on your own accounts.'));
  const msg=el('div','hint');
  const build=()=>{for(const old of box.querySelectorAll('.acc'))old.remove();
    for(const [pl,info] of Object.entries(S.platforms)){
      const sec=el('div','acc'),label=(shareInfo.platforms[pl]||{label:pl}).label;
      sec.append(el('b',null,label+(info.connected?'  ✅ connected '+(info.account||''):'  not connected')));
      const ins={};for(const f of info.fields){const i=el('input');i.type=f.secret?'password':'text';i.placeholder=f.label+(info.have.includes(f.key)?' (saved: leave empty to keep)':'');i.autocomplete='off';ins[f.key]=i;sec.append(i)}
      const opts={};
      if(pl==='youtube'){const s=el('select');for(const v of ['public','unlisted','private']){const o=el('option',null,'Upload as: '+v);o.value=v;s.append(o)}s.value=info.options.privacy||'public';opts.privacy=s;sec.append(s)}
      if(pl==='tiktok'){const m=el('select');for(const [v,t] of [['draft','Draft: video waits in my TikTok inbox, I press Post'],['direct','Direct post (private unless TikTok audited the app)']]){const o=el('option',null,t);o.value=v;m.append(o)}m.value=info.options.mode||'draft';opts.mode=m;sec.append(m)}
      const d=el('details');d.append(el('summary',null,'How to get these (about 5 minutes)'));const ol=el('ol');for(const s of STEPS[pl])ol.append(el('li',null,s.replace('REDIRECT',S.redirect_uri)));d.append(ol);sec.append(d);
      const row=el('div','row'),go=el('button','primary','Save and connect');
      go.onclick=async()=>{go.disabled=true;msg.textContent='Working...';const values={},options={};for(const k in ins)values[k]=ins[k].value;for(const k in opts)options[k]=opts[k].value;
        const r=await fetch('/api/accounts/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:pl,values,options})});go.disabled=false;
        if(!r.ok){msg.textContent=await r.text();return}
        const j=await r.json();
        if(j.auth_url){window.open(j.auth_url,'_blank');msg.textContent='Finish signing in in the tab that just opened. This window updates by itself.';
          for(let n=0;n<90;n++){await new Promise(z=>setTimeout(z,2000));S.platforms=(await (await fetch('/api/accounts')).json()).platforms;if(S.platforms[pl].connected){msg.textContent=label+' connected.';break}}}
        else{msg.textContent=j.connected?label+' connected as '+j.account:'Saved.';S.platforms=(await (await fetch('/api/accounts')).json()).platforms}
        await loadShare();build()};
      row.append(go);
      if(info.connected||info.have.length){const dc=el('button',null,'Disconnect');dc.onclick=async()=>{if(!confirm('Remove the saved login for '+label+' from this PC?'))return;await post('/api/accounts/disconnect',{platform:pl});S.platforms=(await (await fetch('/api/accounts')).json()).platforms;await loadShare();build()};row.append(dc)}
      sec.append(row);box.insertBefore(sec,msg)}};
  const cl=el('button',null,'Close');cl.onclick=()=>{ov.remove();sig='';load(true)};box.append(msg,cl);build();document.body.append(ov)}
async function loadLayouts(){try{layouts=await (await fetch('/api/layouts')).json()}catch(e){}}
const OUTW=1080,OUTH=1920,even=n=>Math.max(2,Math.round(n/2)*2);
function openLayout(c,video){
  const L=JSON.parse(JSON.stringify(layouts[c.channel]||{mode:'split',split:0.5,game_fit:'blur',facecam:null,game:null}));
  if(!L.mode)L.mode='split';if(!L.split)L.split=0.5;if(!L.game_fit)L.game_fit='blur';
  const ov=el('div','modal'),box=el('div','mbox');ov.append(box);
  box.append(el('h2',null,'Vertical layout for '+c.channel));
  box.append(el('div','hint','Drag the RED box over the webcam and the BLUE box over the part of the game you want to show. Drag a box to move it, drag its bottom-right square to resize. Drag the GREEN box on the vertical preview (right) to place the subtitles; its height sets the text size. Set it once per streamer; every clip uses it.'));
  const ctl=el('div','row'),mode=el('select');
  for(const [v,t] of [['split','Webcam on top + gameplay below'],['blur','Whole screen on blurred background'],['crop','Plain center crop']]){const o=el('option',null,t);o.value=v;mode.append(o)}
  mode.value=L.mode;
  const fit=el('select');for(const [v,t] of [['cover','Gameplay: crop to blue box'],['blur','Gameplay: whole screen, blurred bars']]){const o=el('option',null,t);o.value=v;fit.append(o)}fit.value=L.game_fit;
  const sp=el('input');sp.type='range';sp.min='40';sp.max='60';sp.value=Math.round(L.split*100);
  const spl=el('span','mut','Webcam height '+sp.value+'%');
  const tin=numInput((+video.currentTime||0).toFixed(1),()=>{});const tb=el('button',null,'Load frame at (s)');
  const resetCap=el('button',null,'Reset subtitle box'),auto=el('button',null,'Auto-detect webcam'),pv=el('button',null,'Preview'),save=el('button','primary','Save layout'),close=el('button',null,'Close');
  ctl.append(mode,fit,sp,spl,el('span','mut','Frame:'),tin,tb,auto,resetCap,pv,save,close);box.append(ctl);
  const cols=el('div','mcols'),cv=el('canvas'),img=new Image(),pimg=el('img','pvimg'),pw=el('div','pw'),cap=el('div','capbox'),capH=el('div','caph');
  cap.append(el('span'),capH);pw.append(pimg,cap);cols.append(cv,pw);box.append(cols);document.body.append(ov);
  let C=L.captions?{...L.captions}:null,cd=null;
  const capDefault=()=>L.mode==='split'?{x:0.06,y:0.46,w:0.88,h:0.08}:{x:0.06,y:0.70,w:0.88,h:0.08};
  const cc=()=>C||capDefault();
  function renderCap(){const r=cc(),sp=cap.firstChild;cap.style.left=r.x*100+'%';cap.style.top=r.y*100+'%';cap.style.width=r.w*100+'%';cap.style.height=r.h*100+'%';
    cap.style.fontSize=(pw.clientHeight/1920*Math.min(Math.max(r.h*1920*0.6,24),150))+'px';   // same size rule as the exported subtitles
    sp.replaceChildren(document.createTextNode('THIS IS '));const hi=el('span',null,'INSANE');hi.style.color='#ffe600';sp.append(hi)}
  cap.onpointerdown=e=>{const r=cc();C={...r};cd={t:e.target===capH?'size':'move',x:e.clientX,y:e.clientY,r:{...r}};cap.setPointerCapture(e.pointerId);e.preventDefault()};
  cap.onpointermove=e=>{if(!cd)return;const b=pw.getBoundingClientRect(),dx=(e.clientX-cd.x)/b.width,dy=(e.clientY-cd.y)/b.height,r=cd.r;
    if(cd.t==='move'){C.x=Math.min(Math.max(r.x+dx,0),1-r.w);C.y=Math.min(Math.max(r.y+dy,0),1-r.h)}
    else{C.w=Math.min(Math.max(r.w+dx,0.2),1-r.x);C.h=Math.min(Math.max(r.h+dy,0.03),Math.min(0.5,1-r.y))}
    renderCap()};
  cap.onpointerup=()=>{cd=null};
  const msg=el('div','hint');box.append(msg);
  const zoom=el('div','hint');box.append(zoom);
  function updZoom(){zoom.textContent='';if(!c.res||L.mode!=='split'||!L.facecam)return;
    const f=OUTW/(L.facecam.w*c.res[0]),g=L.game_fit==='cover'&&L.game?OUTW/(L.game.w*c.res[0]):OUTW/c.res[0];
    const lab=x=>x.toFixed(1)+'x '+(x<=1.3?'(sharp)':x<=2?'(ok)':'(soft - make the box bigger, or the stream was recorded in low quality)');
    zoom.textContent='Source '+c.res[0]+'x'+c.res[1]+' · webcam is enlarged '+lab(f)+' · gameplay is enlarged '+lab(g);zoom.style.color=Math.max(f,g)>2?'#dc2626':''}
  let W=0,H=0,drag=null;
  const hh=()=>{const t=even(OUTH*L.split);return [t,OUTH-t]};
  const aspect=k=>{const [a,b]=hh();return OUTW/(k==='facecam'?a:b)};
  function fixRect(k){const r=L[k];if(!r)return;const A=aspect(k);let w=Math.min(r.w,1),h=(w*W/A)/H;if(h>1){h=1;w=(h*H*A)/W}
    r.w=w;r.h=h;r.x=Math.min(Math.max(r.x,0),1-w);r.y=Math.min(Math.max(r.y,0),1-h)}
  function defaults(){
    if(!L.facecam){const w=0.26,A=aspect('facecam');L.facecam={w,h:(w*W/A)/H,x:0.72,y:0.66}}
    if(!L.game){const A=aspect('game'),h=1,w=Math.min(1,(h*H*A)/W);L.game={w,h,x:(1-w)/2,y:0}}
    fixRect('facecam');fixRect('game')}
  const px=r=>({x:r.x*W,y:r.y*H,w:r.w*W,h:r.h*H});
  function draw(){updZoom();const g=cv.getContext('2d');g.clearRect(0,0,W,H);if(img.complete)g.drawImage(img,0,0,W,H);
    if(L.mode!=='split')return;
    const boxes=[['facecam','#ef4444','WEBCAM (top)']];if(L.game_fit==='cover')boxes.push(['game','#3b82f6','GAMEPLAY (bottom)']);
    for(const [k,col,label] of boxes){const r=px(L[k]);g.lineWidth=3;g.strokeStyle=col;g.strokeRect(r.x,r.y,r.w,r.h);
      g.fillStyle=col+'33';g.fillRect(r.x,r.y,r.w,r.h);g.fillStyle=col;g.fillRect(r.x+r.w-12,r.y+r.h-12,12,12);
      g.font='bold 14px sans-serif';g.fillText(label,r.x+6,r.y+18)}}
  function sync(){const split=mode.value==='split';fit.style.display=sp.style.display=spl.style.display=auto.style.display=split?'':'none'}
  function layoutOut(){const o=mode.value!=='split'?{mode:mode.value}:{mode:'split',split:L.split,game_fit:L.game_fit,facecam:L.facecam,game:L.game_fit==='cover'?L.game:null};
    if(C)o.captions={x:C.x,y:C.y,w:C.w,h:C.h};return o}
  function loadFrame(){msg.textContent='Loading frame…';img.src='/api/frame?id='+encodeURIComponent(c.id)+'&t='+(parseFloat(tin.value)||0)+'&n='+Date.now()}
  img.onload=()=>{W=Math.min(720,img.naturalWidth);H=Math.round(W*img.naturalHeight/img.naturalWidth);cv.width=W;cv.height=H;defaults();msg.textContent='';draw()};
  img.onerror=()=>{msg.textContent='Could not load a frame from this clip.'};
  const pos=e=>{const b=cv.getBoundingClientRect();return {x:(e.clientX-b.left)*W/b.width,y:(e.clientY-b.top)*H/b.height}};
  cv.onpointerdown=e=>{if(L.mode!=='split'||!W)return;const p=pos(e);
    const ks=L.game_fit==='cover'?['game','facecam']:['facecam'];
    for(const k of ks.reverse()){const r=px(L[k]);
      if(p.x>=r.x+r.w-18&&p.x<=r.x+r.w+6&&p.y>=r.y+r.h-18&&p.y<=r.y+r.h+6){drag={k,type:'size'};break}
      if(p.x>=r.x&&p.x<=r.x+r.w&&p.y>=r.y&&p.y<=r.y+r.h){drag={k,type:'move',dx:p.x-r.x,dy:p.y-r.y};break}}
    if(drag)cv.setPointerCapture(e.pointerId)};
  cv.onpointermove=e=>{if(!drag)return;const p=pos(e),r=L[drag.k],A=aspect(drag.k);
    if(drag.type==='move'){r.x=Math.min(Math.max((p.x-drag.dx)/W,0),1-r.w);r.y=Math.min(Math.max((p.y-drag.dy)/H,0),1-r.h)}
    else{let w=Math.max(p.x-r.x*W,W*0.06),h=w/A;if(r.y*H+h>H){h=H-r.y*H;w=h*A}if(r.x*W+w>W){w=W-r.x*W;h=w/A}r.w=w/W;r.h=h/H}
    draw()};
  cv.onpointerup=()=>{drag=null};
  mode.onchange=()=>{L.mode=mode.value;sync();draw();renderCap()};fit.onchange=()=>{L.game_fit=fit.value;draw()};
  sp.oninput=()=>{L.split=sp.value/100;spl.textContent='Webcam height '+sp.value+'%';fixRect('facecam');fixRect('game');draw()};
  tb.onclick=loadFrame;close.onclick=()=>ov.remove();
  auto.onclick=async()=>{auto.disabled=true;msg.textContent='Looking for a face…';
    const r=await fetch('/api/autodetect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.id,split:L.split})});
    auto.disabled=false;if(!r.ok){msg.textContent=await r.text();return}
    L.facecam=await r.json();fixRect('facecam');draw();msg.textContent='Webcam found. Adjust the red box if needed, then press Preview.'};
  pv.onclick=async()=>{pv.disabled=true;msg.textContent='Rendering preview…';
    const r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.id,t:parseFloat(tin.value)||0,layout:layoutOut()})});
    pv.disabled=false;if(!r.ok){msg.textContent=await r.text();return}
    pimg.src=URL.createObjectURL(await r.blob());pimg.style.visibility='visible';msg.textContent=''};
  save.onclick=async()=>{const r=await fetch('/api/layout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel:c.channel,layout:layoutOut()})});
    if(!r.ok){msg.textContent=await r.text();return}
    layouts[c.channel]=await r.json();msg.textContent='Saved. New clips use it automatically; press "Export clip" on an existing clip to re-render it.';load(true)};
  resetCap.onclick=()=>{C=null;renderCap()};
  sync();renderCap();loadFrame()}
document.getElementById('f').onchange=()=>render();document.getElementById('s').onchange=()=>render();
document.getElementById('purge').onclick=async()=>{if(confirm('Permanently delete every trashed clip file?')){await post('/api/purge',{});load(true)}};
document.getElementById('folders').onclick=openFolders;document.getElementById('accts').onclick=openAccounts;Promise.all([loadLayouts(),loadShare()]).then(()=>load(true));setInterval(()=>load(false),10000);
</script>"""


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Store:
    def __init__(self, root: Path, cfg: Config):
        self.root, self.cfg, self.lock = root.resolve(), cfg, threading.Lock()
        self.port = 8765                              # set by serve(); the sign-in pages come back to this port

    def _metas(self):
        for p in self.root.rglob("*.clip.json"):
            try:
                yield p, json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

    _SIZES: dict = {}

    def size_of(self, f: Path) -> list[int] | None:
        """[width, height] of a clip file; ffprobe runs once per file (cached by path + modification time)."""
        try:
            key = (str(f), f.stat().st_mtime_ns)
        except OSError:
            return None
        if key not in self._SIZES:
            sz = probe_size(f)
            self._SIZES[key] = list(sz) if sz else None
        return self._SIZES[key]

    def list(self) -> list[dict]:
        out = []
        for p, m in self._metas():
            m["dir"] = p.parent.relative_to(self.root).as_posix()
            m["res"] = self.size_of(p.parent / m["file"]) if m.get("file") else None
            if not m.get("description"):
                m["description"] = self._fresh_description(p, m)
            out.append(m)
        return out

    def _fresh_description(self, p: Path, m: dict) -> dict:
        words = cached_words(p.parent / m["file"]) if m.get("file") else []
        d = suggest_description(m, self.cfg, words or None)
        d["auto_body"] = d["body"]
        return d

    def new_titles(self, cid: str, listen: bool = False, start: float | None = None, end: float | None = None) -> dict:
        """A NEW set of clip-specific titles, never repeating ones already shown. With listen=True the clip is transcribed first
        (Whisper, only the part you are exporting) so titles can quote what the streamer actually said."""
        p, m = self.find(cid)
        if not p:
            raise ValueError("clip not found")
        src = p.parent / m["file"]
        lo = float(start if start is not None else m.get("suggest_start_s", 0))
        hi = float(end if end is not None else m.get("suggest_end_s", m.get("duration_s", 0)))
        words = None
        note = ""
        if listen:
            try:
                words = transcribe(src, self.cfg, lo, hi)
            except CaptionsUnavailable as e:
                note = f"Could not listen to the clip ({e}). Titles are based on chat only."
        if words is None:
            words = cached_words(src) or None
        with self.lock:
            p, m = self.find(cid)
            old = m.get("description") or {}
            fresh = suggest_description(m, self.cfg, words, (lo, hi), seed=int(old.get("seed", -1)) + 1, seen=old.get("seen"))
            body_edited = bool(old.get("body")) and old.get("body") != old.get("auto_body")      # no record of an automatic body = treat as yours
            d = {**old, **fresh, "auto_body": fresh["body"]}
            if body_edited:
                d["body"] = old["body"]                     # never overwrite text you wrote yourself
            m["description"] = d
            _write_json(p, m)
        return {**d, "note": note}

    def find(self, cid: str):
        for p, m in self._metas():
            if m.get("id") == cid:
                return p, m
        return None, None

    def set_status(self, cid: str, status: str) -> bool:
        if status not in ("new", "keep", "trash"):
            return False
        with self.lock:
            p, m = self.find(cid)
            if not p:
                return False
            m["status"] = status
            _write_json(p, m)
            return True

    def set_description(self, cid: str, title: str, body: str) -> bool:
        if len(title) > 300 or len(body) > 5000:
            return False
        with self.lock:
            p, m = self.find(cid)
            if not p:
                return False
            d = m.get("description") or self._fresh_description(p, m)
            d["title"], d["body"] = title, body
            m["description"] = d
            _write_json(p, m)
            return True

    def copy_to_export_dir(self, f: Path, channel: str) -> str | None:
        """Also place the finished clip in the folder you chose for final clips (if any)."""
        if not self.cfg.export_dir:
            return None
        try:
            import shutil
            dest = settings.validate_dir(self.cfg.export_dir) / re.sub(r"[^\w.-]", "_", channel)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest / f.name)
            return str(dest / f.name)
        except (OSError, ValueError) as e:
            print(f"could not copy to the final clips folder: {e}")
            return None

    def draft_path(self, cid: str) -> tuple[Path, dict]:
        p, m = self.find(cid)
        if not p:
            raise ValueError("clip not found")
        return p.parent / m["file"], m

    def frame(self, cid: str, t: float) -> bytes:
        src, _ = self.draft_path(cid)
        img = grab_frame(src, t)
        if not img:
            raise RuntimeError("could not read a frame from the clip")
        return img

    def preview(self, cid: str, t: float, layout: dict) -> bytes:
        src, _ = self.draft_path(cid)
        graph = lay.chain(lay.validate(layout))
        img = grab_frame(src, t, graph)
        if not img:
            raise RuntimeError("could not render the preview")
        return img

    def autodetect(self, cid: str, split: float) -> dict:
        src, m = self.draft_path(cid)
        rect = detect_facecam(src, float(m["duration_s"]), split)
        if not rect:
            raise ValueError("no face found consistently in this clip. Draw the webcam box by hand.")
        return rect

    def export(self, cid: str, start: float, end: float, vertical: bool, captions: bool) -> str:
        """Precise trim (+ optional 9:16, + optional burned captions) in ONE encode. Returns the new file name."""
        p, m = self.find(cid)
        if not p:
            raise ValueError("clip not found")
        src = p.parent / m["file"]
        if not (0 <= start < end <= m["duration_s"] + 1) or end - start > 600:
            raise ValueError(f"bad trim {start}-{end} (draft is {m['duration_s']}s)")
        layout = LayoutStore(self.root).get(m["channel"]) if vertical else None
        ass = None
        if captions:
            words = transcribe(src, self.cfg, start, end)          # only the part being exported; may raise CaptionsUnavailable
            inside = words_in_window(words, start, end)
            if not inside:
                raise ValueError(f"no speech was recognised between {start:.1f}s and {end:.1f}s, even after a second, more sensitive pass. "
                                 "The streamer may be silent there, or game sound / music may be drowning the voice. "
                                 "Move the trim, or export without captions.")
            ass = build_ass(words, start, end, vertical, self.cfg.caption_font, seam=(layout or {}).get("mode") == "split",
                            box=(layout or {}).get("captions"))
        name = f"{src.stem}_{int(start * 10)}-{int(end * 10)}{'_v' if vertical else ''}{'_cc' if captions else ''}.mp4"
        errs: list[str] = []
        placed = export_clip(src, p.parent / name, start, end, self.cfg, vertical, ass, layout, errs)
        if not placed:
            why = errs[0] if errs else "see the console running `review`"
            raise RuntimeError(("" if why.startswith("could not save") else "ffmpeg failed: ") + why)
        name = placed.name                                   # may differ from the plan if Windows had the old file locked
        copy = self.copy_to_export_dir(placed, m["channel"])
        with self.lock:
            p, m = self.find(cid)
            m.setdefault("exports", [])
            m["exports"] = [e for e in m["exports"] if e["file"] != name] + [
                {"file": name, "start": start, "end": end, "vertical": vertical, "captions": captions, "copy": copy,
                 "layout": (layout or {}).get("mode", self.cfg.vertical_mode) if vertical else None}]
            _write_json(p, m)
        return name

    def save_account(self, platform: str, values: dict, options: dict) -> dict:
        """Store what you typed in the Accounts window. Instagram is checked right away; YouTube / TikTok return a sign-in URL."""
        if platform not in accounts.FIELDS:
            raise ValueError("unknown platform")
        allowed = {k for k, _, _ in accounts.FIELDS[platform]}
        clean = {k: str(v).strip() for k, v in values.items() if k in allowed and str(v).strip()}
        opts = {}
        if platform == "youtube" and options.get("privacy") in ("public", "unlisted", "private"):
            opts["privacy"] = options["privacy"]
        if platform == "tiktok":
            if options.get("mode") in ("draft", "direct"):
                opts["mode"] = options["mode"]
            if options.get("privacy") in ("SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"):
                opts["privacy"] = options["privacy"]
        if platform == "instagram" and clean.get("access_token"):
            name = accounts.connect_instagram(clean["access_token"])
            return {"connected": True, "account": "@" + name}
        if clean or opts:
            accounts.update(platform, **clean, **opts)
        if platform in ("youtube", "tiktok") and (clean or not accounts.connected(platform)):
            return {"auth_url": accounts.begin_oauth(platform, self.port)}
        return {"saved": True}

    def purge(self) -> int:
        n = 0
        with self.lock:
            for p, m in list(self._metas()):
                if m.get("status") == "trash":
                    names = [m.get("file")] + [e["file"] for e in m.get("exports", [])]
                    for name in names + [m.get("file", "").replace(".mp4", ".words.json")]:
                        if name:
                            (p.parent / name).unlink(missing_ok=True)
                    p.unlink(missing_ok=True)
                    n += 1
        return n


class QuietServer(ThreadingHTTPServer):
    """Browsers abort connections constantly (WinError 10053 / broken pipe). That is normal: don't print a traceback."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_handler(store: Store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/clips":
                return self._send(200, json.dumps(store.list()).encode())
            if path == "/api/settings":
                return self._send(200, json.dumps({"out_dir": str(store.root), "export_dir": store.cfg.export_dir,
                                                   "settings_file": str(settings.path())}).encode())
            if path == "/api/share/providers":
                return self._send(200, json.dumps({"providers": share.providers_info(), "platforms": share.PLATFORMS}).encode())
            if path == "/api/accounts":
                return self._send(200, json.dumps({"platforms": accounts.status(), "redirect_uri": accounts.redirect_uri(store.port)}).encode())
            if path == "/oauth/callback":
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                try:
                    if q.get("error"):
                        raise accounts.ApiError(f"sign-in was cancelled or refused: {q.get('error_description') or q['error']}")
                    msg, ok = accounts.finish_oauth(q.get("state", ""), q.get("code", "")), True
                except accounts.ApiError as e:
                    msg, ok = str(e), False
                page = (f"<!doctype html><meta charset=utf-8><title>autoclip</title><body style='font:18px system-ui;padding:40px'>"
                        f"<h2>{'Done' if ok else 'Not connected'}</h2><p>{html.escape(msg)}</p>"
                        f"<p>{'You can close this tab and go back to the review page.' if ok else 'Close this tab, fix the problem and press Connect again.'}</p>")
                return self._send(200 if ok else 400, page.encode(), "text/html; charset=utf-8")
            if path == "/api/layouts":
                return self._send(200, json.dumps(LayoutStore(store.root).all()).encode())
            if path == "/api/frame":
                q = parse_qs(urlparse(self.path).query)
                try:
                    return self._send(200, store.frame(q["id"][0], float(q.get("t", ["0"])[0])), "image/jpeg", {"Cache-Control": "no-store"})
                except (ValueError, KeyError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
                except Exception as e:
                    return self._send(500, str(e).encode(), "text/plain")
            if path.startswith("/files/"):
                f = (store.root / path[7:]).resolve()
                if store.root not in f.parents or not f.is_file() or f.suffix.lower() != ".mp4":
                    return self._send(404, b"not found", "text/plain")
                return self._serve_file(f)
            self._send(404, b"not found", "text/plain")

        def _serve_file(self, f: Path):
            """Stream a video in chunks with Range support (seeking). Browsers cancel transfers all the time
            (seeking, reloading); that raises ConnectionError mid-send, which is normal and silently ignored."""
            size = f.stat().st_size
            start, end, partial = 0, size - 1, False
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if m and (m[1] or m[2]):
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                else:                                   # "bytes=-500" means the LAST 500 bytes
                    start = max(size - int(m[2]), 0)
                if start >= size or start > end:
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                partial = True
            try:
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "video/mp4")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                pos, left = start, end - start + 1
                while left > 0:                             # open per chunk: a slow browser must not keep the file locked (WinError 32)
                    with open(f, "rb") as fh:
                        fh.seek(pos)
                        chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    pos += len(chunk)
                    left -= len(chunk)
            except ConnectionError:
                pass

        def do_POST(self):
            if self.headers.get("Origin") and urlparse(self.headers["Origin"]).hostname not in ("127.0.0.1", "localhost"):
                return self._send(403, b"forbidden", "text/plain")     # block other websites from poking the API
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self._send(400, b"bad json", "text/plain")
            path = urlparse(self.path).path
            ok = False
            if path == "/api/status":
                ok = store.set_status(str(body.get("id")), str(body.get("status")))
            elif path == "/api/export":
                try:
                    name = store.export(str(body.get("id")), float(body["start"]), float(body["end"]),
                                        bool(body.get("vertical")), bool(body.get("captions")))
                    return self._send(200, json.dumps({"file": name}).encode())
                except (ValueError, KeyError, CaptionsUnavailable) as e:
                    return self._send(400, str(e).encode(), "text/plain")
                except Exception as e:
                    return self._send(500, f"export failed: {e}".encode(), "text/plain")
            elif path == "/api/layout":
                try:
                    if not re.fullmatch(r"[\w.-]{1,60}", str(body.get("channel", ""))):
                        raise LayoutError("bad channel name")
                    clean = LayoutStore(store.root).save(str(body["channel"]), body.get("layout"))
                    return self._send(200, json.dumps(clean).encode())
                except (LayoutError, KeyError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/preview":
                try:
                    return self._send(200, store.preview(str(body["id"]), float(body.get("t", 0)), body["layout"]), "image/jpeg",
                                      {"Cache-Control": "no-store"})
                except (ValueError, KeyError, LayoutError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
                except Exception as e:
                    return self._send(500, str(e).encode(), "text/plain")
            elif path == "/api/autodetect":
                try:
                    return self._send(200, json.dumps(store.autodetect(str(body["id"]), float(body.get("split", 0.5)))).encode())
                except (ValueError, KeyError, FacecamUnavailable) as e:
                    return self._send(400, str(e).encode(), "text/plain")
                except Exception as e:
                    return self._send(500, str(e).encode(), "text/plain")
            elif path == "/api/settings":
                try:
                    changes = {}
                    if body.get("export_dir") is not None:
                        ed = str(body["export_dir"]).strip()
                        changes["export_dir"] = str(settings.validate_dir(ed)) if ed else ""
                        store.cfg.export_dir = changes["export_dir"]                  # takes effect immediately
                    if body.get("out_dir"):
                        changes["out_dir"] = str(settings.validate_dir(str(body["out_dir"])))
                    settings.update(**changes)
                    restart = "out_dir" in changes and Path(changes["out_dir"]) != store.root
                    return self._send(200, json.dumps({**changes, "restart_needed": restart}).encode())
                except ValueError as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/browse":
                return self._send(200, json.dumps({"path": settings.pick_folder(str(body.get("initial", "")))}).encode())
            elif path == "/api/reveal":
                try:
                    target = store.root if body.get("what") == "out" else (Path(store.cfg.export_dir) if body.get("what") == "export" and store.cfg.export_dir else None)
                    if target is None:
                        f, _ = store.draft_path(str(body["id"]))
                        target = f.parent / Path(str(body["file"])).name
                        if target.suffix.lower() != ".mp4" or not target.is_file():
                            raise ValueError("file not found")
                    settings.reveal(target)
                    return self._send(200, b"{}")
                except (ValueError, KeyError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/share":
                try:
                    res = share.run(store, share.ShareRequest(str(body["id"]), str(body["file"]), list(body.get("platforms", [])),
                                                              dict(body.get("texts", {})), str(body.get("provider", "assistant")),
                                                              bool(body.get("force"))))
                    return self._send(200, json.dumps({"results": res}).encode())
                except (share.ShareError, KeyError, TypeError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/accounts/save":
                try:
                    return self._send(200, json.dumps(store.save_account(str(body.get("platform")), dict(body.get("values") or {}),
                                                                         dict(body.get("options") or {}))).encode())
                except (ValueError, KeyError, TypeError, accounts.ApiError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/accounts/disconnect":
                if str(body.get("platform")) in accounts.FIELDS:
                    accounts.clear(str(body["platform"]))
                    ok = True
            elif path == "/api/share/mark":
                ok = share.mark(store, str(body.get("id")), str(body.get("platform")), str(body.get("status")))
            elif path == "/api/describe/new":
                try:
                    return self._send(200, json.dumps(store.new_titles(
                        str(body["id"]), bool(body.get("listen")),
                        float(body["start"]) if body.get("start") is not None else None,
                        float(body["end"]) if body.get("end") is not None else None)).encode())
                except (ValueError, KeyError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
                except Exception as e:
                    return self._send(500, f"could not make titles: {e}".encode(), "text/plain")
            elif path == "/api/describe":
                ok = store.set_description(str(body.get("id")), str(body.get("title", "")), str(body.get("body", "")))
            elif path == "/api/purge":
                store.purge()
                ok = True
            self._send(200 if ok else 400, b"{}" if ok else b"failed", "application/json" if ok else "text/plain")

    return H


def serve(root: Path, cfg: Config, port: int = 8765, open_browser: bool = True):
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root, cfg)
    store.port = port
    srv = QuietServer(("127.0.0.1", port), make_handler(store))
    url = f"http://127.0.0.1:{port}/"
    print(f"Review page: {url}   (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
