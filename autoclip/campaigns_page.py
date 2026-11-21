"""The Campaigns window + the per-clip campaign badge of the review page (JavaScript). Kept apart from review.PAGE (an ordinary
Python string that cannot contain backslashes); spliced into the page where PAGE has the /*__CP__*/ marker."""

JS = r"""
const CP_ST={to_submit:'To submit',submitted:'Submitted',approved:'Approved',rejected:'Rejected'};
const cpLabel=k=>(shareInfo.platforms[k]||{label:k}).label;
const money=x=>'$'+(Math.round((x||0)*100)/100).toFixed(2);
function cpLeft(s){if(s<=0)return 'deadline passed';return Math.ceil(s/60)+' min left'}
function cpBadge(c){
  const k=c.campaign,ex=(c.exports||[]).slice(-1)[0]||{},o=ex.originality||{},tags=[];
  if(o.voiceover)tags.push('🎙 Your voice-over');if(o.hook)tags.push('🪝 Hook text');if(ex.reframe&&ex.reframe.applied)tags.push('🎬 Reframed');
  if(o.hook&&((ex.reframe&&ex.reframe.applied)||o.voiceover))tags.unshift('✨ Original');
  if(!k&&!tags.length)return null;
  const box=el('div');box.style.cssText='display:flex;flex-direction:column;gap:4px';
  const r=el('div','row');
  if(k){const ch=el('span','chip'+(k.live?' ok':''),'💰 '+k.name+(k.live?'':' · not live: '+k.why));ch.title='Paid campaign: its rules are checked before every post';r.append(ch)}
  for(const t of tags)r.append(el('span','chip',t));
  box.append(r);
  if(!k)return box;
  if(k.live){
    if(!k.ready)box.append(el('div','note warn','Export the 9:16 clip again for this campaign: campaign exports burn in the disclosure'+(k.rules.must_show?' and keep the '+k.rules.must_show+' visible':'')+'.'));
    for(const w of k.warn||[])box.append(el('div','note warn','⚠ '+w));
    const bad=Object.entries(k.problems||{}).filter(([p,e])=>e.length);
    if(bad.length){for(const [p,e] of bad)box.append(el('div','note bad','Post to '+cpLabel(p)+' is blocked: '+e.join('; ')))}
    else box.append(el('div','note good','✓ Passes the campaign check for '+k.platforms.map(cpLabel).join(', ')));
  }
  for(const [p,f] of Object.entries(k.flags_missing||{}))box.append(el('div','note warn','Paid-promotion flag on '+cpLabel(p)+' '+f));
  for(const x of k.posts||[])box.append(el('div','mut',cpLabel(x.platform)+' post: '+CP_ST[x.status]+(x.reason?' ('+x.reason+')':'')));
  return box}

function cpExportExtras(c,t){
  const box=el('details','fine');box.open=!!openSt.get(c.id+'og');
  const vo=c.voiceover;box.append(el('summary',null,'Make it original: hook + voice-over'+(vo?' 🎙':'')));
  const hrow=el('div','row'),hk=el('input');hk.type='text';hk.maxLength=120;hk.placeholder='Hook (7 words max). Empty = made for you';hk.value=t.hook||'';hk.oninput=()=>{t.hook=hk.value;t.hookSrc='you';hn.textContent=''};hk.style.cssText='flex:1;min-width:180px';
  const hn=el('span','mut',t.hookSrc&&t.hookSrc!=='you'?'('+t.hookSrc+')':'');
  const rg=el('button','sm','↻ Regenerate');rg.title='Another hook from what the streamer said (Groq) or from the templates';
  const sugg=async()=>{rg.disabled=true;rg.textContent='…';const r=await rpost('/api/hook/suggest',{id:c.id,start:t.start,end:t.end,avoid:t.hookSeen||[]});rg.disabled=!!t.hookOff;rg.textContent='↻ Regenerate';
    if(!r.ok){alert(await r.text());return}const d=await r.json();if(!d.text){hn.textContent='No new hook for this language: type one';return}
    t.hookSeen=(t.hookSeen||[]).concat([d.text]);t.hook=d.text;t.hookSrc=d.source;hk.value=d.text;hn.textContent='('+d.source+')'};
  rg.onclick=sugg;
  const sty=el('select');sty.title='Hook look. They take turns unless you pick one';for(const [v,l] of [['','Style: next in turn'],['pop','White'],['yellow','Yellow'],['red','Red outline'],['ink','Dark on white']]){const o=el('option',null,l);o.value=v;sty.append(o)}
  sty.value=t.hookStyle||'';sty.onchange=()=>{t.hookStyle=sty.value};
  const off=el('label'),ocb=el('input');ocb.type='checkbox';ocb.checked=!!t.hookOff;off.append(ocb,'No hook');
  const dis=()=>{hk.disabled=rg.disabled=sty.disabled=!!t.hookOff};ocb.onchange=()=>{t.hookOff=ocb.checked;dis()};dis();
  const gk=el('button','sm','Groq key…');gk.title='Optional: a free key from console.groq.com writes hooks from what the streamer said';
  gk.onclick=async()=>{const k=prompt('Paste your free Groq API key (console.groq.com, API Keys). It stays on this PC. Leave empty to remove it.');if(k===null)return;
    const r=await rpost('/api/hook/key',{key:k});alert(r.ok?(k.trim()?'Saved: new hooks are written by Groq.':'Removed: hooks come from the templates.'):await r.text())};
  hrow.append(hk,rg,off,hn);
  const hm=el('details'),hmr=el('div','row');hm.open=!!openSt.get(c.id+'hm');hm.ontoggle=()=>openSt.set(c.id+'hm',hm.open);hmr.append(sty,gk);hm.append(el('summary',null,'Hook color and Groq key'),hmr);
  box.ontoggle=()=>{openSt.set(c.id+'og',box.open);if(box.open&&!t.hook&&!t.hookOff)sugg()};
  const row=el('div','row'),st=el('span','mut',vo?'Voice-over saved ('+Math.round(vo.size/1024)+' KB)':'No voice-over yet');
  const use=el('label'),cb=el('input');cb.type='checkbox';cb.checked=!!(t.vo&&vo);cb.disabled=!vo;cb.onchange=()=>{t.vo=cb.checked};use.append(cb,'Use my voice-over (the stream sound goes quieter while I talk)');
  const up=async(blob,ext)=>{if(blob.size>25*1024*1024){alert('The voice-over must be 25 MB or less.');return}st.textContent='Saving…';
    const b64=await new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(String(fr.result).split(',')[1]||'');fr.readAsDataURL(blob)});
    if(await post('/api/voiceover',{id:c.id,data:b64,ext})){t.vo=true;await load(true)}else st.textContent='Not saved'};
  const pick=el('input');pick.type='file';pick.accept='.webm,.ogg,.mp3,.wav,.m4a,audio/*';pick.hidden=true;
  pick.onchange=()=>{const f=pick.files[0];if(f)up(f,(f.name.split('.').pop()||'').toLowerCase())};
  const pb=el('button','sm','Pick a file…');pb.onclick=()=>pick.click();
  let rec=null;const rb=el('button','sm','🎙 Record');
  rb.onclick=async()=>{if(rec){rec.stop();return}
    let s;try{s=await navigator.mediaDevices.getUserMedia({audio:true})}catch(e){alert('No microphone access: '+e.message);return}
    const chunks=[];rec=new MediaRecorder(s);rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
    rec.onstop=()=>{s.getTracks().forEach(x=>x.stop());rec=null;rb.textContent='🎙 Record';const type=(chunks[0]&&chunks[0].type)||'audio/webm';
      up(new Blob(chunks,{type}),type.includes('ogg')?'ogg':type.includes('mp4')?'m4a':'webm')};
    rec.start();rb.textContent='■ Stop recording';st.textContent='Recording… talk over the clip, then press Stop'};
  const del=el('button','sm','Remove');del.hidden=!vo;del.onclick=async()=>{if(await post('/api/voiceover/delete',{id:c.id})){t.vo=false;load(true)}};
  row.append(rb,pb,pick,del,st);box.append(hrow,hm,row,...(vo?[use]:[]),el('div','hint','Both go into your next export.'));
  return box}

async function openCampaigns(){
  const ov=el('div','modal'),box=el('div','mbox');ov.append(box);
  box.append(el('h2',null,'💰 Campaigns'));
  box.append(el('div','hint','Paid clipping campaigns. While one is live, its clips get the required disclosure and hashtags, and posting stays blocked until its rules pass. Submit each post link on the campaign page within 30 minutes.'));
  const body=el('div');body.style.cssText='display:flex;flex-direction:column;gap:12px';box.append(body);
  const close=el('button',null,'Close');box.append(close);
  document.body.append(ov);
  let S=null,editing=null,flash=null;
  const drafts=new Map();                      // links you typed but did not save yet: post id -> {text, base: the saved link when typed}
  let advOpen=false;                           // the Advanced fold stays as you left it across redraws
  let form=null;                               // null = the lists; -1 = a new campaign; n = editing entry n of campaigns.json
  const openPosts=new Set();                   // campaigns whose post list you opened: survives the background redraw
  const iv=setInterval(()=>{if(!ov.isConnected){clearInterval(iv);return}const ae=document.activeElement;if(form===null&&!(body.contains(ae)&&ae.tagName!=='SUMMARY'))refresh()},15000);
  close.onclick=()=>{clearInterval(iv);ov.remove();sig='';load(true);cpPoll()};
  let lane=Promise.resolve(),waiting=null;     // refreshes and link saves run one at a time, so an old reply can never land after a save
  const inLane=f=>{const p=lane.then(f);lane=p.catch(()=>{});return p};
  function refresh(){if(waiting)return waiting;return waiting=inLane(async()=>{waiting=null;S=await (await fetch('/api/campaigns')).json();if(form===null)paint()})}     // never redraw an open form
  const postIn=id=>{for(const g of S.to_submit)for(const p of g.posts)if(p.id===id)return p;return null};
  const jpost=async(u,d)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  function paint(){for(const id of [...drafts.keys()])if(!postIn(id))drafts.delete(id);
    const ae=document.activeElement,at=[...body.querySelectorAll('summary')].indexOf(ae),pid=body.contains(ae)&&ae.dataset?ae.dataset.post:null,c1=pid?ae.selectionStart:0,c2=pid?ae.selectionEnd:0;
    body.replaceChildren(...(form!==null?[formView()]:[...(S.to_submit.length?[todo()]:[]),mine(),file()]));
    if(pid){const n=[...body.querySelectorAll('input[data-post]')].find(i=>i.dataset.post===pid);if(n){n.focus({preventScroll:true});n.setSelectionRange(c1,c2)}}     // a redraw keeps your cursor
    else if(at>=0){const n=body.querySelectorAll('summary')[at];if(n)n.focus({preventScroll:true})}}
  const slug=s=>String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,40)||'campaign';   // = campaigns._slug
  function rawList(){if(!S.exists)return [];try{const d=JSON.parse(S.text),a=Array.isArray(d)?d:(d&&d.campaigns);return Array.isArray(a)?a:null}catch(e){return null}}
  const rawId=r=>slug(r&&(r.id||r.name));
  const arr=v=>Array.isArray(v)?v:(typeof v==='string'?v.split(/[\s,]+/).filter(Boolean):[]);     // a hand-typed "a, b" still opens
  async function saveList(change){              // change(the file as it is NOW) -> the new list, or an error text
    const now=await (await fetch('/api/campaigns')).json();S=now;const cur=rawList();
    if(cur===null)return 'campaigns.json is damaged: fix it under "Advanced" first.';
    const list=change(cur);if(typeof list==='string')return list;
    const r=await jpost('/api/campaigns/save',{text:JSON.stringify({campaigns:list},null,2)});
    if(!r.ok)return await r.text();form=null;editing=null;await refresh();load(true);return null}     // an Advanced draft is now stale: saving it would undo this
  async function trySave(change){try{return await saveList(change)}catch(x){return 'Could not save (is the review page still running?): '+x}}

  const editHasRules=r=>{const x=(r&&r.rules)||{};return !!(r&&(r.min||r.cap||r.ends||r.budget_empty))||!!(x.must_show||x.min_len_s||x.max_len_s||x.need_speech||x.no_ai||x.notes||(x.hashtags||[]).length)};
  function formView(){                         // the easy way: a form; the server checks everything again on save
    const list=rawList()||[],isNew=form<0,src=isNew?{}:(list[form]||{}),r0=(src.rules&&typeof src.rules==='object')?src.rules:{};
    const want=JSON.stringify(src);let at=form;
    const same=f=>cur=>{if(!isNew){at=cur.findIndex(r=>JSON.stringify(r)===want);     // the file may have changed since the form opened
        if(at<0)return 'This campaign was changed somewhere else meanwhile: press Cancel and open it again.'}return f(cur.slice())};
    const sec=el('div','acc');sec.append(el('b',null,isNew?'New campaign':'Edit campaign: '+(src.name||'')));
    sec.append(el('div','hint','Copy these from the campaign page (Content Rewards, Whop, ...). Name, page, streamers, platforms and pay are needed; the rest is optional.'));
    const grid=el('div','form2');sec.append(grid);
    const field=(label,input,tip)=>{const l=el('span','lbl',label);if(tip){l.title=tip;input.title=tip}grid.append(l,input);return input};
    const txt=(v,ph)=>{const i=el('input');i.type='text';i.value=v==null?'':v;i.placeholder=ph||'';i.style.cssText='width:100%;box-sizing:border-box';return i};
    const num=(v,ph)=>{const i=el('input');i.type='number';i.min='0';i.step='any';i.value=v==null?'':v;i.placeholder=ph||'';i.style.width='110px';return i};
    const chk=(v,label)=>{const l=el('label'),c=el('input');c.type='checkbox';c.checked=!!v;l.append(c,' '+label);l.cb=c;return l};
    const row=(...xs)=>{const r=el('div','row');r.append(...xs);return r};
    const name=field('Name',txt(src.name,'e.g. Kai Cenat clipping'));
    const url=field('Campaign page',txt(src.url,'https://... the page where you submit your links'));
    const ch=txt(arr(src.channels).join(', '),'streamer names, e.g. kaicenat, xqc');
    const known=[...new Set(clips.map(c=>String(c.channel||'').toLowerCase()).filter(Boolean))].sort();
    const picks=row();if(known.length)picks.append(el('span','mut','Add:'));
    for(const k of known){const b=el('button','sm',k);b.onclick=()=>{const cur=chans();if(!cur.includes(k))ch.value=[...cur,k].join(', ')};picks.append(b)}
    const chWrap=el('div');chWrap.append(ch,picks);field('Streamers',chWrap,'The channel names you watch with "run". Clips of these streamers get the campaign rules.');
    const plats={},pr=row();for(const p of ['tiktok','youtube','instagram']){const c=chk(arr(src.platforms).includes(p),cpLabel(p));plats[p]=c.cb;pr.append(c)}
    field('Pays for',pr,'Only these platforms can be posted to for this campaign');
    const rate=num(src.rate_per_1k,'1.00'),mn=num(src.min,'0'),cap=num(src.cap,'0');
    field('Pay',row(el('span',null,'$'),rate,el('span','mut','per 1000 views')));
    const act=chk(isNew?true:src.active===true,'Running: use this campaign now'),bud=chk(src.budget_empty,'Budget used up (stops posting)');
    field('Status',row(act));
    const more=el('details');more.open=!isNew&&editHasRules(src);more.append(el('summary',null,'Campaign rules: payout, end date, disclosure, hashtags, length'));sec.append(more);
    const g2=el('div','form2');more.append(g2);
    const f2=(label,input,tip)=>{const l=el('span','lbl',label);if(tip){l.title=tip;input.title=tip}g2.append(l,input);return input};
    f2('Payout',row(el('span','mut','minimum $'),mn,el('span','mut','maximum $'),cap,el('span','mut','(0 = no maximum)')));
    const ends=el('input');ends.type='date';const day0=/^\d{4}-\d{2}-\d{2}/.test(String(src.ends||''))?String(src.ends).slice(0,10):'';ends.value=day0;
    const endsOut=()=>day0&&ends.value===day0?src.ends:(ends.value||null);       // an untouched "2026-12-31T18:00" keeps its time; an unreadable one can be cleared
    f2('Ends',row(ends,el('span','mut','leave empty if it has no end date')));
    f2('Budget',row(bud));
    const disc=txt(r0.disclosure===undefined?'Ad':(r0.disclosure||''),'Ad');disc.style.width='160px';
    f2('Disclosure',row(disc,el('span','mut','burned into the first 3 seconds; empty = none')));
    const tags=f2('Hashtags',txt(arr(r0.hashtags).join(' '),'#ad #kaicenat (every post gets these)'));
    const ms=el('select');for(const [v,l] of [['','Nothing special'],['facecam','The webcam (facecam)'],['overlay','The stream overlay'],['handle',"The streamer's @name"]]){const o=el('option',null,l);o.value=v;ms.append(o)}
    ms.value=r0.must_show||'';f2('Must stay visible',ms,'The 9:16 layout keeps this in the picture');
    const lo=num(r0.min_len_s||'','0'),hi=num(r0.max_len_s||'','0');
    f2('Clip length',row(el('span','mut','at least'),lo,el('span','mut','and at most'),hi,el('span','mut','seconds (0 = any)')));
    const sp=chk(r0.need_speech,'Clips must have talking'),ai=chk(r0.no_ai,'No AI-made content');f2('Also',row(sp,ai));
    const notes=el('textarea');notes.rows=2;notes.value=r0.notes||'';notes.placeholder='Anything else from the rules, for you to remember';notes.style.cssText='width:100%;box-sizing:border-box';f2('Notes',notes);
    function chans(){return ch.value.split(/[\s,]+/).map(x=>x.trim().replace(/^.*(twitch\.tv|youtube\.com)\//i,'').replace(/^@/,'').replace(/\/.*$/,'').toLowerCase()).filter(Boolean)}
    const msg=el('div','note bad');msg.style.display='none';
    const WORDS=[['rules.min_len_s','the shortest clip length'],['rules.max_len_s','the longest clip length'],['rules.hashtags','Hashtags'],
      ['rules.disclosure','Disclosure'],['rate_per_1k','Pay per 1000 views'],['cap (0 = no cap)','Maximum payout'],['channels','Streamers'],['platforms','Pays for'],['url ','Campaign page ']];
    const fail=t=>{const m=/^campaign \d+ \((.*?)\):/.exec(t),mine=name.value.trim()||src.name;
      if(m&&m[1]!==mine&&m[1]!==src.name)t='Another campaign in the list has a problem, and nothing can be saved until it is fixed (Cancel, then its "Fix" button): '+t;
      else for(const [a,b] of WORDS)t=t.split(a).join(b);                  // this form's own problem: in the form's words
      msg.textContent=t;msg.style.display=''};     // the file's field names -> the form's
    const sv=el('button','primary',isNew?'Add campaign':'Save changes'),cancel=el('button',null,'Cancel'),del=el('button','danger sm','Delete campaign');del.hidden=isNew;
    cancel.onclick=()=>{form=null;paint()};
    sv.onclick=async()=>{
      const n=v=>v.value===''?0:+v.value,pl=Object.keys(plats).filter(p=>plats[p].checked);
      if(!name.value.trim())return fail('Give the campaign a name.');
      if(!/^https?:[/][/]\S+$/i.test(url.value.trim()))return fail('Paste the campaign page link (it starts with https://).');
      if(!chans().length)return fail('Add at least one streamer.');
      if(!pl.length)return fail('Tick at least one platform the campaign pays for.');
      if(rate.value===''||!(+rate.value>=0))return fail('Type how much it pays per 1000 views.');
      const e={...src,name:name.value.trim(),url:url.value.trim(),channels:chans(),platforms:pl,rate_per_1k:+rate.value,min:n(mn),cap:n(cap),
        ends:endsOut(),active:act.cb.checked,budget_empty:bud.cb.checked,
        rules:{...r0,disclosure:disc.value.trim()||null,hashtags:tags.value.split(/[\s,]+/).filter(Boolean).map(t=>'#'+t.replace(/^#+/,'')),
          must_show:ms.value||null,min_len_s:n(lo),max_len_s:n(hi),need_speech:sp.cb.checked,no_ai:ai.cb.checked,notes:notes.value}};
      if(!isNew&&!src.id)e.id=slug(src.name);              // renaming keeps its posts and stats
      sv.disabled=true;const err=await trySave(same(nl=>{if(isNew)nl.push(e);else nl[at]=e;return nl}));sv.disabled=false;
      if(err){more.open=true;return fail(err)}form=null;paint()};
    del.onclick=async()=>{if(!confirm('Delete the campaign "'+(src.name||'')+'"?'+NL+NL+'Posts already logged for it stay in the list.'))return;
      const err=await trySave(same(nl=>{nl.splice(at,1);return nl}));if(err)return fail(err);form=null;paint()};
    sec.append(msg,row(sv,cancel,el('span','grow'),del));
    setTimeout(()=>name.focus(),0);return sec}

  function todo(){
    const sec=el('div','acc'),n=S.to_submit.reduce((a,g)=>a+g.posts.length,0);sec.append(el('b',null,'To submit ('+n+')'));
    for(const g of S.to_submit){                          // one row per clip: all its posts, timed from the earliest one
      const box=el('div');box.style.cssText='display:flex;flex-direction:column;gap:4px;padding:6px;border-radius:8px;border:'+(g.late?'2px solid #d33':'1px solid #8884');
      const keep=x=>inLane(async()=>{const dr=drafts.get(x.id),p=postIn(x.id);if(!dr)return true;const u=dr.text.trim();if(!p||u===(p.url||'')){drafts.delete(x.id);return true}
        let r;try{r=await jpost('/api/campaigns/post',{id:x.id,url:u})}catch(e){alert('Could not save the link: '+e);return false}if(!r.ok){alert(await r.text());return false}
        p.url=u;const cur=drafts.get(x.id);if(cur){if(cur.text.trim()===u)drafts.delete(x.id);else cur.base=u}return true});     // text typed during the save stays a draft
      const hd=el('div','row');hd.append(el('b',null,String(g.title||g.clip).slice(0,60)),el('span','mut',g.campaign_name),el('span',g.late?'note bad':'mut',cpLeft(g.left_s)),el('span','grow'));
      const all=el('button','sm','Copy all links');all.title='Every link of this clip, one per line';
      all.onclick=async()=>{for(const x of g.posts)if(!await keep(x))return;const urls=g.posts.map(x=>(postIn(x.id)||x).url).filter(Boolean);
        if(!urls.length){alert('Paste the post links first (open each post in its app and copy its link).');return}
        const t=urls.join(NL);try{await navigator.clipboard.writeText(t)}catch(e){prompt('Copy these links:',t)}all.textContent='Copied '+urls.length+' ✓'};
      const op=el('button','sm','Open campaign');op.onclick=()=>window.open(safeUrl(g.campaign_url),'_blank');
      hd.append(all,op);box.append(hd);
      for(const x of g.posts){
        const r=el('div','row'),u=el('input');u.type='text';u.placeholder='Paste the '+cpLabel(x.platform)+' link';const dr=drafts.get(x.id);if(dr&&dr.base!==(x.url||''))drafts.delete(x.id);     // the saved link changed: a draft of the old one is out of date
        u.value=drafts.has(x.id)?drafts.get(x.id).text:(x.url||'');u.dataset.post=x.id;u.style.cssText='flex:1;min-width:200px';
        u.oninput=()=>{const now=(postIn(x.id)||x).url||'';if(u.value===now)drafts.delete(x.id);else drafts.set(x.id,{text:u.value,base:now})};
        const l=el('label'),cb=el('input');cb.type='checkbox';l.append(cb,'Submitted');
        cb.onchange=async()=>{if(!cb.checked)return;for(const y of g.posts)if(!await keep(y)){cb.checked=false;return}
          let q;try{q=await jpost('/api/campaigns/post',{id:x.id,status:'submitted'})}catch(e){alert('Could not save: '+e);cb.checked=false;return}if(!q.ok){alert(await q.text());cb.checked=false;return}refresh();cpPoll()};
        r.append(el('span',null,cpLabel(x.platform)),u,l);box.append(r)}
      sec.append(box)}
    return sec}

  function mine(){
    const sec=el('div','acc'),list=rawList(),valid=new Set(S.campaigns.map(k=>k.id));
    const add=el('button','primary','+ Add campaign');add.onclick=()=>{form=-1;paint()};
    const top=el('div','row');top.append(el('b',null,'Campaigns and earnings'),el('span','grow'));
    if('Notification' in window&&Notification.permission==='default'){const nb=el('button','sm','Turn on desktop alerts');nb.title='The page warns you when a clip is 25 minutes old and still not submitted';nb.onclick=async()=>{await Notification.requestPermission();paint()};top.append(nb)}
    top.append(add);sec.append(top);
    if(list===null){add.disabled=true;sec.append(el('div','note bad','campaigns.json is damaged, so the form cannot open it. Fix it under "Advanced" below (or replace it all with {"campaigns": []} to start again).'))}
    for(const p of S.problems)sec.append(el('div','note bad','⚠ '+p+' (this campaign is ignored until it is fixed)'));
    (list||[]).forEach((r,i)=>{if(valid.has(rawId(r)))return;const b=el('button','sm','Fix "'+((r&&r.name)||'campaign '+(i+1))+'"');b.onclick=()=>{form=i;paint()};b.style.alignSelf='flex-start';sec.append(b)});
    if(!S.campaigns.length)sec.append(el('div','mut','No campaigns yet: press "+ Add campaign" and copy the details from the campaign page.'));
    for(const k of S.campaigns){
      const st=S.stats[k.id],why=S.live[k.id],h=el('div','row'),a=el('a',null,k.name+' ↗');a.href=safeUrl(k.url);a.target='_blank';
      const at=(list||[]).findIndex(r=>rawId(r)===k.id);
      if(at>=0){const ed=el('button','sm','Edit');ed.onclick=()=>{form=at;paint()};h.append(ed)}
      h.append(a,el('span','chip'+(why?'':' ok'),why?'not live: '+why:'live'),el('span','mut','$'+k.rate_per_1k+' per 1000 views · min '+money(k.min)+' · cap '+(k.cap?money(k.cap):'none')+(k.ends?' · ends '+k.ends:'')+' · '+k.platforms.map(cpLabel).join(', ')));
      sec.append(h,el('div',null,st.posted+' posted · '+st.submitted+' submitted · '+st.approved+' approved · '+st.rejected+' rejected · '+st.views.toLocaleString()+' views'),
        el('div','hint','Estimated earnings (approved posts): '+money(st.earnings)+(st.earnings_if_approved>st.earnings?' · if the pending ones are approved too: '+money(st.earnings_if_approved):'')+'.'));
      const ps=S.posts.filter(x=>x.campaign===k.id).slice().reverse(),pd=el('details');pd.open=openPosts.has(k.id);pd.ontoggle=()=>{pd.open?openPosts.add(k.id):openPosts.delete(k.id)};pd.append(el('summary',null,'Posts ('+ps.length+')'));
      for(const x of ps){
        const r=el('div','row'),sel=el('select');for(const [v,l] of Object.entries(CP_ST)){const o=el('option',null,l);o.value=v;sel.append(o)}sel.value=x.status;
        const why2=el('input');why2.type='text';why2.placeholder='Why was it rejected?';why2.value=x.reason||'';why2.style.width='190px';why2.style.display=x.status==='rejected'?'':'none';
        const vw=el('input');vw.type='number';vw.min='0';vw.placeholder='views';vw.value=x.views==null?'':x.views;vw.style.width='110px';vw.title=x.views_src==='api'?'Views from YouTube':'Views (type them in)';
        const save=async()=>{if(sel.value==='rejected'&&!why2.value.trim()){alert('Say why it was rejected: it is saved as a bad example for your style.');why2.focus();return}
          const d={id:x.id,status:sel.value,reason:why2.value};if(vw.value!==''&&+vw.value!==x.views)d.views=+vw.value;
          const dr=drafts.get(x.id);if(dr&&sel.value!=='to_submit')d.url=dr.text.trim();     // leaving To submit: your typed link goes with it
          const undo=()=>{sel.value=x.status;why2.style.display=x.status==='rejected'?'':'none'};     // not saved: the dropdown goes back to the truth
          let q;try{q=await jpost('/api/campaigns/post',d)}catch(e){undo();alert('Could not save: '+e);return}if(!q.ok){undo();alert(await q.text());return}if(dr&&d.url!==undefined&&drafts.get(x.id)===dr)drafts.delete(x.id);refresh();cpPoll()};
        sel.onchange=()=>{why2.style.display=sel.value==='rejected'?'':'none';if(sel.value!=='rejected')save();else why2.focus()};why2.onchange=save;vw.onchange=save;
        const link=x.url?el('a',null,'open post'):el('span','mut','no link yet');if(x.url){link.href=safeUrl(x.url);link.target='_blank'}
        r.append(el('span',null,cpLabel(x.platform)),el('span','mut',String(x.title||x.clip).slice(0,40)),link,sel,why2,vw);pd.append(r)}
      if(ps.length)sec.append(pd)}
    const rv=el('button','sm','Refresh views (YouTube)'),nt=el('span','mut');rv.title='TikTok and Instagram views: type them in';
    rv.onclick=async()=>{rv.disabled=true;try{const r=await jpost('/api/campaigns/views',{});flash=r.ok?(await r.json()).note:await r.text()}catch(e){flash='Could not refresh the views: '+e}rv.disabled=false;await refresh()};
    if(flash){nt.textContent=flash;flash=null}
    const rr=el('div','row');rr.append(rv,nt);sec.append(rr);return sec}

  function file(){
    const sec=el('details','acc');sec.open=advOpen||editing!=null||rawList()===null;sec.ontoggle=()=>{advOpen=sec.open};sec.append(el('summary',null,'Advanced: edit the campaigns file'));
    sec.append(el('div','hint','One entry per campaign: name, url (the campaign page), channels, platforms (tiktok / youtube / instagram), rate_per_1k, min, cap (0 = none), ends ("2026-12-31" or null), active, budget_empty (true stops posting), rules: disclosure ("Ad" or null), hashtags, must_show ("overlay", "facecam", "handle" or null), min_len_s, max_len_s (0 = no limit), need_speech, no_ai, notes. Stored in '+S.path));
    const ta=el('textarea');ta.rows=18;ta.spellcheck=false;ta.value=editing!=null?editing:S.text;ta.style.cssText='width:100%;box-sizing:border-box;font-family:monospace;font-size:12px';ta.oninput=()=>{editing=ta.value};
    const msg=el('div','hint');
    const sv=el('button','primary','Check and save');sv.onclick=async()=>{const r=await jpost('/api/campaigns/save',{text:ta.value});
      if(!r.ok){msg.className='note bad';msg.textContent=await r.text();return}editing=null;await refresh();load(true)};
    const ex=el('button','sm','Insert example');ex.onclick=()=>{ta.value=S.example;editing=ta.value};
    const rr=el('div','row');rr.append(sv,ex);sec.append(ta,rr,msg);return sec}
  await refresh()}

const cpSeen=new Set();
async function cpPoll(){try{const L=await (await fetch('/api/campaigns/todo')).json(),b=document.getElementById('cpN'),btn=document.getElementById('cpBtn');
  const n=L.reduce((a,g)=>a+g.posts.length,0);b.hidden=!n;b.textContent=String(n);btn.classList.toggle('danger',L.some(g=>g.late));
  btn.title=n?n+' campaign post link'+(n>1?'s':'')+' to submit':'Paid clipping campaigns';
  for(const g of L)if(g.late&&!cpSeen.has(g.key)){cpSeen.add(g.key);
    if('Notification' in window&&Notification.permission==='granted')new Notification('Submit your clip links now',{body:g.posts.map(x=>cpLabel(x.platform)).join(', ')+' for '+g.campaign_name+': '+cpLeft(g.left_s)})}}catch(e){}}
document.getElementById('cpBtn').onclick=openCampaigns;cpPoll();setInterval(cpPoll,30000);
"""
