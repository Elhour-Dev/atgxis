"""Local review page: ranked clips with players, keep / trash / make-vertical buttons. Binds to 127.0.0.1 only."""
from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import Config
from .captions import CaptionsUnavailable, build_ass, cached_words, transcribe, words_in_window
from . import layout as lay
import html
from . import accounts, autopilot, autopilot_page, campaigns, campaigns_page, hooks, publish, quality, reframe, reframe_page, settings, share, style, taste, zernio      # noqa: F401  (publish / zernio register the 'direct' / 'auto' sharing methods)
from .cutter import export_clip, grab_frame, probe_size, render_fps
from .recorder import ffmpeg_exe
from .describe import suggest as suggest_description
from .facecam import FacecamUnavailable, detect_facecam
from .lanes import PriorityPool
from .layout import LayoutError, LayoutStore

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Clip review</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f7;--fg:#18181b;--card:#fff;--soft:#f0f0f3;--line:#e3e3e8;--mut:#6b6b76;--acc:#6d28d9;--acc2:#ede9fe;
  --good:#15803d;--goodbg:#dcfce7;--bad:#b91c1c;--badbg:#fee2e2;--warn:#a16207;--warnbg:#fef3c7;--r:12px}
@media(prefers-color-scheme:dark){:root{--bg:#0f0f12;--fg:#ececf1;--card:#1a1a1f;--soft:#232329;--line:#2e2e36;--mut:#9a9aa6;--acc:#8b5cf6;--acc2:#2e2350;
  --good:#4ade80;--goodbg:#143323;--bad:#f87171;--badbg:#3a1717;--warn:#facc15;--warnbg:#3a3010}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
main{padding:16px;max-width:1600px;margin:0 auto}
header.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px}
header .bar,header .sub{display:flex;gap:10px;flex-wrap:wrap;align-items:center;max-width:1568px;margin:0 auto}
header .sub{margin-top:8px;font-size:13px}header .sub select{padding:4px 8px;font-size:13px}
h1{font-size:19px;margin:0 8px 0 0;letter-spacing:-.01em}
.tabs{display:flex;gap:4px;flex-wrap:wrap;background:var(--soft);padding:3px;border-radius:10px}
.tab{border:0;background:transparent;padding:6px 12px;border-radius:8px;font-weight:500}
.tab .n{display:inline-block;min-width:20px;padding:0 6px;border-radius:999px;background:var(--line);font-size:12px;text-align:center;margin-left:2px}
.tab.on{background:var(--card);box-shadow:0 1px 3px #0002}.tab.on .n{background:var(--acc);color:#fff}
.grow{flex:1}
select,button,input[type=number],input[type=text],textarea{font:inherit;color:var(--fg)}
select,button{padding:7px 12px;border-radius:9px;border:1px solid var(--line);background:var(--card);cursor:pointer}
button:hover{border-color:var(--mut)} button:disabled{opacity:.6;cursor:wait}
button:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:600}
button.good{background:var(--goodbg);color:var(--good);border-color:transparent;font-weight:600}
header.top{transition:padding .15s}header.top.compact{padding-top:6px;padding-bottom:6px;box-shadow:0 2px 10px #0002}header.top.compact .sub{display:none}
header.top.compact h1{font-size:16px}header.top.compact .tab{padding:4px 10px}header.top.compact .bar>button,header.top.compact .menu>button{padding:5px 10px}
button.danger{color:var(--bad);border-color:var(--bad);background:transparent}
button.sm{padding:4px 10px;font-size:13px;border-radius:8px}
input[type=number]{width:78px;padding:5px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card)}
.menu{position:relative}.menu-list{position:absolute;right:0;top:calc(100% + 6px);background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px;display:flex;flex-direction:column;gap:2px;min-width:230px;box-shadow:0 8px 24px #0003;z-index:6}
.menu-list[hidden]{display:none}.menu-list button{border:0;text-align:left;background:transparent;padding:8px 10px}.menu-list button:hover{background:var(--soft)}
.dot::after{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--warn);margin-left:6px;vertical-align:middle}
.keys{color:var(--mut)} kbd{font:11px ui-monospace,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;margin-left:6px;background:var(--soft);color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(380px,100%),1fr));gap:16px;align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px;display:flex;flex-direction:column;gap:10px;outline:none;transition:box-shadow .15s,border-color .15s}
.card:focus-within,.card:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc2)}
.card.keep{border-left:4px solid var(--good)}.card.trash{opacity:.6}.card.trash:focus-within,.card.trash:focus{opacity:1}
.head{display:flex;gap:10px;align-items:center}.htx{flex:1;min-width:0}
.htitle{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.score{flex:none;width:42px;height:42px;border-radius:10px;background:var(--acc2);color:var(--acc);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:17px}
.pill{flex:none;font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;background:var(--soft)}
.pill.keep{background:var(--goodbg);color:var(--good)}.pill.trash{background:var(--badbg);color:var(--bad)}
video{width:100%;border-radius:10px;background:#000;display:block}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.mut{color:var(--mut);font-size:13px}.lbl{font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.trimbox{display:flex;flex-direction:column;gap:8px}
.track{position:relative;height:30px;border-radius:8px;background:var(--soft);cursor:pointer;touch-action:none;margin:0 8px}
.track .sel{position:absolute;top:0;bottom:0;background:var(--acc2);border-top:3px solid var(--acc);border-bottom:3px solid var(--acc)}
.track .hd{position:absolute;top:-3px;bottom:-3px;width:14px;margin-left:-7px;border-radius:5px;background:var(--acc);cursor:ew-resize;touch-action:none}
.track .hd::after{content:'';position:absolute;left:5px;right:5px;top:9px;bottom:9px;border-left:2px solid #fff8;border-right:2px solid #fff8}
.track .moment{position:absolute;top:2px;bottom:2px;width:2px;margin-left:-1px;background:var(--warn);pointer-events:none}
.track .ph{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:var(--fg);opacity:.7;pointer-events:none}
.tlab b{font-variant-numeric:tabular-nums}
details.fine{background:none;padding:0}details.fine>summary{font-size:13px;color:var(--mut)}details.fine .row{margin-top:6px}
.info{display:flex;flex-direction:column;gap:6px}
.tags{display:flex;gap:6px;flex-wrap:wrap}.tag{font-size:12px;padding:2px 8px;border-radius:6px;background:var(--soft)}
.decide{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding-top:10px;border-top:1px solid var(--line)}
button.big{padding:9px 18px;font-weight:600;border-radius:10px}
button.big.good{background:#16a34a;border-color:#16a34a;color:#fff}button.big.bad{background:var(--badbg);border-color:transparent;color:var(--bad)}
button.big kbd{background:#0002;border-color:#0003;color:inherit}
a{color:var(--acc)}a.chan{margin-left:auto;font-size:13px}a.file{font-size:13px}
details.stage{background:var(--soft);border-radius:10px;padding:0}
details.stage>summary{display:flex;gap:10px;align-items:center;padding:9px 12px;cursor:pointer;list-style:none;font-size:14px}
details.stage>summary::-webkit-details-marker{display:none}
details.stage>summary::after{content:'▸';margin-left:6px;color:var(--mut)}details.stage[open]>summary::after{content:'▾'}
details.stage .num{flex:none;width:22px;height:22px;border-radius:50%;background:var(--line);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
details.stage.ok .num{background:var(--good);color:#fff}
details.stage .st{margin-left:auto;color:var(--mut);font-size:13px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}
details.stage.locked>summary{cursor:default;opacity:.6}details.stage.locked>summary::after{content:''}
.sbody{padding:4px 12px 12px;display:flex;flex-direction:column;gap:8px}.sbody>button{align-self:flex-start}
.sbody input[type=text],.sbody textarea{width:100%;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--card)}
.form2{display:grid;grid-template-columns:auto 1fr;gap:6px 10px;align-items:center}.form2 select{min-width:0;width:100%}
label{display:flex;gap:6px;align-items:center;font-size:14px}label.plat{padding:4px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
.empty{grid-column:1/-1;text-align:center;padding:60px 16px;display:flex;flex-direction:column;gap:6px}
button.more{grid-column:1/-1;justify-self:center}
.note{font-size:13px;white-space:pre-wrap}.note.bad{color:var(--bad)}.note.good{color:var(--good)}.note.warn{color:var(--warn);background:var(--warnbg);padding:6px 8px;border-radius:8px}
.hint{font-size:13px;color:var(--mut)}
video.vert{max-height:360px;width:auto;max-width:100%;align-self:flex-start}
.chat{font-size:13px;background:var(--soft);border-radius:8px;padding:6px 8px;max-height:60px;overflow:auto}
.modal{position:fixed;inset:0;background:#000b;display:flex;align-items:flex-start;justify-content:center;overflow:auto;padding:16px;z-index:10}
.mbox{background:var(--card);border-radius:14px;padding:16px;max-width:1180px;width:100%;display:flex;flex-direction:column;gap:10px}
.mbox h2{margin:0 0 4px;font-size:19px}
.mcols{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start}
canvas{max-width:100%;border-radius:8px;touch-action:none;cursor:crosshair;background:#000}
.pv{height:560px;max-width:100%;border-radius:8px;background:#000;object-fit:contain}
.pw{position:relative;height:560px;aspect-ratio:9/16;background:#000;border-radius:8px;overflow:hidden;flex:none}
.pvimg{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;visibility:hidden}
.capbox{position:absolute;border:2px dashed #22c55e;background:#22c55e22;display:flex;align-items:center;justify-content:center;color:#fff;font:800 14px Arial,sans-serif;text-align:center;cursor:move;touch-action:none;overflow:hidden;box-sizing:border-box;text-shadow:0 0 4px #000,0 0 4px #000}
.caph{position:absolute;right:0;bottom:0;width:14px;height:14px;background:#22c55e;cursor:nwse-resize}
.acc{background:var(--soft);border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:6px}
.acc input[type=text],.acc input[type=password]{padding:6px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.acc ol{margin:4px 0 4px 18px;padding:0;font-size:13px;color:var(--mut)}
.steps{margin:4px 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:8px}
.steps li{display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap;font-size:13px}.steps li.done .t{text-decoration:line-through;opacity:.55}
.steps .t{flex:1 1 320px}a.btn{font:inherit;font-size:13px;padding:4px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);text-decoration:none;white-space:nowrap}
.chip{font-size:12px;padding:2px 10px;border-radius:999px;background:var(--soft)}.chip.ok{background:var(--goodbg);color:var(--good)}
.drop{border:2px dashed var(--line);border-radius:10px;padding:12px;text-align:center;font-size:13px}.drop.over{border-color:var(--acc);background:var(--acc2)}
code.cp{font:12px ui-monospace,monospace;background:var(--soft);border-radius:6px;padding:2px 6px;word-break:break-all}
pre.share{white-space:pre-wrap;font:12px/1.4 system-ui;background:var(--soft);border-radius:6px;padding:6px;margin:4px 0;max-height:120px;overflow:auto}
details{background:var(--soft);border-radius:8px;padding:6px 8px}summary{cursor:pointer;font-size:14px}
details input[type=text],details input[type=password],details textarea{width:100%;box-sizing:border-box;font:inherit;padding:6px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);margin-top:6px}
@media(max-width:600px){main{padding:10px}header.top{position:static;padding:8px 10px}.keys{display:none}h1{font-size:17px}.tab{padding:5px 8px;font-size:14px}.card{padding:12px}}
</style>
<header class=top><div class=bar><h1>Clip review</h1><nav class=tabs id=tabs></nav><span class=grow></span>
<button id=upd class="sm good" hidden></button><div class=menu><button id=setupBtn title="Accounts, video layout and folders">⚙ Setup ▾</button><div class=menu-list id=setupList hidden>
<button id=connect title="Set up automatic posting">🔗 Connect accounts</button><button id=layoutBtn title="Where the webcam and the game go in vertical clips, per streamer">📐 Video layout</button><button id=folders>📁 Folders</button></div></div>
<button id=cpBtn title="Paid clipping campaigns">💰 Campaigns <span class=n id=cpN hidden></span></button><button id=ap class=primary title="Learns which clips you keep and can post the good ones for you">🤖 Autopilot</button></div>
<div class=sub><select id=s title="Order"><option value=score>Best score first</option><option value=created>Newest first</option><option value=ap>Autopilot pick first</option></select><span class=mut id=count></span><span class=grow></span><span class=keys>Keys: <kbd>K</kbd> keep <kbd>T</kbd> trash <kbd>U</kbd> undo <kbd>←</kbd><kbd>→</kbd> next clip <kbd>Space</kbd> play</span><button id=purge class="danger sm" hidden>Delete trashed files</button></div></header>
<main><div class=grid id=g></div></main>
<script>
let clips=[],shown=30,sig='',shareInfo={providers:[],platforms:{}};const openSt=new Map(),NL=String.fromCharCode(10);const trims=new Map();const exUi=new Map(),exPending=new Set();const g=document.getElementById('g');
const enc=id=>id.split('/').map(encodeURIComponent).join('/');
const fmt=t=>Math.floor(t/60)+':'+String(Math.floor(t%60)).padStart(2,'0');
const fileUrl=(c,f)=>'/files/'+enc(c.dir)+'/'+encodeURIComponent(f);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
async function load(force){
  const next=await (await fetch('/api/clips')).json();
  const ns=sigOf(next);
  const before=shownIds,wasBusy=clips.filter(exBusy).map(c=>c.id);clips=next;
  const finished=wasBusy.some(id=>!exBusy(next.find(c=>c.id===id)));      // your own export just finished: show it now
  if(ns===sig){paintUpd(0)}
  else if(force||finished||!userBusy())render()
  else paintUpd(next.filter(c=>!before.has(c.id)).length||-1)
  for(const id of exUi.keys())paintEx(id)}                // buttons follow the SERVER state, even when a playing video holds back the redraw
let lastAct=0,shownIds=new Set();for(const ev of ['pointerdown','keydown','wheel','input'])document.addEventListener(ev,()=>{lastAct=Date.now()},{capture:true,passive:true});
function userBusy(){                                    // you are working: do not redraw under your hands, offer it instead
  const ae=document.activeElement,tag=ae?ae.tagName:'';
  return Date.now()-lastAct<30000||['INPUT','TEXTAREA','SELECT'].includes(tag)||!!document.querySelector('.modal')||[...document.querySelectorAll('video')].some(v=>!v.paused)}
function paintUpd(n){const b=document.getElementById('upd');b.hidden=!n;b.textContent=n>0?n+' new clip'+(n>1?'s':'')+' · Show':'Updates ready · Show'}
const exBusy=c=>c&&c.export_job&&(c.export_job.state==='queued'||c.export_job.state==='encoding');
function paintEx(id){
  const u=exUi.get(id);if(!u)return;const c=clips.find(x=>x.id===id),j=c&&c.export_job;
  const busy=exPending.has(id)||exBusy(c);
  u.ex.disabled=busy;u.ex.textContent=busy?((j&&j.state==='encoding')?'Encoding…':'Queued…'):'Export clip';if(u.st)u.st.textContent=busy?((j&&j.state==='encoding')?'encoding…':'queued…'):(!busy&&j&&j.state==='error'?'export failed':u.base);
  u.note.replaceChildren();u.note.style.display='none';
  if(!busy&&j&&j.state==='error'){u.note.style.display='';u.note.append('Export failed: '+j.message+' ');
    const d=el('button',null,'Dismiss');d.onclick=async()=>{await post('/api/export/dismiss',{id});await load(false)};u.note.append(d)}}
function numInput(v,on){const i=el('input');i.type='number';i.step='0.1';i.min='0';i.value=v;i.oninput=()=>on(parseFloat(i.value));return i}
const LABEL={new:'To review',keep:'Kept',trash:'Trash',done:'Archived',all:'All'};let filter='new',cur=null;
function ago(iso){const s=(Date.now()-new Date(iso).getTime())/1000;if(!(s>=0))return String(iso).replace('T',' ');if(s<90)return 'just now';
  if(s<3600)return Math.round(s/60)+' min ago';if(s<86400)return Math.round(s/3600)+' h ago';return Math.round(s/86400)+' d ago'}
function paintTabs(){const tabs=document.getElementById('tabs'),n={all:clips.length};for(const c of clips)n[c.status]=(n[c.status]||0)+1;tabs.replaceChildren();
  for(const k of ['new','keep','trash','done','all']){const b=el('button','tab'+(k===filter?' on':''));b.append(LABEL[k]+' ',el('span','n',String(n[k]||0)));
    b.onclick=()=>{filter=k;shown=30;cur=null;render()};tabs.append(b)}
  document.getElementById('purge').hidden=filter!=='trash'||!n.trash}
function head(c){const h=el('div','head'),sc=el('div','score',String(Math.round(c.score)));sc.title='Clip score: how much bigger the chat reaction was than normal';
  const tx=el('div','htx'),t2=el('div','mut',c.channel+' · '+ago(c.created)+(c.duration_s&&c.status!=='done'?' · '+fmt(c.duration_s)+' draft':''));t2.title=String(c.created).replace('T',' ');
  tx.append(el('div','htitle',(c.description||{}).title||c.channel),t2);h.append(sc,tx);
  if(c.status!=='new')h.append(el('span','pill '+c.status,{keep:'Kept',trash:'Trashed',done:'Archived · files freed'}[c.status]||c.status));return h}
function stage(c,key,num,title,state,ok,body,locked){
  const d=el('details','stage'+(ok?' ok':'')+(locked?' locked':''));d.open=!locked&&!!openSt.get(c.id+key);d.ontoggle=()=>openSt.set(c.id+key,d.open);
  const s=el('summary'),stx=el('span','st',state);s.append(el('span','num',ok?'✓':num),el('b',null,title),stx);d.append(s);
  if(locked)s.onclick=e=>e.preventDefault();else d.append(body);d.stx=stx;return d}
async function setStatus(c,st){if(st===c.status)return;
  const cards=[...g.querySelectorAll('.card')],i=cards.findIndex(x=>x.dataset.id===c.id),leaves=filter!=='all'&&filter!==st;
  const nx=leaves?(cards[i+1]||cards[i-1]):cards[i];cur=nx?nx.dataset.id:null;
  if(await post('/api/status',{id:c.id,status:st}))await load(true);focusCur()}
function focusCur(scroll){const k=[...g.querySelectorAll('.card')].find(x=>x.dataset.id===cur);if(k){k.focus({preventScroll:true});if(scroll!==false)k.scrollIntoView({block:'nearest',behavior:'smooth'})}}
function sigOf(list){return JSON.stringify(list.map(c=>[c.id,c.status,(c.exports||[]).length,c.shares||null,c.res?c.res[1]:0,c.taste?c.taste.pick:null,c.autopilot?c.autopilot.state:null,c.campaign?JSON.stringify([c.campaign.live,c.campaign.ready,c.campaign.problems,c.campaign.posts.length,c.campaign.flags_missing]):null,c.voiceover?c.voiceover.at:null]))}
function render(){
  sig=sigOf(clips);shownIds=new Set(clips.map(c=>c.id));paintUpd(0);          // whatever redraws, the screen is now up to date
  const s=document.getElementById('s').value,pae=document.activeElement,hadCard=!!(pae&&pae.classList&&pae.classList.contains('card'));paintTabs();
  let list=clips.filter(c=>filter==='all'||c.status===filter);
  list.sort((a,b)=>s==='score'?b.score-a.score:s==='ap'?((b.taste?b.taste.p:-1)-(a.taste?a.taste.p:-1)):b.created.localeCompare(a.created));
  g.replaceChildren();exUi.clear();const total=list.length;list=list.slice(0,shown);
  document.getElementById('count').textContent=total?(total>list.length?'Showing '+list.length+' of '+total+' clips':total+' clip'+(total>1?'s':'')):'';
  if(!total){const e=el('div','empty');e.append(el('b',null,filter==='new'?'Nothing to review right now.':'No clips here.'),el('div','mut',filter==='new'?'New clips appear here by themselves while the clipper is running.':'Pick another tab above.'));g.append(e)}
  let moreBtn=null;if(total>list.length){moreBtn=el('button','primary more','Show '+Math.min(30,total-list.length)+' more');moreBtn.onclick=()=>{shown+=30;render()}}
  for(const c of list){
    if(!trims.has(c.id)){const n=c.clip_len_s||30;let a=c.suggest_start_s,b=c.suggest_end_s;
      if(b-a>n+1){a=Math.min(Math.max(c.moment_s-12,0),Math.max(c.duration_s-n,0));b=Math.min(a+n,c.duration_s)}   // older, longer suggestions -> 30 s
      trims.set(c.id,{start:+a.toFixed(1),end:+b.toFixed(1),vertical:true,captions:false})}
    const t=trims.get(c.id);
    const card=el('div','card '+c.status);card.tabIndex=0;card.dataset.id=c.id;card.addEventListener('focusin',()=>{cur=c.id});
    card.append(head(c));
    if(c.status==='done'){                              // posted and files deleted: just what was posted and where
      for(const [k,v0] of Object.entries(c.shares||{})){const line=el('div','mut',(shareInfo.platforms[k]||{label:k}).label+': '+v0.status+' '+(v0.at||'').slice(0,16).replace('T',' '));
        if(v0.url){const a0=el('a',null,'open');a0.href=safeUrl(v0.url);a0.target='_blank';line.append(' ',a0)}card.append(line)}
      const s0=el('a',null,'Channel');s0.href=c.source_url;s0.target='_blank';card.append(s0);g.append(card);continue}
    {const ab=apBadge(c);if(ab)card.append(ab)}
    {const cb=cpBadge(c);if(cb)card.append(cb)}
    const v=el('video');v.controls=true;v.preload='metadata';v.src=fileUrl(c,c.file);
    v.onloadedmetadata=()=>{v.currentTime=t.start};
    v.ontimeupdate=()=>{if(!v.paused&&v.currentTime>t.end+0.2)v.currentTime=t.start};
    card.append(v);
    // ---- trim: a bar you drag, plus fine-tuning
    const D=c.duration_s||Math.max(t.end,1),tb=el('div','trimbox'),track=el('div','track'),sel=el('div','sel'),h1=el('div','hd'),h2=el('div','hd'),mk=el('div','moment'),ph=el('div','ph');
    mk.title='The moment chat exploded';h1.title='Drag: where the clip starts';h2.title='Drag: where the clip ends';track.title='Click to jump the player here';track.append(sel,mk,ph,h1,h2);
    const lab=el('span','tlab');
    const a=numInput(t.start,x=>{if(!isNaN(x)){t.start=x;sync(false)}}),b=numInput(t.end,x=>{if(!isNaN(x)){t.end=x;sync(false)}});
    const pc=x=>Math.min(Math.max(100*x/D,0),100)+'%';
    function sync(inputs){sel.style.left=pc(t.start);sel.style.width=Math.max(0,Math.min(100*(t.end-t.start)/D,100))+'%';h1.style.left=pc(t.start);h2.style.left=pc(t.end);mk.style.left=pc(c.moment_s||0);
      if(inputs!==false){a.value=t.start;b.value=t.end}lab.replaceChildren(el('b',null,fmt(t.start)+' – '+fmt(t.end)),el('span','mut','  '+Math.max(t.end-t.start,0).toFixed(1)+' s'))}
    const xt=ev=>{const r=track.getBoundingClientRect();return Math.round(Math.min(Math.max((ev.clientX-r.left)/r.width,0),1)*D*10)/10};
    for(const [h,isA] of [[h1,true],[h2,false]]){h.onpointerdown=e=>{e.preventDefault();e.stopPropagation();h.setPointerCapture(e.pointerId);
      h.onpointermove=ev=>{const x=xt(ev);if(isA)t.start=Math.min(x,+(t.end-1).toFixed(1));else t.end=Math.max(x,+(t.start+1).toFixed(1));sync();v.currentTime=isA?t.start:Math.max(t.end-2,t.start)};
      h.onpointerup=h.onpointercancel=()=>{h.onpointermove=null}}}
    track.onpointerdown=e=>{if(e.target!==h1&&e.target!==h2)v.currentTime=xt(e)};
    v.addEventListener('timeupdate',()=>{ph.style.left=pc(v.currentTime)});
    const setA=el('button','sm','Start here');setA.title='Start the clip where the player is now';setA.onclick=()=>{t.start=+v.currentTime.toFixed(1);if(t.end<=t.start)t.end=+Math.min(D,t.start+(c.clip_len_s||30)).toFixed(1);sync()};
    const setB=el('button','sm','End here');setB.title='End the clip where the player is now';setB.onclick=()=>{t.end=+Math.max(v.currentTime,0.1).toFixed(1);if(t.start>=t.end)t.start=+Math.max(0,t.end-(c.clip_len_s||30)).toFixed(1);sync()};
    const shift=d=>{const L=c.duration_s,n=t.end-t.start;let a=Math.min(Math.max(t.start+d,0),Math.max(L-n,0));t.start=+a.toFixed(1);t.end=+(a+n).toFixed(1);sync();v.currentTime=t.start};
    const left=el('button','sm','◀ 5 s');left.title='Slide the whole clip 5 s earlier';left.onclick=()=>shift(-5);
    const right=el('button','sm','5 s ▶');right.title='Slide the whole clip 5 s later';right.onclick=()=>shift(5);
    const snap=el('button','sm','30 s');snap.title='Make the clip exactly 30 s from the start point';snap.onclick=()=>{t.end=+Math.min(t.start+(c.clip_len_s||30),c.duration_s).toFixed(1);sync()};
    const tr1=el('div','row');tr1.append(el('span','lbl','Clip'),lab,el('span','grow'),setA,setB);
    const ft=el('details','fine');ft.open=!!openSt.get(c.id+'ft');ft.ontoggle=()=>openSt.set(c.id+'ft',ft.open);ft.append(el('summary',null,'More trim options'));const fr=el('div','row');fr.append(left,right,el('span','mut','Start'),a,el('span','mut','End'),b,snap);ft.append(fr);
    tb.append(track,tr1,ft);sync();card.append(tb);
    // ---- what happened in chat
    const sp=c.spike||{},info=el('details','fine');info.open=!!openSt.get(c.id+'why');info.ontoggle=()=>openSt.set(c.id+'why',info.open);info.append(el('summary',null,'Why this clip'));
    info.append(el('div','mut',(sp.peak!=null?sp.peak+' chatters at once (normal '+Math.round(sp.baseline||0)+') · '+(sp.msgs||0)+' msgs':'No chat data for this clip')+(c.loudness_jump_db!=null?' · audio '+(c.loudness_jump_db>0?'+':'')+c.loudness_jump_db+' dB':'')+(c.res?' · '+c.res[1]+'p':'')));
    if(sp.top&&sp.top.length){const tg=el('div','tags');for(const x of sp.top.slice(0,8))tg.append(el('span','tag',x[0]+' ×'+x[1]));info.append(tg)}
    card.append(info);
    if(c.res&&c.res[1]<1000)card.append(el('div','note warn','⚠ Recorded at '+c.res[1]+'p, so it cannot look sharp when enlarged to 1080x1920. Clips recorded after the update use the best quality the stream offers.'));
    // ---- the decision: always the same place, big
    const dec=el('div','decide');
    const big=(label,st,cls,key)=>{const x=el('button','big '+cls);x.append(label,el('kbd',null,key));x.title=label+' (key '+key+')';x.onclick=()=>setStatus(c,st);return x};
    if(c.status!=='keep')dec.append(big('Keep','keep','good','K'));
    if(c.status!=='trash')dec.append(big('Trash','trash','bad','T'));
    if(c.status!=='new')dec.append(big('Undo','new','','U'));
    if(Object.values(c.shares||{}).some(x=>['done','draft'].includes(x.status))){
      const dn=el('button','big','Archive (free space)');dn.title='Posted already: delete this clip'+String.fromCharCode(39)+'s video files here to free disk space. Counts as a Keep for learning. Copies in your final clips folder are not touched.';
      dn.onclick=async()=>{if(!confirm('Delete the draft and export videos of this clip to free space?'+NL+NL+'It stays listed under Archived and counts as a Keep. Copies in your final clips folder are kept.'))return;
        await post('/api/status',{id:c.id,status:'done'});load(true)};dec.append(dn)}
    const src=el('a','chan','Open channel ↗');src.href=c.source_url;src.target='_blank';dec.append(src);
    card.append(dec);
    // ---- step 1: export
    const nx=(c.exports||[]).length,b1=el('div','sbody');
    const r2=el('div','row');
    for(const [k,label] of [['vertical','Vertical 9:16'],['captions','Captions']]){const l=el('label'),cb=el('input');cb.type='checkbox';cb.checked=t[k];cb.onchange=()=>{t[k]=cb.checked};l.append(cb,label);r2.append(l)}
    const ex=el('button','primary','Export clip'),exNote=el('div','note bad');exNote.style.display='none';
    ex.onclick=async()=>{exPending.add(c.id);paintEx(c.id);const ok=await post('/api/export/start',{id:c.id,start:t.start,end:t.end,vertical:t.vertical,captions:t.captions,hook:t.hookOff?'':(t.hook||'auto'),hook_style:t.hookStyle||'',hook_src:t.hookSrc||'',voiceover:!!(t.vo&&c.voiceover)});
      exPending.delete(c.id);await load(false)};          // the server now owns the state: it flips back only when the file is really made
    r2.append(el('span','grow'),ex);b1.append(r2,exNote,cpExportExtras(c,t),rfBox(c,t));
    if(nx){const e=el('div','row');e.append(el('span','mut','Files:'));for(const x of c.exports){const l=el('a','file',(x.vertical?'9:16 ':'')+(x.captions?'CC ':'')+fmt(x.start)+'-'+fmt(x.end));l.href=fileUrl(c,x.file);l.target='_blank';
      const sf=el('button','sm','📂');sf.title='Show this file in its folder'+(x.copy?' (a copy is also in your final clips folder)':'');sf.onclick=()=>post('/api/reveal',{id:c.id,file:x.file});e.append(l,sf)}b1.append(e)}
    const lb=el('button','sm',layouts[c.channel]?'Edit 9:16 layout for '+c.channel+' ✓':'Set 9:16 layout for '+c.channel+' (webcam + gameplay)');lb.onclick=()=>openLayout(c,v);b1.append(lb);
    const vx=(c.exports||[]).filter(x=>x.vertical).slice(-1)[0];
    if(vx){const pvid=el('video','vert');pvid.controls=true;pvid.preload='metadata';pvid.src=fileUrl(c,vx.file);b1.append(el('div','mut','Latest 9:16 export:'),pvid)}
    const s1base=nx?nx+' file'+(nx>1?'s':'')+' made':'not exported yet';
    const st1=stage(c,'ex','1','Export',s1base,nx>0,b1,false);card.append(st1);
    exUi.set(c.id,{ex,note:exNote,st:st1.stx,base:s1base});paintEx(c.id);
    // ---- step 2: post text
    let getText=()=>({title:'',body:''});
    {const d=c.description||{title:'',body:'',alts:[]},det=el('div','sbody');getText=()=>({title:ti.value,body:ta.value});
      const ht=card.querySelector('.htitle');
      let ti=el('input');ti.type='text';ti.value=d.title||'';ti.placeholder='Title';let ta=el('textarea');ta.rows=4;ta.value=d.body||'';ta.placeholder='Description';
      let timer;const save=()=>{clearTimeout(timer);ht.textContent=ti.value||c.channel;timer=setTimeout(()=>post('/api/describe',{id:c.id,title:ti.value,body:ta.value}),600)};
      ti.oninput=save;ta.oninput=save;let alts=(d.alts||[]).slice();let ai=Math.max(alts.indexOf(d.title),0);const hint=el('div','hint');
      const fetchNew=async listen=>{hint.textContent=listen?'Listening to the clip (first time can take a minute)...':'Thinking up new titles...';
        const tt=trims.get(c.id)||{},r=await fetch('/api/describe/new',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.id,listen:!!listen,start:tt.start,end:tt.end})});
        if(!r.ok){hint.textContent=await r.text();return false}
        const nd=await r.json();alts=nd.alts||[];ai=0;if(alts.length)ti.value=alts[0];
        if(nd.body&&ta.value===(d.auto_body||d.body)){ta.value=nd.body}                 // only replace the description if you have not edited it
        d.auto_body=nd.auto_body;d.body=ta.value;hint.textContent=nd.note||(nd.used_speech?'Titles use what the streamer said.':'Titles use the chat reaction.');save();return true};
      const other=el('button','sm','↻ New title');other.title='Next suggestion; makes a new set when they run out';other.onclick=async()=>{other.disabled=true;
        if(alts.length>1&&ai+1<alts.length){ai++;ti.value=alts[ai];save()}else{await fetchNew(false)}other.disabled=false};
      const say=el('button','sm','🎧 Use the streamer'+String.fromCharCode(39)+'s words');say.title='Listen to this clip (needs captions set up) and quote the words the streamer said in the titles';say.onclick=async()=>{say.disabled=true;await fetchNew(true);say.disabled=false};
      const cp=el('button','sm','Copy title + description');cp.onclick=async()=>{const t=ti.value+NL+NL+ta.value;
        try{await navigator.clipboard.writeText(t)}catch(e){ta.select();document.execCommand('copy')}cp.textContent='Copied ✓';setTimeout(()=>cp.textContent='Copy title + description',1500)};
      const dr=el('div','row');dr.append(other,say,cp);det.append(el('div','lbl','Title'),ti,el('div','lbl','Description'),ta,dr,hint);
      const mine=['picked','typed'].includes(d.title_src)||d.body_edited;
      card.append(stage(c,'tx','2','Post text',mine?'edited by you':'suggested'+(d.category?' · '+d.category:''),mine,det,false))}
    // ---- step 3: share
    const posted=Object.entries(c.shares||{}).filter(([k,x])=>['done','draft'].includes(x.status)).map(([k])=>(shareInfo.platforms[k]||{label:k}).label);
    if(nx){
      const sd=el('div','sbody');
      const fsel=el('select');const exs=c.exports.slice().reverse();for(const x of exs){const o=el('option',null,x.file+(x.vertical?' (9:16)':' (not vertical)'));o.value=x.file;fsel.append(o)}
      const vi=exs.findIndex(x=>x.vertical);if(vi>0)fsel.selectedIndex=vi;
      const psel=el('select');for(const p of shareInfo.providers){const o=el('option',null,p.label+(p.configured?'':' (not set up)'));o.value=p.name;o.disabled=!p.configured;psel.append(o)}
      const pick=n=>(shareInfo.providers.find(p=>p.name===n)||{}).configured;if(pick('auto'))psel.value='auto';else if(pick('direct'))psel.value='direct';
      const boxes={},row=el('div','row');
      for(const [k,v] of Object.entries(shareInfo.platforms)){const l=el('label','plat'),cb=el('input');cb.type='checkbox';cb.checked=true;boxes[k]=cb;l.append(cb,v.label);row.append(l)}
      const fl=el('label'),force=el('input');force.type='checkbox';fl.append(force,'post again if already posted');
      const st=el('div','mut');const stat=()=>{st.replaceChildren();for(const [k,v] of Object.entries(c.shares||{})){const line=el('div',null,(shareInfo.platforms[k]||{label:k}).label+': '+v.status+' '+(v.at||'').slice(11,16)+(v.message?' - '+v.message:''));
        if(v.url){const a2=el('a',null,'open');a2.href=v.url;a2.target='_blank';line.append(' ',a2)}
        if(v.status==='opened'||v.status==='draft'){const m=el('button','sm','Mark as posted');m.onclick=async()=>{await post('/api/share/mark',{id:c.id,platform:k,status:'done'});load(true)};line.append(' ',m)}
        if(v.status==='done'){const m=el('button','sm','Reset');m.onclick=async()=>{await post('/api/share/mark',{id:c.id,platform:k,status:'reset'});load(true)};line.append(' ',m)}
        st.append(line)}};stat();
      const go=el('button','primary','Share selected…');
      go.onclick=async()=>{
        const chosen=Object.keys(boxes).filter(k=>boxes[k].checked);if(!chosen.length){alert('Tick at least one platform.');return}
        const tx=getText(),texts={};for(const k of chosen)texts[k]={title:tx.title,body:tx.body};
        const prov=shareInfo.providers.find(p=>p.name===psel.value)||{label:psel.value,automatic:false};
        const names=chosen.map(k=>shareInfo.platforms[k].label).join(', ');
        let warnIg='';const ck=await fetch('/api/share/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.id,file:fsel.value,platforms:chosen})});
        if(ck.ok){const k=await ck.json();if(k.block){alert('Instagram Reels '+k.block+'.'+NL+NL+'Untick Instagram to post to the others now.');return}
          if(k.same_moment.length)warnIg=NL+NL+'⚠ This moment was already posted:'+NL+k.same_moment.join(NL)+NL+'Instagram may show a repeat to fewer people.'}
        if(!confirm('Share this clip?'+NL+NL+'File: '+fsel.value+NL+'Title: '+tx.title+NL+'To: '+names+NL+'How: '+prov.label+NL+NL+(prov.automatic?'It is uploaded to your connected account(s) the moment you press OK.':'Nothing is posted or opened until you press OK.')+warnIg))return;
        go.disabled=true;go.textContent='Working…';
        const send=async plats=>{const r=await fetch('/api/share',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.id,file:fsel.value,platforms:plats,texts,provider:psel.value,force:force.checked})});
          if(!r.ok){alert(await r.text());return null}return (await r.json()).results};
        if(prov.automatic){const res=await send(chosen);if(res){try{await navigator.clipboard.writeText(Object.values(res)[0].text)}catch(e){}alert(Object.entries(res).map(([k,v])=>shareInfo.platforms[k].label+': '+v.status+(v.message?' - '+v.message:'')+(v.url?NL+v.url:'')).join(NL+NL))}}
        else{for(const k of chosen){const res=await send([k]);if(!res)break;const r1=res[k];
            try{await navigator.clipboard.writeText(r1.text)}catch(e){}
            if(confirm(shareInfo.platforms[k].label+': page opened, file selected, caption copied to your clipboard.'+NL+NL+'Paste the caption, attach the video and press Post there.'+NL+'Press OK once it is posted (marks it done), or Cancel to leave it as "opened".'))await post('/api/share/mark',{id:c.id,platform:k,status:'done'})}}
        go.disabled=false;go.textContent='Share selected…';load(true)};
      const cn=el('div','row');const paintCn=()=>{cn.replaceChildren();const auto=psel.value==='auto';if(!(auto||psel.value==='direct')||!shareInfo.connected)return;const need=Object.keys(shareInfo.platforms).filter(k=>!shareInfo.connected[k]&&!(auto&&(shareInfo.quick||{})[k]));if(!need.length)return;
        cn.append(el('span','mut','Not connected yet:'));for(const k of need){const b=el('button','sm','Connect '+shareInfo.platforms[k].label);b.onclick=()=>auto&&k!=='youtube'?openQuick():openAccounts(k);cn.append(b)}};psel.addEventListener('change',paintCn);paintCn();
      const g2=el('div','form2');if(exs.length>1)g2.append(el('span','lbl','File'),fsel);g2.append(el('span','lbl','How to post'),psel);
      const gr=el('div','row');if(Object.keys(c.shares||{}).length)gr.append(fl);gr.append(el('span','grow'),go);
      sd.append(g2,cn,row,gr,st);
      card.append(stage(c,'sh','3','Share',posted.length?'posted: '+posted.join(', '):'not shared yet',posted.length>0,sd,false))}
    else card.append(stage(c,'sh','3','Share','export the clip first',false,null,true));
    g.append(card)}
  if(moreBtn)g.append(moreBtn);
  if(cur&&hadCard)focusCur(false)}                    // a background redraw keeps a focused card focused, but never moves you
document.addEventListener('keydown',e=>{
  if(e.ctrlKey||e.metaKey||e.altKey||document.querySelector('.modal'))return;
  const tg=e.target,tag=(tg.tagName||'');if(['INPUT','TEXTAREA','SELECT'].includes(tag)||tg.isContentEditable)return;
  const cards=[...g.querySelectorAll('.card')];if(!cards.length)return;
  let i=cards.findIndex(x=>x.dataset.id===cur);const k=e.key.toLowerCase();
  if(['arrowright','arrowleft'].includes(k)&&tag==='VIDEO')return;              // the player uses the arrows itself
  if(['arrowright','l','arrowleft','j'].includes(k)){e.preventDefault();const fw=k==='arrowright'||k==='l';i=i<0?0:Math.min(Math.max(i+(fw?1:-1),0),cards.length-1);cur=cards[i].dataset.id;focusCur();return}
  if(i<0||e.repeat||!(tg.closest&&tg.closest('.card')))return;const c=clips.find(x=>x.id===cur);if(!c||c.status==='done')return;
  if(k==='k'){e.preventDefault();setStatus(c,'keep')}else if(k==='t'){e.preventDefault();setStatus(c,'trash')}else if(k==='u'&&c.status!=='new'){e.preventDefault();setStatus(c,'new')}
  else if(k===' '&&tag!=='BUTTON'&&tag!=='VIDEO'&&tag!=='SUMMARY'){const v=cards[i].querySelector('video');if(v){e.preventDefault();v.paused?v.play():v.pause()}}});
async function post(u,d){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok){alert(await r.text());return false}return true}

let layouts={};
async function loadShare(){try{shareInfo=await (await fetch('/api/share/providers')).json()}catch(e){}paintSetupDot()}
function paintSetupDot(){const sb=document.getElementById('setupBtn'),none=!(shareInfo.providers||[]).some(p=>p.automatic&&p.configured);sb.classList.toggle('dot',none);sb.title=none?'No account connected for posting yet: open Connect accounts':'Accounts, video layout and folders'}
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
const lsGet=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
const lsSet=(k,on)=>{try{on?localStorage.setItem(k,'1'):localStorage.removeItem(k)}catch(e){}};
const safeUrl=u=>/^https:[/][/]/.test(u)?u:'#';
async function copyText(t){try{await navigator.clipboard.writeText(t);return true}catch(e){const x=el('textarea');x.value=t;document.body.append(x);x.select();let ok=false;try{ok=document.execCommand('copy')}catch(y){}x.remove();return ok}}
const GID_SUFFIX='.apps.googleusercontent.com';
async function openAccounts(focus){
  let S=await (await fetch('/api/accounts')).json();
  const notes={};                                   // platform -> {text, kind, href}: survives a redraw of the cards
  const ov=el('div','modal'),box=el('div','mbox');ov.append(box);box.append(el('h2',null,'Connect your accounts'));
  box.append(el('div','hint','For YouTube, or if you prefer your own developer apps. Instagram + TikTok are easier through Setup, Connect accounts. Only needed for automatic posting. Every platform makes you create a free developer app on your own account (that cannot be skipped); each step below opens the exact page and tells you what to press. Your logins stay on this PC (accounts.json in the autoclip settings folder), are never shown again and are only ever sent to the platform itself.'));
  const list=el('div');list.style.cssText='display:flex;flex-direction:column;gap:10px';box.append(list);
  const refresh=async()=>{S=await (await fetch('/api/accounts')).json();await loadShare()};
  const card=pl=>{
    const info=S.platforms[pl],guide=S.setup[pl],label=(shareInfo.platforms[pl]||{label:pl}).label;
    const sec=el('div','acc');sec.dataset.pl=pl;
    const head=el('div','row');head.append(el('b',null,label),el('span','chip'+(info.connected?' ok':''),info.connected?'✅ Connected'+(info.account?' · '+info.account:''):'Not connected'));sec.append(head);
    const msg=el('div','note');
    const setNote=(text,kind,href)=>{notes[pl]={text,kind,href};paint()};
    const paint=()=>{const n=notes[pl];msg.replaceChildren();msg.className='note'+(n&&n.kind?' '+n.kind:'');if(!n)return;msg.append(n.text);if(n.href){const a=el('a',null,' Open the sign-in page');a.href=safeUrl(n.href);a.target='_blank';a.rel='noopener';msg.append(a)}};
    // --- guided steps (progress is remembered in this browser)
    const det=el('details');det.open=!info.connected&&(!focus||focus===pl);const sum=el('summary');det.append(sum);
    const key=i=>'acc-'+pl+'-'+i;
    const prog=()=>{const n=guide.steps.filter((x,i)=>lsGet(key(i))==='1').length;sum.textContent=(info.connected?'Setup steps':'How to connect: '+guide.steps.length+' steps, about '+guide.minutes+' minutes')+' ('+n+'/'+guide.steps.length+' done)'};
    const ol=el('ol','steps');
    guide.steps.forEach((s0,i)=>{
      const li=el('li'),cb=el('input');cb.type='checkbox';cb.checked=lsGet(key(i))==='1';li.classList.toggle('done',cb.checked);
      cb.onchange=()=>{lsSet(key(i),cb.checked);li.classList.toggle('done',cb.checked);prog()};
      const t=el('div','t');t.append(el('b',null,(i+1)+'. '),s0.text);
      if(s0.copy){t.append(' ',el('code','cp',s0.copy.value))}
      li.append(cb,t);
      if(s0.link){const a=el('a','btn',s0.link.label+' ↗');a.href=safeUrl(s0.link.url);a.target='_blank';a.rel='noopener';li.append(a)}
      if(s0.copy){const c=el('button',null,s0.copy.label);c.onclick=async()=>{const ok=await copyText(s0.copy.value);c.textContent=ok?'Copied ✓':'Copy failed: select the text yourself';setTimeout(()=>c.textContent=s0.copy.label,1800)};li.append(c)}
      ol.append(li)});
    prog();det.append(ol);sec.append(det);
    // --- what to paste
    const ins={};let jsonText='';
    if(pl==='youtube'){
      const drop=el('div','drop'),dmsg=el('span',null,'Drop the downloaded client JSON file here, or ');
      const fi=el('input');fi.type='file';fi.accept='.json,application/json';fi.style.display='none';
      const pick=el('button',null,'Choose file…');pick.onclick=()=>fi.click();
      const take=async f=>{if(!f)return;if(f.size>100000){dmsg.textContent='That file is too big to be a client JSON. ';return}jsonText=await f.text();drop.classList.remove('over');dmsg.textContent='✓ '+f.name+' loaded. Now press Save and connect. ';drop.style.borderColor='#16a34a'};
      fi.onchange=()=>take(fi.files[0]);
      drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};drop.ondragleave=()=>drop.classList.remove('over');
      drop.ondrop=e=>{e.preventDefault();take(e.dataTransfer.files[0])};
      drop.append(dmsg,pick,fi);sec.append(drop);
      const hand=el('details');hand.append(el('summary',null,'Or type the client ID and secret by hand'));
      const idHint=el('div','note bad');
      for(const f of info.fields){const i=el('input');i.type=f.secret?'password':'text';i.placeholder=f.label+(info.have.includes(f.key)?' (saved: leave empty to keep)':'');i.autocomplete='off';ins[f.key]=i;hand.append(i)}
      ins.client_id.oninput=()=>{const v=ins.client_id.value.trim();idHint.textContent=v&&v[0]!=='{'&&!v.endsWith(GID_SUFFIX)?'The client ID ends with '+GID_SUFFIX+'. This looks like something else (project ID? API key?).':''};
      hand.append(idHint);sec.append(hand);
    }else{
      for(const f of info.fields){const i=el('input');i.type=f.secret?'password':'text';i.placeholder=f.label+(info.have.includes(f.key)?' (saved: leave empty to keep)':'');i.autocomplete='off';ins[f.key]=i;sec.append(i)}
    }
    const opts={};
    if(pl==='youtube'){const s1=el('select');for(const v of ['public','unlisted','private']){const o=el('option',null,'Upload as: '+v);o.value=v;s1.append(o)}s1.value=info.options.privacy||'public';opts.privacy=s1;sec.append(s1)}
    if(pl==='tiktok'){const m=el('select');for(const [v,t] of [['draft','Draft: video waits in my TikTok inbox, I press Post'],['direct','Direct post (private unless TikTok audited the app)']]){const o=el('option',null,t);o.value=v;m.append(o)}m.value=info.options.mode||'draft';opts.mode=m;sec.append(m)}
    // --- buttons
    const row=el('div','row'),go=el('button','primary',info.connected?'Save changes':'Save and connect');
    go.onclick=async()=>{go.disabled=true;setNote('Working…','');const values={},options={};for(const k in ins)values[k]=ins[k].value;if(jsonText)values.client_json=jsonText;for(const k in opts)options[k]=opts[k].value;
      const r=await fetch('/api/accounts/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:pl,values,options})});go.disabled=false;
      if(!r.ok){setNote(await r.text(),'bad');return}
      const j=await r.json();
      if(j.auth_url){const w=window.open(j.auth_url,'_blank');
        setNote(w?'Finish signing in in the tab that just opened. This window updates by itself.':'Your browser blocked the new tab.','',w?null:j.auth_url);
        for(let n=0;n<90;n++){await new Promise(z=>setTimeout(z,2000));if(!ov.isConnected)return;await refresh();if(S.platforms[pl].connected&&S.platforms[pl].account){notes[pl]={text:label+' connected'+(S.platforms[pl].account?' as '+S.platforms[pl].account:'')+'.',kind:'good'};break}}}
      else{await refresh();notes[pl]=j.connected?{text:label+' connected as '+j.account+'.',kind:'good'}:{text:'Saved.',kind:'good'}}
      build()};
    row.append(go);
    if(info.connected){const tb=el('button',null,'Test connection');tb.title='Checks the saved login with the platform. Posts nothing.';
      tb.onclick=async()=>{tb.disabled=true;setNote('Checking with '+label+'…','');const r=await fetch('/api/accounts/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({platform:pl})});tb.disabled=false;
        if(r.ok){const j=await r.json();await refresh();notes[pl]={text:'✓ '+j.message,kind:'good'};build()}else setNote(await r.text(),'bad')};row.append(tb)}
    if(info.connected||info.have.length){const dc=el('button',null,'Disconnect');dc.onclick=async()=>{if(!confirm('Remove the saved login for '+label+' from this PC?'))return;await post('/api/accounts/disconnect',{platform:pl});delete notes[pl];await refresh();build()};row.append(dc)}
    sec.append(row,msg);paint();
    // --- when something goes wrong
    const tr=el('details');tr.append(el('summary',null,'Something went wrong?'));
    const ul=el('ul');ul.style.cssText='margin:4px 0 4px 18px;padding:0;font-size:13px';
    for(const t of guide.troubleshooting){const li=el('li');li.append(el('b',null,t.problem+': '),t.fix);ul.append(li)}
    tr.append(ul);sec.append(tr);return sec};
  const build=()=>{list.replaceChildren(...Object.keys(S.platforms).map(card))};
  const cl=el('button',null,'Close');cl.onclick=()=>{ov.remove();sig='';load(true)};box.append(cl);build();document.body.append(ov);
  if(focus){const t=list.querySelector('[data-pl="'+focus+'"]');if(t)t.scrollIntoView()}}
async function openConnect(){
  await loadShare();if(document.querySelector('.modal'))return;const ov=el('div','modal'),box=el('div','mbox');ov.append(box);box.append(el('h2',null,'Connect accounts'),el('div','hint','Only needed for automatic posting. You can always share a clip by hand without connecting anything.'));
  for(const [k,v] of Object.entries(shareInfo.platforms||{})){const ok=(shareInfo.connected||{})[k]||(shareInfo.quick||{})[k],r=el('div','row');r.append(el('b',null,v.label),el('span','chip'+(ok?' ok':''),ok?'✅ Connected':'Not connected'));box.append(r)}
  const go=(label,cls,fn)=>{const b=el('button',cls,label);b.onclick=()=>{ov.remove();fn()};return b};
  const row=el('div','row');row.append(go('Instagram + TikTok (easiest)','primary',openQuick),go('YouTube',null,()=>openAccounts('youtube')));
  const adv=el('details');adv.append(el('summary',null,'Use my own developer apps'),go('Open advanced setup','sm',()=>openAccounts()));
  const cl=el('button',null,'Close');cl.onclick=()=>ov.remove();box.append(row,adv,cl);document.body.append(ov)}
function openQuick(){
  const ov=el('div','modal'),box=el('div','mbox');ov.append(box);box.append(el('h2',null,'Connect Instagram + TikTok'));
  box.append(el('div','hint','One free Zernio account posts to Instagram and TikTok for you: no developer apps, no approval waiting. Its free plan covers your first 2 connected accounts with unlimited posts, so connect only these two there. YouTube is set up separately under Connect accounts. Your key is kept on this PC and only ever sent to Zernio.'));
  const steps=[['Create a free Zernio account (no card needed).',{label:'Open Zernio',url:'https://zernio.com'}],
    ['In Zernio, connect your Instagram (a professional/business account) and your TikTok account. Do not connect more than these two, to stay on the free plan.',{label:'Open Zernio',url:'https://zernio.com'}],
    ['In Zernio, create an API key (in the dashboard) and copy it.',null]];
  const ol=el('ol','steps'),key=i=>'quick-'+i;
  steps.forEach(([text,link],i)=>{const li=el('li'),cb=el('input');cb.type='checkbox';cb.checked=lsGet(key(i))==='1';li.classList.toggle('done',cb.checked);
    cb.onchange=()=>{lsSet(key(i),cb.checked);li.classList.toggle('done',cb.checked)};
    const t=el('div','t');t.append(el('b',null,(i+1)+'. '),text);li.append(cb,t);
    if(link){const a=el('a','btn',link.label+' ↗');a.href=safeUrl(link.url);a.target='_blank';a.rel='noopener';li.append(a)}ol.append(li)});
  const last=el('li'),t4=el('div','t');t4.append(el('b',null,'4. '),'Paste the key here and press the button. This checks it with Zernio and finds your accounts. Nothing is posted.');last.append(t4);ol.append(last);box.append(ol);
  const inp=el('input');inp.type='password';inp.placeholder='Zernio API key';inp.autocomplete='off';inp.style.cssText='padding:8px;border-radius:6px;border:1px solid #8884;background:var(--bg);color:var(--fg)';
  const go=el('button','primary','Save and check'),again=el('button',null,'Check again'),off=el('button',null,'Disconnect'),cl=el('button',null,'Close');
  const row=el('div','row');row.append(inp,go,again,off,cl);box.append(row);
  const tt=el('select');for(const [v,t] of [['public','TikTok: post publicly right away'],['draft','TikTok: send to my TikTok drafts, I press Post there']]){const o=el('option',null,t);o.value=v;tt.append(o)}
  const ttRow=el('div','row');ttRow.append(tt);box.append(ttRow);
  const res=el('div'),note=el('div','note');res.style.cssText='display:flex;flex-direction:column;gap:6px';box.append(res,note);
  const trb=el('details');trb.append(el('summary',null,'Something went wrong?'));const ul=el('ul');ul.style.cssText='margin:4px 0 4px 18px;padding:0;font-size:13px';
  for(const [p,f] of [['Zernio did not accept this key','Copy the key again with its copy button and paste it with nothing before or after it.'],['An account says "not found in Zernio"','Connect it in the Zernio dashboard (step 2), then press Check again.'],['Instagram will not connect in Zernio','The Instagram account must be a professional (business or creator) account linked to a Facebook page.'],['A post fails','The reason is shown in the share result; the same text also appears in the Zernio dashboard under that post.']]){const li=el('li');li.append(el('b',null,p+': '),f);ul.append(li)}
  trb.append(ul);box.append(trb);
  const paint=S=>{res.replaceChildren();tt.value=S.tiktok_mode||'public';
    for(const k of ['instagram','tiktok']){const r=el('div','row'),ok=!!(S.accounts||{})[k];r.append(el('b',null,((shareInfo.platforms||{})[k]||{label:k}).label),el('span','chip'+(ok?' ok':''),ok?'✅ Connected'+(S.accounts[k].name?' · '+S.accounts[k].name:''):S.saved?'Not found in Zernio yet':'Not connected'));res.append(r)}
    if(S.accounts&&S.accounts.youtube)res.append(el('div','mut','YouTube is also connected in Zernio and will be posted through it.'));
    if(S.warning){const w=el('div','note bad');w.textContent=S.warning;res.append(w)}
    go.textContent=S.saved?'Replace key':'Save and check';again.style.display=off.style.display=S.saved?'':'none';ttRow.style.display=S.saved?'':'none'};
  const call=async(u,b,busy)=>{note.className='note';note.textContent=busy;const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
    if(!r.ok){note.className='note bad';note.textContent=await r.text();return null}note.textContent='';const S=await r.json();await loadShare();return S};
  go.onclick=async()=>{go.disabled=true;const S=await call('/api/quick/save',{api_key:inp.value,tiktok_mode:tt.value},'Checking with Zernio…');go.disabled=false;if(S){inp.value='';paint(S);note.className='note good';note.textContent='Connected. Pick "Post automatically" when you share a clip.'}};
  again.onclick=async()=>{again.disabled=true;const S=await call('/api/quick/refresh',{},'Checking with Zernio…');again.disabled=false;if(S){paint(S);note.className='note good';note.textContent='Up to date.'}};
  off.onclick=async()=>{if(!confirm('Remove the saved Zernio key from this PC? (Your accounts stay connected in Zernio.)'))return;await post('/api/quick/disconnect',{});await loadShare();paint({saved:false,accounts:{}})};
  tt.onchange=async()=>{const S=await call('/api/quick/options',{tiktok_mode:tt.value},'Saving…');if(S)paint(S)};
  cl.onclick=()=>{ov.remove();sig='';load(true)};
  document.body.append(ov);fetch('/api/quick').then(r=>r.json()).then(paint)}
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
/*__AP__*/
/*__CP__*/
/*__RF__*/
document.getElementById('s').onchange=()=>{shown=30;render()};
document.getElementById('upd').onclick=()=>{sig='';lastAct=0;load(true)};
{const hd=document.querySelector('header.top');let on=false;window.addEventListener('scroll',()=>{const want=on?scrollY>40:scrollY>90;if(want!==on){on=want;hd.classList.toggle('compact',on)}},{passive:true})}
{const lb=document.getElementById('layoutBtn');
  const best=ch=>clips.filter(c=>c.channel===ch&&c.status!=='done'&&c.file).sort((a,b)=>b.created.localeCompare(a.created))[0];
  const open=ch=>{const c=best(ch);if(c)openLayout(c,{currentTime:c.moment_s||0})};
  lb.onclick=()=>{const chs=[...new Set(clips.filter(c=>c.status!=='done'&&c.file).map(c=>c.channel))].sort();
    if(!chs.length){alert('No clips yet: the layout is set on a clip of that streamer.');return}
    if(chs.length===1){open(chs[0]);return}
    const ov=el('div','modal'),box=el('div','mbox'),row=el('div','row');ov.append(box);box.append(el('h2',null,'Video layout: which streamer?'),row);
    for(const ch of chs){const b=el('button',null,(layouts[ch]?'✓ ':'')+ch);b.onclick=()=>{ov.remove();open(ch)};row.append(b)}
    const cl=el('button',null,'Close');cl.onclick=()=>ov.remove();box.append(cl);document.body.append(ov)}}
{const sb=document.getElementById('setupBtn'),sl=document.getElementById('setupList');sb.onclick=e=>{e.stopPropagation();sl.hidden=!sl.hidden};sl.onclick=()=>{sl.hidden=true};document.addEventListener('click',e=>{if(!sb.contains(e.target))sl.hidden=true})}
document.getElementById('purge').onclick=async()=>{if(confirm('Permanently delete every trashed clip file?')){await post('/api/purge',{});load(true)}};
document.getElementById('folders').onclick=openFolders;document.getElementById('connect').onclick=openConnect;Promise.all([loadLayouts(),loadShare()]).then(()=>{load(true);const m=location.hash.match(/connect=([a-z]*)/);if(m)m[1]?openAccounts(m[1]):openConnect()});(function tick(){setTimeout(async()=>{try{await load(false)}catch(e){}tick()},clips.some(exBusy)?1500:10000)})();
</script>"""

PAGE = PAGE.replace("/*__AP__*/", autopilot_page.JS).replace("/*__CP__*/", campaigns_page.JS).replace("/*__RF__*/", reframe_page.JS)


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Store:
    def __init__(self, root: Path, cfg: Config):
        self.root, self.cfg, self.lock = root.resolve(), cfg, threading.Lock()
        self.port = 8765                              # set by serve(); the sign-in pages come back to this port
        self.jobs: dict[str, dict] = {}               # clip id -> running / failed export (the page paints its button from this)
        self.job_lock = threading.Lock()
        self._export_pool: PriorityPool | None = None

    def _metas(self):
        for p in self.root.rglob("*.clip.json"):
            try:
                yield p, json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

    _SIZES: dict = {}
    _TODO: list = []
    _probe_lock = threading.Lock()
    _prober: threading.Thread | None = None

    def size_of(self, f: Path) -> list[int] | None:
        """[width, height] of a clip file, or None for now. Never blocks: clips made by this version store it in their record; older
        ones are probed one by one in a background thread and show up on a later refresh (hundreds of clips must not stall the page)."""
        try:
            key = (str(f), f.stat().st_mtime_ns)
        except OSError:
            return None
        if key in self._SIZES:
            return self._SIZES[key]
        with self._probe_lock:
            if key not in self._TODO:
                self._TODO.append(key)
            if Store._prober is None or not Store._prober.is_alive():
                Store._prober = threading.Thread(target=self._probe_loop, daemon=True)
                Store._prober.start()
        return None

    def _probe_loop(self) -> None:
        while True:
            with self._probe_lock:
                if not self._TODO:
                    return
                key = self._TODO.pop(0)
            try:
                sz = probe_size(Path(key[0]))
            except Exception:
                sz = None
            self._SIZES[key] = list(sz) if sz else None

    def list(self) -> list[dict]:
        out = []
        ap = autopilot.load()
        ready = taste.readiness(taste.model(), ap["min_precision"], ap["min_confidence"])
        camps = campaigns.load()                      # paid campaigns: each card shows what would block Post
        rows, pp = (campaigns.posts() if camps else []), None
        prov = next((share.PROVIDERS[n] for n in ("auto", "direct") if n in share.PROVIDERS and share.PROVIDERS[n].configured()), None)
        if camps and prov:
            pp = prov.problems
        for p, m in self._metas():
            m["dir"] = p.parent.relative_to(self.root).as_posix()
            m["res"] = m.get("res") or (self.size_of(p.parent / m["file"]) if m.get("file") else None)
            m["export_job"] = self.job_view(m.get("id"))
            m["taste"] = self._taste_view(m, ready, ap)
            if not m.get("description"):
                m["description"] = self._fresh_description(p, m)
            m["campaign"] = campaigns.card_view(m, pp, camps=camps, rows=rows) if camps else None
            out.append(m)
        return out

    def campaigns_view(self) -> dict:
        """Everything the Campaigns window shows."""
        camps, problems = campaigns.load_all()
        rows = campaigns.posts()
        return {"text": campaigns.raw_text(), "example": json.dumps(campaigns.EXAMPLE, indent=2), "path": str(campaigns.path()),
                "exists": campaigns.path().is_file(),
                "campaigns": camps, "problems": problems, "stats": {c["id"]: campaigns.stats(c, rows) for c in camps},
                "live": {c["id"]: campaigns.live_reason(c, None, rows) for c in camps}, "posts": rows, "to_submit": campaigns.to_submit_groups()}

    def update_campaign_post(self, pid: str, body: dict) -> dict:
        """Submitted / approved / rejected, the post link and its views. A rejection is saved as a bad example of your style,
        with the campaign's reason, so the learner and the teacher skill can see what not to do."""
        before = next((r for r in campaigns.posts() if r["id"] == pid), None)
        row = campaigns.update_post(pid, status=body.get("status"), reason=body.get("reason"), url=body.get("url"), views=body.get("views"))
        if row["status"] == "rejected" and (before or {}).get("status") != "rejected":
            p, m = self.find(row["clip"])
            if not p:                                         # its record is gone: keep the lesson with what the post log knows
                m = {"id": row["clip"], "channel": str(row["clip"]).split("/")[0], "description": {"title": row.get("title", "")}}
            style.record(m, "campaign_reject", "bad", "you", p.parent if p else None, note=f"{row['campaign']}: {row['reason']}")
        return row

    @staticmethod
    def _taste_view(m: dict, ready: dict, ap: dict | None = None) -> dict | None:
        """What the learned taste thinks of this clip: chance you would keep it, why, and whether autopilot would act on it."""
        pred = taste.predict(m)
        if not pred:
            return None
        sc = autopilot.clip_score(m)
        enough = sc is not None and sc >= (ap or {}).get("min_score", 0)                  # it never posts a clip under your score floor
        return {**pred, "pick": bool(ready["ok"] and pred["p"] >= ready["threshold"] and enough)}

    @staticmethod
    def style_rules(m: dict) -> dict | None:
        """Your learned style for this clip's streamer, unless you switched "use my style" off in the Autopilot window."""
        try:
            return style.load(m.get("channel")) if autopilot.load()["use_style"] else None
        except Exception:
            return None

    def _fresh_description(self, p: Path, m: dict) -> dict:
        words = cached_words(p.parent / m["file"]) if m.get("file") else []
        d = suggest_description(m, self.cfg, words or None, rules=self.style_rules(m))
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
            fresh = suggest_description(m, self.cfg, words, (lo, hi), seed=int(old.get("seed", -1)) + 1, seen=old.get("seen"),
                                        rules=self.style_rules(m))
            body_edited = bool(old.get("body")) and old.get("body") != old.get("auto_body")      # no record of an automatic body = treat as yours
            d = {**old, **fresh, "auto_body": fresh["body"], "title_src": "auto", "body_edited": body_edited}
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
        """'done' = posted, and you want the disk space back: the draft, exports and words file are deleted (not the copies in the
        final clips folder), the .clip.json stays, and it counts as a Keep. Only for a clip that was shared."""
        if status not in ("new", "keep", "trash", "done"):
            return False
        with self.lock:
            p, m = self.find(cid)
            if not p:
                return False
            if status == "done":
                if not any(sh.get("status") in ("done", "draft") for sh in (m.get("shares") or {}).values()):
                    return False                                  # "opened" uploaded nothing: this may be the only copy
                if (self.jobs.get(cid) or {}).get("state") in ("queued", "encoding"):
                    return False                                  # an export is still writing this clip's files
                names = [m.get("file")] + [e.get("file") for e in m.get("exports") or []] + [(m.get("voiceover") or {}).get("file")]
                failed = False
                for name in names + [(m.get("file") or "").replace(".mp4", x) for x in (".words.json", ".signals.json")]:
                    if name and Path(name).name == name:          # only plain file names next to the .clip.json
                        try:
                            (p.parent / name).unlink(missing_ok=True)
                        except OSError as e:                      # e.g. still playing in the page: try again in a moment
                            print(f"could not delete {p.parent / name}: {e}")
                            failed = True
                if failed:                                        # status stays, so Done can be pressed again; forget exports already gone
                    m["exports"] = [e for e in m.get("exports") or [] if (p.parent / str(e.get("file"))).is_file()]
                    _write_json(p, m)
                    return False
                m["files_deleted"] = True
            elif m.get("status") == "done":
                return False                                      # its files are gone: it cannot go back to review
            if (m.get("autopilot") or {}).get("state") == "trashed" and status != "trash":
                m["autopilot"]["state"] = "restored"              # you overruled the Autopilot's trash: yours from now on
            m["status"] = status
            _write_json(p, m)
        if status != "done" or taste.posted_by_you(m):                              # Done on an Autopilot-only post is no judgement of yours
            taste.record(m, {"keep": 1, "trash": 0, "done": 1}.get(status), status)           # kept in your settings folder: survives "Delete trashed files"
        taste.retrain_soon()
        verdict, fail = style.verdict_for(status, m)
        style.record(m, "undo" if status == "new" else status, verdict, "you", p.parent, rule_fail=fail)
        return True

    def set_description(self, cid: str, title: str, body: str) -> bool:
        if len(title) > 300 or len(body) > 5000:
            return False
        with self.lock:
            p, m = self.find(cid)
            if not p:
                return False
            d = m.get("description") or self._fresh_description(p, m)
            if title != d.get("title"):                     # who wrote the title: one of the suggestions you picked, or typed by you
                d["title_src"] = "picked" if title in (d.get("alts") or []) + (d.get("seen") or []) else "typed"
            if body != d.get("body"):
                d["body_edited"] = body != d.get("auto_body", d.get("body"))
            changed = (title, body) != (d.get("title"), d.get("body"))
            d["title"], d["body"] = title, body
            m["description"] = d
            _write_json(p, m)
        if changed:
            style.record(m, "describe", "none", "you", p.parent)
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

    def export_layout(self, m: dict, vertical: bool) -> tuple[dict | None, dict]:
        """(layout, campaign plan) exactly as an export of this clip would use them."""
        layout = LayoutStore(self.root).get(m["channel"]) if vertical else None
        cp = campaigns.plan(m, layout, vertical, self.cfg)
        return cp["layout"], cp

    def _reframe(self, src: Path, m: dict, start: float, end: float, layout: dict | None, cp: dict, offset: float = 0.0) -> dict | None:
        if not m.get("res"):                                       # an older clip: measure it now
            m = {**m, "res": list(probe_size(src) or []) or None}
        return reframe.make(src, start, end, layout or {"mode": self.cfg.vertical_mode}, m, (cp.get("record") or {}).get("must_show"),
                            m.get("reframe"), render_fps(self.cfg, src), ffmpeg_exe(), offset, getattr(self.cfg, "sharpen", 0.0))

    def set_reframe(self, cid: str, body: dict) -> dict:
        """The card's reframe switches (and Shuffle: new random choices for this clip)."""
        with self.lock:
            p, m = self.find(cid)
            if not p:
                raise ValueError("clip not found")
            cur = reframe.options(m.get("reframe"))
            cur.update({k: bool(body[k]) for k in reframe.DEFAULTS if k in body})
            if body.get("shuffle"):
                cur["seed"] = os.urandom(4).hex()
            m["reframe"] = cur
            _write_json(p, m)
            return cur

    def quality_view(self, cid: str, start: float, end: float, vertical: bool) -> dict:
        """The card's quality report and the moment shown in the before / after stills (the chat moment, inside the trim)."""
        p, m = self.find(cid)
        if not p:
            raise ValueError("clip not found")
        self._check_trim(m, start, end)
        layout, _ = self.export_layout(m, vertical)
        rep = quality.report(layout or {"mode": self.cfg.vertical_mode}, m.get("res") or list(probe_size(p.parent / m["file"]) or []))
        if not vertical:                                           # the full frame at its own size: nothing is enlarged
            rep.update(panels={}, zoom_room={})
        t = min(max(float(m.get("moment_s") or (start + end) / 2), start), max(start, end - 0.1))
        rf = self._reframe(p.parent / m["file"], m, start, end, layout, self.export_layout(m, vertical)[1]) if vertical else None
        if rf:
            zs = rf["plan"]["zooms"]
            if zs:                                                 # show the strongest zoom at its fullest
                z = max(zs, key=lambda z: z["amp"])
                t = start + (z["t1"] + z["t2"]) / 2
            rep["plan"] = reframe.summary(rf["plan"])
            rep["warnings"] += rf["plan"]["notes"]
        return {"t": round(t, 2), "report": rep, "options": reframe.options(m.get("reframe"))}

    def after_frame(self, cid: str, t: float, start: float, end: float, vertical: bool) -> bytes:
        """One frame of the export at time t, rendered through the same filter graph as the export itself."""
        src, m = self.draft_path(cid)
        self._check_trim(m, start, end)
        layout, cp = self.export_layout(m, vertical)
        rf = self._reframe(src, m, start, end, layout, cp, offset=t - start) if vertical else None
        graph = ((rf or {}).get("graph") or lay.chain(layout or {"mode": self.cfg.vertical_mode}, getattr(self.cfg, "sharpen", 0.0))
                 if vertical else None)
        img = grab_frame(src, t, graph)
        if not img:
            raise RuntimeError("could not render the after frame")
        return img

    def autodetect(self, cid: str, split: float) -> dict:
        src, m = self.draft_path(cid)
        rect = detect_facecam(src, float(m["duration_s"]), split)
        if not rect:
            raise ValueError("no face found consistently in this clip. Draw the webcam box by hand.")
        return rect

    @staticmethod
    def _check_trim(m: dict, start: float, end: float) -> None:
        if not (0 <= start < end <= m["duration_s"] + 1) or end - start > 600:
            raise ValueError(f"bad trim {start}-{end} (draft is {m['duration_s']}s)")

    def job_view(self, cid: str | None) -> dict | None:
        with self.job_lock:
            j = self.jobs.get(cid)
            return {"state": j["state"], "message": j["message"], "since": j["since"]} if j else None

    def start_export(self, cid: str, start: float, end: float, vertical: bool, captions: bool, hook: str | None = None,
                     voiceover: bool = False, hook_style: str = "", hook_src: str = "") -> dict:
        """Queue an export and return at once. The clip stays 'queued' / 'encoding' on the SERVER until the file really exists and is
        recorded, so the page shows the right button state after any redraw, reload or second tab. Bad requests fail here, not later."""
        p, m = self.find(cid)
        if not p:
            raise ValueError("clip not found")
        self._check_trim(m, start, end)
        with self.job_lock:
            cur = self.jobs.get(cid)
            if cur and cur["state"] in ("queued", "encoding"):
                raise ValueError("this clip is already being exported: wait for it to finish")
            job = {"state": "queued", "message": "", "since": time.time()}
            self.jobs[cid] = job
            if self._export_pool is None:
                self._export_pool = PriorityPool(max(1, self.cfg.cut_workers), "export")
            self._export_pool.submit(0, self._run_export, cid, job, start, end, vertical, captions, hook, voiceover, hook_style, hook_src)
        return {"state": "queued"}

    def _run_export(self, cid: str, job: dict, start: float, end: float, vertical: bool, captions: bool, hook: str | None = None,
                    voiceover: bool = False, hook_style: str = "", hook_src: str = "") -> None:
        with self.job_lock:
            job["state"] = "encoding"
        try:
            extra = {k: v for k, v in (("hook", hook), ("voiceover", voiceover), ("hook_style", hook_style), ("hook_src", hook_src)) if v}
            self.export(cid, start, end, vertical, captions, **extra)   # records the export in the clip's file before returning
        except Exception as e:
            msg = str(e) if isinstance(e, (ValueError, CaptionsUnavailable, RuntimeError)) else f"export failed: {e}"
            with self.job_lock:
                job.update(state="error", message=msg)                # stays visible until dismissed or the next try
            return
        with self.job_lock:
            if self.jobs.get(cid) is job:
                del self.jobs[cid]                                    # only now does the button go back to normal

    def dismiss_export(self, cid: str) -> bool:
        with self.job_lock:
            if (self.jobs.get(cid) or {}).get("state") == "error":
                del self.jobs[cid]
        return True

    VO_EXT = ("webm", "ogg", "mp3", "wav", "m4a")
    VO_MAX = 25 * 1024 * 1024

    def save_voiceover(self, cid: str, data: str, ext: str) -> dict:
        """Your voice-over for a clip (recorded in the page or picked from a file), saved next to the clip under a name we choose."""
        ext = str(ext or "").lower().lstrip(".")
        if ext not in self.VO_EXT:
            raise ValueError("voice-over must be a " + ", ".join(self.VO_EXT) + " file")
        try:
            raw = base64.b64decode(str(data or ""), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("the voice-over did not arrive in one piece: try again") from None
        if not raw or len(raw) > self.VO_MAX:
            raise ValueError("the voice-over must be between 1 byte and 25 MB")
        with self.lock:
            p, m = self.find(cid)
            if not p or not m.get("file"):
                raise ValueError("clip not found")
            name = f"{Path(m['file']).stem}_vo.{ext}"
            (p.parent / name).write_bytes(raw)
            old = (m.get("voiceover") or {}).get("file")
            if old and old != name and Path(old).name == old:
                (p.parent / old).unlink(missing_ok=True)
            m["voiceover"] = {"file": name, "size": len(raw), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            _write_json(p, m)
            return m["voiceover"]

    def delete_voiceover(self, cid: str) -> bool:
        with self.lock:
            p, m = self.find(cid)
            if not p:
                return False
            old = (m.get("voiceover") or {}).get("file")
            if old and Path(old).name == old:
                try:
                    (p.parent / old).unlink(missing_ok=True)
                except OSError:                               # still in use (an export is reading it): try again later
                    return False
            m.pop("voiceover", None)
            _write_json(p, m)
        return True

    def suggest_hook(self, cid: str, start: float, end: float, avoid: list[str] | None = None) -> dict | None:
        """A hook for this clip from its transcript (only words already heard: no speech-to-text run just for this)."""
        p, m = self.find(cid)
        if not p:
            raise ValueError("clip not found")
        words = words_in_window(cached_words(p.parent / m["file"]), start, end)
        lang = hooks.language(words, getattr(self.cfg, "whisper_language", "") or None)
        rules = style.load(m.get("channel"))
        return hooks.suggest(words, lang, avoid, seed=cid, extra=(rules.get("body") or {}).get("hooks") or [])

    def export(self, cid: str, start: float, end: float, vertical: bool, captions: bool, by: str = "you",
               hook: str | None = None, voiceover: bool = False, hook_style: str = "", hook_src: str = "") -> str:
        """Precise trim (+ optional 9:16, + optional burned captions) in ONE encode. Returns the new file name.
        `by` = "you" or "autopilot": only your own exports teach your style."""
        p, m = self.find(cid)
        if not p:
            raise ValueError("clip not found")
        src = p.parent / m["file"]
        self._check_trim(m, start, end)
        vo = None
        if voiceover:
            v = str((m.get("voiceover") or {}).get("file") or "")
            if not v or Path(v).name != v or not (p.parent / v).is_file():
                raise ValueError("record or pick a voice-over first")
            vo = p.parent / v
        hook_src = hook_src if hook_src in ("groq", "template", "yours") else "you"   # a suggestion you kept on the card
        auto = hook == "auto"
        hook = None if auto else (hooks.clean(hook) or None)
        if hook and hooks.problem(hook):                           # typed hooks obey the same rules (re-using one is fine)
            raise ValueError(f"hook \"{hook}\": {hooks.problem(hook)}. Shorten or change it, or tick No hook.")
        hook_style = hook_style if hook_style in hooks.STYLE_NAMES else hooks.next_style()
        layout = LayoutStore(self.root).get(m["channel"]) if vertical else None
        cp = campaigns.plan(m, layout, vertical, self.cfg)          # a paid campaign: keep must_show in, burn the disclosure
        layout = cp["layout"]
        rf = self._reframe(src, m, start, end, layout, cp) if vertical else None
        plan = rf["plan"] if rf else None
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
            ass = reframe.move_captions(ass, plan)                 # off the face while the webcam is big
        if auto:                                                   # made for you after the captions, so it can use their words
            got = self.suggest_hook(cid, start, end)
            hook, hook_src = (got["text"], got["source"]) if got else (None, "")
        burned: dict = {}
        ass = campaigns.burn(cp, ass, vertical, self.cfg, end - start, m, hook=hook, hook_style=hook_style,
                             screen=reframe.hook_screen(plan, captions) if vertical else None, info=burned)
        if hook and not burned.get("hook", True):
            hook = None                                            # no room off the facecam / captions / disclosure: not burned
        name = (f"{src.stem}_{int(start * 10)}-{int(end * 10)}{'_v' if vertical else ''}{'_cc' if captions else ''}"
                f"{'_ad' if cp['record'] else ''}{'_rf' if reframe.applied(plan) else ''}{'_hk' if hook else ''}{'_vo' if vo else ''}.mp4")
        errs: list[str] = []
        placed = export_clip(src, p.parent / name, start, end, self.cfg, vertical, ass, layout, errs, voiceover=vo,
                             motion=rf["graph"] if rf else None)
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
                 "layout": (layout or {}).get("mode", self.cfg.vertical_mode) if vertical else None, "by": by,
                 **({"campaign": cp["record"]} if cp["record"] else {}),
                 **({"originality": {"voiceover": bool(vo), "hook": bool(hook)}} if vo or hook else {}),
                 **({"hook": {"text": hook, "style": hook_style, "source": hook_src}} if hook else {}),
                 **({"reframe": reframe.summary(plan)} if vertical else {})}]
            _write_json(p, m)
        if hook:
            hooks.remember(hook, hook_style, hook_src)
        if by == "you":
            style.record(m, "export", "none", "you", p.parent)
        return name

    def save_account(self, platform: str, values: dict, options: dict) -> dict:
        """Store what you typed in the Accounts window. Instagram is checked right away; YouTube / TikTok return a sign-in URL."""
        if platform not in accounts.FIELDS:
            raise ValueError("unknown platform")
        clean = accounts.clean_credentials(platform, values)      # tidies pasted text, reads a downloaded Google JSON, explains mix-ups
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
                    names = [m.get("file")] + [e["file"] for e in m.get("exports", [])] + [(m.get("voiceover") or {}).get("file")]
                    for name in names + [m.get("file", "").replace(".mp4", x) for x in (".words.json", ".signals.json")]:
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

        def _from_this_page(self) -> bool:
            """Only this page may talk to the server: blocks DNS rebinding (Host) and other sites or local ports (Origin)."""
            port = self.server.server_address[1]
            own = {f"{h}:{port}" if port != 80 else h for h in ("127.0.0.1", "localhost")}     # browsers drop ":80"
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") or "").lower() in own and (not origin or origin.lower() in {f"http://{h}" for h in own})

        def do_GET(self):
            if not self._from_this_page():
                return self._send(403, b"forbidden", "text/plain")
            path = unquote(urlparse(self.path).path)
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/clips":
                return self._send(200, json.dumps(store.list()).encode())
            if path == "/api/settings":
                return self._send(200, json.dumps({"out_dir": str(store.root), "export_dir": store.cfg.export_dir,
                                                   "settings_file": str(settings.path()), "groq": bool(hooks.groq_key())}).encode())
            if path == "/api/share/providers":
                return self._send(200, json.dumps({"providers": share.providers_info(), "platforms": share.PLATFORMS,
                                                   "connected": {p: accounts.connected(p) for p in share.PLATFORMS},
                                                   "quick": {p: zernio.covers(p) for p in share.PLATFORMS}}).encode())
            if path == "/api/quick":
                return self._send(200, json.dumps(zernio.summary()).encode())
            if path == "/api/autopilot":
                return self._send(200, json.dumps(autopilot.status(store)).encode())
            if path == "/api/campaigns":
                return self._send(200, json.dumps(store.campaigns_view(), ensure_ascii=False).encode())
            if path == "/api/campaigns/todo":
                return self._send(200, json.dumps(campaigns.to_submit_groups(), ensure_ascii=False).encode())
            if path == "/api/accounts":
                return self._send(200, json.dumps({"platforms": accounts.status(), "redirect_uri": accounts.redirect_uri(store.port),
                                                   "setup": accounts.setup_guide(store.port)}).encode())
            if path == "/oauth/callback":
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                try:
                    if q.get("error"):
                        raise accounts.ApiError(accounts.explain_oauth_error(accounts.cancel_oauth(q.get("state", "")), q["error"],
                                                                             q.get("error_description", "")))
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
            if path in ("/api/quality", "/api/after"):
                q = parse_qs(urlparse(self.path).query)
                try:
                    cid, start, end = q["id"][0], float(q["start"][0]), float(q["end"][0])
                    vertical = q.get("vertical", ["1"])[0] == "1"
                    if path == "/api/quality":
                        return self._send(200, json.dumps(store.quality_view(cid, start, end, vertical)).encode())
                    return self._send(200, store.after_frame(cid, float(q["t"][0]), start, end, vertical), "image/jpeg",
                                      {"Cache-Control": "no-store"})
                except (ValueError, KeyError, LayoutError) as e:
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
            if not self._from_this_page():
                return self._send(403, b"forbidden", "text/plain")     # block other websites from poking the API
            try:
                size = int(self.headers.get("Content-Length", 0))
                if size < 0:                                           # read(-1) would wait forever
                    raise ValueError
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, OverflowError, RecursionError):
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
            elif path == "/api/export/start":
                try:
                    return self._send(200, json.dumps(store.start_export(str(body.get("id")), float(body["start"]), float(body["end"]),
                                                                         bool(body.get("vertical")), bool(body.get("captions")),
                                                                         str(body.get("hook") or ""), bool(body.get("voiceover")),
                                                                         str(body.get("hook_style") or ""),
                                                                         str(body.get("hook_src") or ""))).encode())
                except (ValueError, KeyError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/export/dismiss":
                ok = store.dismiss_export(str(body.get("id")))
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
            elif path == "/api/share/check":
                try:
                    return self._send(200, json.dumps(share.precheck(store, str(body.get("id")), str(body.get("file")),
                                                                  list(body.get("platforms") or [])), ensure_ascii=False).encode())
                except (share.ShareError, TypeError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/accounts/save":
                try:
                    return self._send(200, json.dumps(store.save_account(str(body.get("platform")), dict(body.get("values") or {}),
                                                                         dict(body.get("options") or {}))).encode())
                except (ValueError, KeyError, TypeError, accounts.ApiError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/accounts/test":
                try:
                    return self._send(200, json.dumps(accounts.verify(str(body.get("platform")))).encode())
                except (accounts.ApiError, KeyError, TypeError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path in ("/api/quick/save", "/api/quick/refresh", "/api/quick/options"):
                try:
                    if path == "/api/quick/save":
                        out = zernio.connect(str(body.get("api_key", "")), body.get("tiktok_mode"))
                    elif path == "/api/quick/refresh":
                        out = zernio.refresh()
                    else:
                        out = zernio.set_options(str(body.get("tiktok_mode")))
                    return self._send(200, json.dumps(out).encode())
                except accounts.ApiError as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/quick/disconnect":
                zernio.disconnect()
                ok = True
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
            elif path == "/api/autopilot/settings":
                try:
                    autopilot.save({k: v for k, v in body.items() if k != "paused_reason"})
                    ok = True
                except (ValueError, TypeError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/autopilot/train":
                try:
                    taste.train()
                    ok = True
                except Exception as e:
                    return self._send(500, f"could not learn: {e}".encode(), "text/plain")
            elif path == "/api/autopilot/hold":
                ok = autopilot.Autopilot(store).hold(str(body.get("id")))
            elif path == "/api/hook/suggest":
                try:
                    got = store.suggest_hook(str(body.get("id")), float(body["start"]), float(body["end"]),
                                             [str(x) for x in body.get("avoid") or []][:50])
                    return self._send(200, json.dumps(got or {"text": "", "source": "none"}, ensure_ascii=False).encode())
                except (ValueError, KeyError, TypeError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/reframe":
                try:
                    return self._send(200, json.dumps(store.set_reframe(str(body.get("id")), body)).encode())
                except ValueError as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/hook/key":
                k = "".join(str(body.get("key") or "").split())
                if k and not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", k):
                    return self._send(400, b"that does not look like a Groq key (it starts with gsk_)", "text/plain")
                if k:
                    accounts.update("groq", api_key=k)
                else:
                    accounts.clear("groq")
                ok = True
            elif path == "/api/voiceover":
                try:
                    return self._send(200, json.dumps(store.save_voiceover(str(body.get("id")), body.get("data"), body.get("ext"))).encode())
                except (ValueError, OSError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/voiceover/delete":
                ok = store.delete_voiceover(str(body.get("id")))
            elif path == "/api/campaigns/save":
                try:
                    campaigns.save_text(str(body.get("text", "")))
                    ok = True
                except (campaigns.CampaignError, OSError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/campaigns/post":
                try:
                    return self._send(200, json.dumps(store.update_campaign_post(str(body.get("id")), body), ensure_ascii=False).encode())
                except (campaigns.CampaignError, ValueError, TypeError, OSError) as e:
                    return self._send(400, str(e).encode(), "text/plain")
            elif path == "/api/campaigns/views":
                try:
                    return self._send(200, json.dumps(campaigns.refresh_views()).encode())
                except Exception as e:
                    return self._send(200, json.dumps({"updated": 0, "note": f"Could not refresh the views: {e}"}).encode())
            elif path == "/api/purge":
                store.purge()
                ok = True
            self._send(200 if ok else 400, b"{}" if ok else b"failed", "application/json" if ok else "text/plain")

    return H


def serve(root: Path, cfg: Config, port: int = 8765, open_browser: bool = True, open_path: str = ""):
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root, cfg)
    store.port = port
    try:
        taste.import_existing(root)                            # Keep / Trash pressed before learning existed
        if taste.is_stale():
            taste.retrain_soon()
    except Exception as e:
        print(f"learning could not start: {e}")
    srv = QuietServer(("127.0.0.1", port), make_handler(store))
    url = f"http://127.0.0.1:{port}/"
    print(f"Review page: {url}   (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url + open_path)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
