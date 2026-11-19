"""The Campaigns window + the per-clip campaign badge of the review page (JavaScript). Kept apart from review.PAGE (an ordinary
Python string that cannot contain backslashes); spliced into the page where PAGE has the /*__CP__*/ marker."""

JS = r"""
const CP_ST={to_submit:'To submit',submitted:'Submitted',approved:'Approved',rejected:'Rejected'};
const cpLabel=k=>(shareInfo.platforms[k]||{label:k}).label;
const money=x=>'$'+(Math.round((x||0)*100)/100).toFixed(2);
function cpLeft(s){if(s<=0)return 'deadline passed';return Math.ceil(s/60)+' min left'}
function cpBadge(c){
  const k=c.campaign,ex=(c.exports||[]).slice(-1)[0]||{},o=ex.originality||{},tags=[];
  if(o.voiceover)tags.push('🎙 Your voice-over');if(o.hook)tags.push('🪝 Hook text');
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
  const box=el('details','fine');box.open=!!openSt.get(c.id+'og');box.ontoggle=()=>openSt.set(c.id+'og',box.open);
  const vo=c.voiceover;box.append(el('summary',null,'Make it original: hook text + your voice-over'+(vo?' 🎙':'')));
  const hk=el('input');hk.type='text';hk.maxLength=120;hk.placeholder='Hook text, shown big for the first 3 seconds (optional)';hk.value=t.hook||'';hk.oninput=()=>{t.hook=hk.value};hk.style.cssText='width:100%;box-sizing:border-box';
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
  row.append(rb,pb,pick,del,st);box.append(hk,row,use,el('div','hint','The voice-over starts with the clip. Both are burned into the next export, and the clip is tagged so you can see which clips have them.'));
  return box}

async function openCampaigns(){
  const ov=el('div','modal'),box=el('div','mbox');ov.append(box);
  box.append(el('h2',null,'💰 Campaigns'));
  box.append(el('div','hint','Paid clipping campaigns (Content Rewards and the like). While a campaign is live, every clip of its streamers keeps the required part of the picture visible, gets the disclosure burned in for its first 3 seconds, "#ad" first in the post text, the campaign hashtags and a credit line. Post stays blocked until the campaign rules pass. Submit each post link on the campaign page within 30 minutes.'));
  const body=el('div');body.style.cssText='display:flex;flex-direction:column;gap:12px';box.append(body);
  const close=el('button',null,'Close');box.append(close);
  document.body.append(ov);
  let S=null,editing=null,flash=null;
  let dirty=false;                             // a link you pasted but did not save yet: no background redraw may wipe it
  const iv=setInterval(()=>{if(!ov.isConnected){clearInterval(iv);return}if(!dirty&&!body.contains(document.activeElement))refresh()},15000);
  close.onclick=()=>{clearInterval(iv);ov.remove();sig='';load(true);cpPoll()};
  async function refresh(){S=await (await fetch('/api/campaigns')).json();paint()}
  const jpost=async(u,d)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  function paint(){body.replaceChildren(todo(),mine(),file())}

  function todo(){
    const sec=el('div','acc'),n=S.to_submit.reduce((a,g)=>a+g.posts.length,0);sec.append(el('b',null,'To submit ('+n+')'));
    if(!S.to_submit.length)sec.append(el('div','mut','Nothing waiting. A post made for a campaign shows up here: submit its link on the campaign page within 30 minutes.'));
    if('Notification' in window&&Notification.permission==='default'){const nb=el('button','sm','Turn on desktop alerts');nb.title='The page warns you when a clip is 25 minutes old and still not submitted';nb.onclick=async()=>{await Notification.requestPermission();paint()};nb.style.alignSelf='flex-start';sec.append(nb)}
    for(const g of S.to_submit){                          // one row per clip: all its posts, timed from the earliest one
      const box=el('div'),inputs={};box.style.cssText='display:flex;flex-direction:column;gap:4px;padding:6px;border-radius:8px;border:'+(g.late?'2px solid #d33':'1px solid #8884');
      const keep=async x=>{const u=inputs[x.id].value.trim();if(u===(x.url||''))return true;const r=await jpost('/api/campaigns/post',{id:x.id,url:u});if(!r.ok){alert(await r.text());return false}x.url=u;return true};
      const hd=el('div','row');hd.append(el('b',null,String(g.title||g.clip).slice(0,60)),el('span','mut',g.campaign_name),el('span',g.late?'note bad':'mut',cpLeft(g.left_s)),el('span','grow'));
      const all=el('button','sm','Copy all links');all.title='Every link of this clip, one per line';
      all.onclick=async()=>{for(const x of g.posts)if(!await keep(x))return;dirty=false;const urls=g.posts.map(x=>x.url).filter(Boolean);
        if(!urls.length){alert('Paste the post links first (open each post in its app and copy its link).');return}
        const t=urls.join(NL);try{await navigator.clipboard.writeText(t)}catch(e){prompt('Copy these links:',t)}all.textContent='Copied '+urls.length+' ✓'};
      const op=el('button','sm','Open campaign');op.onclick=()=>window.open(safeUrl(g.campaign_url),'_blank');
      hd.append(all,op);box.append(hd);
      for(const x of g.posts){
        const r=el('div','row'),u=el('input');u.type='text';u.placeholder='Paste the '+cpLabel(x.platform)+' link';u.value=x.url||'';u.style.cssText='flex:1;min-width:200px';inputs[x.id]=u;u.oninput=()=>{dirty=true};
        const l=el('label'),cb=el('input');cb.type='checkbox';l.append(cb,'Submitted');
        cb.onchange=async()=>{if(!cb.checked)return;for(const y of g.posts)if(!await keep(y)){cb.checked=false;return}dirty=false;
          const q=await jpost('/api/campaigns/post',{id:x.id,status:'submitted'});if(!q.ok){alert(await q.text());cb.checked=false;return}refresh();cpPoll()};
        r.append(el('span',null,cpLabel(x.platform)),u,l);box.append(r)}
      sec.append(box)}
    return sec}

  function mine(){
    const sec=el('div','acc');sec.append(el('b',null,'Campaigns, posts and earnings'));
    for(const p of S.problems)sec.append(el('div','note bad','⚠ '+p+' (this campaign is ignored until it is fixed)'));
    if(!S.campaigns.length)sec.append(el('div','mut','No campaigns yet: add one in campaigns.json below.'));
    for(const k of S.campaigns){
      const st=S.stats[k.id],why=S.live[k.id],h=el('div','row'),a=el('a',null,k.name+' ↗');a.href=safeUrl(k.url);a.target='_blank';
      h.append(a,el('span','chip'+(why?'':' ok'),why?'not live: '+why:'live'),el('span','mut','$'+k.rate_per_1k+' per 1000 views · min '+money(k.min)+' · cap '+(k.cap?money(k.cap):'none')+(k.ends?' · ends '+k.ends:'')+' · '+k.platforms.map(cpLabel).join(', ')));
      sec.append(h,el('div',null,st.posted+' posted · '+st.submitted+' submitted · '+st.approved+' approved · '+st.rejected+' rejected · '+st.views.toLocaleString()+' views'),
        el('div','hint','Estimated earnings (approved posts): '+money(st.earnings)+(st.earnings_if_approved>st.earnings?' · if the pending ones are approved too: '+money(st.earnings_if_approved):'')+'. min(views / 1000 × rate, cap) × 0.9, nothing under the minimum.'));
      for(const x of S.posts.filter(x=>x.campaign===k.id).slice().reverse()){
        const r=el('div','row'),sel=el('select');for(const [v,l] of Object.entries(CP_ST)){const o=el('option',null,l);o.value=v;sel.append(o)}sel.value=x.status;
        const why2=el('input');why2.type='text';why2.placeholder='Why was it rejected?';why2.value=x.reason||'';why2.hidden=x.status!=='rejected';
        const vw=el('input');vw.type='number';vw.min='0';vw.placeholder='views';vw.value=x.views==null?'':x.views;vw.style.width='110px';vw.title=x.views_src==='api'?'Views from YouTube':'Views (type them in)';
        const save=async()=>{if(sel.value==='rejected'&&!why2.value.trim()){alert('Say why it was rejected: it is saved as a bad example for your style.');why2.focus();return}
          const d={id:x.id,status:sel.value,reason:why2.value};if(vw.value!==''&&+vw.value!==x.views)d.views=+vw.value;
          const q=await jpost('/api/campaigns/post',d);if(!q.ok){alert(await q.text());return}refresh();cpPoll()};
        sel.onchange=()=>{why2.hidden=sel.value!=='rejected';if(sel.value!=='rejected')save();else why2.focus()};why2.onchange=save;vw.onchange=save;
        const link=x.url?el('a',null,'open post'):el('span','mut','no link yet');if(x.url){link.href=safeUrl(x.url);link.target='_blank'}
        r.append(el('span',null,cpLabel(x.platform)),el('span','mut',String(x.title||x.clip).slice(0,40)),link,sel,why2,vw);sec.append(r)}}
    const rv=el('button','sm','Refresh views (YouTube)'),nt=el('span','mut');rv.title='TikTok and Instagram views: type them in';
    rv.onclick=async()=>{rv.disabled=true;try{const r=await jpost('/api/campaigns/views',{});flash=r.ok?(await r.json()).note:await r.text()}catch(e){flash='Could not refresh the views: '+e}rv.disabled=false;await refresh()};
    if(flash){nt.textContent=flash;flash=null}
    const rr=el('div','row');rr.append(rv,nt);sec.append(rr);return sec}

  function file(){
    const sec=el('details','acc');sec.open=!S.campaigns.length&&!S.problems.length?true:editing!=null;sec.append(el('summary',null,'Edit campaigns.json'));
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
