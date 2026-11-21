"""The Autopilot window + the per-clip badge of the review page (JavaScript). Kept apart from review.PAGE, which is an ordinary Python
string and cannot contain backslashes; this one is a raw string and is spliced into the page where PAGE has the /*__AP__*/ marker."""

JS = r"""
function apTime(ts){return new Date(ts*1000).toTimeString().slice(0,5)}
function apBadge(c){
  const t=c.taste,a=c.autopilot||{};
  if(!t&&!a.state)return null;
  const d=el('div','row');
  if(t){
    const b=el('span','chip'+(t.pick?' ok':''),'🤖 '+Math.round(t.p*100)+'%'+(t.state==='early'?' (early guess)':''));
    b.title=t.why&&t.why.length?'Why: '+t.why.map(w=>(w.w>0?'+ ':'- ')+w.text).join(', '):'Estimated chance that you would keep this clip.';
    d.append(b);
    if(t.pick&&!a.state)d.append(el('span','mut','Autopilot would post this one'));
  }
  if(a.state==='queued'||a.state==='retry'){
    d.append(el('span','note','Autopilot posts this at '+apTime(a.post_at||a.retry_at||0)+' unless you Trash it.'));
    const h=el('button',null,"Don't post");h.onclick=async()=>{await post('/api/autopilot/hold',{id:c.id});load(true)};d.append(h);
  }else if(a.state==='posting')d.append(el('span','note','Autopilot is posting this now...'));
  else if(a.state==='posted')d.append(el('span','note good','Posted by autopilot'+(a.platforms?' to '+a.platforms.join(', '):'')+'. A good pick? Keep or Trash it below: that is how it learns.'));
  else if(a.state==='failed')d.append(el('span','note bad','Autopilot could not post this: '+(a.message||'unknown reason')));
  else if(a.state==='held')d.append(el('span','mut','You stopped autopilot for this clip.'));
  else if(a.state==='trashed')d.append(el('span','mut','Autopilot moved this to Trash ('+(a.message||'low score')+'). Undo brings it back.'));
  else if(a.state==='restored'&&c.status==='new')d.append(el('span','mut','Autopilot had trashed this ('+(a.message||'low score')+'); you brought it back.'));
  else if(a.state==='skipped')d.append(el('span','mut','Autopilot skipped this ('+(a.message||'your style rules')+').'));
  return d}

async function openAutopilot(){
  const ov=el('div','modal'),box=el('div','mbox');ov.append(box);
  box.append(el('h2',null,'🤖 Autopilot'));
  box.append(el('div','hint','Learns from your Keep and Trash, then can post the clips it is sure about. It starts off, and posts nothing until it has proved itself and you turn it on.'));
  const body=el('div');body.style.cssText='display:flex;flex-direction:column;gap:12px';box.append(body);
  const close=el('button',null,'Close');close.onclick=()=>{ov.remove();sig='';load(true)};box.append(close);
  document.body.append(ov);
  let S=null,flash=null;                      // flash: a message that must survive the redraw that follows a save
  const folds={},pct=x=>Math.round(x*100)+'%';   // folds: which sections you opened, kept across redraws
  const fold=(key,title,x,open)=>{const d=el('details','acc');d.open=key in folds?folds[key]:!!open;d.ontoggle=()=>{folds[key]=d.open};
    x.classList.remove('acc');x.style.cssText='display:flex;flex-direction:column;gap:6px;margin-top:6px';d.append(el('summary',null,title),x);return d};
  async function refresh(){S=await (await fetch('/api/autopilot')).json();paint()}

  function learned(){
    const sec=el('div','acc'),n=S.counts,r=S.ready;
    sec.append(el('div',null,n.total+' decisions so far: '+n.keeps+' kept, '+n.trashes+' trashed. Every Keep and Trash you press teaches it (and deleting trashed files does not make it forget).'));
    if(S.model&&S.model.auc!=null)sec.append(el('div','hint','Tested on clips it had not seen while learning: it puts a clip you kept above one you trashed '+pct(S.model.auc)+' of the time (50% would be guessing).'));
    if(r.ok)sec.append(el('div','hint','At that level, on your past clips it would have picked '+r.support+' and you would have kept '+r.hits+' of them ('+pct(r.precision)+'). The honest worst case, after allowing for luck, is about '+pct(r.lower)+'.'));
    const cv=(S.curve||[]).filter(x=>x.picked>0);
    if(cv.length){
      const tb=el('table');tb.style.cssText='border-collapse:collapse;font-size:13px';
      const hd=el('tr');for(const h of ['If it only acts when sure at least','it would pick','you agreed with']){const th=el('th',null,h);th.style.cssText='text-align:left;padding:2px 10px 2px 0;font-weight:600';hd.append(th)}tb.append(hd);
      for(const x of cv){const tr=el('tr');for(const v of [pct(x.at),x.picked+' of '+S.model.n+' clips',x.agreed+' ('+pct(x.precision)+')']){const td=el('td',null,v);td.style.padding='2px 10px 2px 0';tr.append(td)}tb.append(tr)}
      sec.append(el('div','hint','What each confidence level would have done on the clips you already judged (not clips it learned from):'),tb)}
    const again=el('button',null,'Learn again now'),note=el('div','hint');
    again.onclick=async()=>{again.disabled=true;note.textContent='Learning...';const x=await fetch('/api/autopilot/train',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});again.disabled=false;if(!x.ok){note.textContent=await x.text();return}await refresh()};
    again.style.alignSelf='flex-start';sec.append(again,note);return sec}

  function controls(){
    const sec=el('div','acc'),s=S.settings;sec.append(el('b',null,'What it may do'));
    if(s.paused_reason){const w=el('div','note bad');w.textContent='⏸ Paused: '+s.paused_reason;sec.append(w)}
    const rd=S.ready,chip=el('div','chip'+(rd.ok?' ok':''),rd.ok?'✅ Ready: it may post clips it is at least '+pct(rd.threshold)+' sure about':'⏳ '+rd.reason);
    chip.style.alignSelf='flex-start';chip.style.whiteSpace='normal';sec.append(chip);
    const mode=el('select');
    for(const [v,t] of [['off','Off: only learn from my Keep / Trash'],['suggest','Suggest only: mark the clips it would post, post nothing'],['post','Post automatically (only clips it is sure about)']]){const o=el('option',null,t);o.value=v;mode.append(o)}
    mode.value=s.mode;sec.append(mode);
    const row=(label,node)=>{const r=el('div','row');r.append(el('span','mut',label),node);return r};
    const pls=el('div','row'),pb={};
    for(const [k,v] of Object.entries(S.platforms)){const l=el('label'),cb=el('input');cb.type='checkbox';cb.checked=s.platforms.includes(k);pb[k]=cb;l.append(cb,v.label);pls.append(l)}
    sec.append(el('div','mut','Post to (needs the account connected in Setup, Connect accounts):'),pls);
    const prov=el('select');for(const p of S.providers.filter(p=>p.automatic)){const o=el('option',null,p.label+(p.configured?'':' (not set up)'));o.value=p.name;prov.append(o)}
    prov.value=s.provider;
    const pv=S.providers.find(p=>p.name===s.provider);if(s.mode!=='off'&&!(pv&&pv.configured))sec.append(el('div','note bad','Its posting method is not set up, so it cannot post: open Setup, Connect accounts.'));
    const chs=el('div','row'),cb2={};
    for(const c of S.channels){const l=el('label'),cb=el('input');cb.type='checkbox';cb.checked=s.channels.includes(c);cb2[c]=cb;l.append(cb,c);chs.append(l)}
    sec.append(el('div','mut','Only these streamers (tick the ones whose clips you are allowed to post: publishing needs the streamer\'s permission):'),
      S.channels.length?chs:el('div','hint','No clips yet.'));
    const num=(v,step)=>{const i=el('input');i.type='number';i.step=step||'1';i.min='0';i.value=v;return i};
    const per=num(s.max_per_day),gap=num(s.gap_min),delay=num(s.delay_min),age=num(s.max_age_h),prec=num(Math.round(s.min_precision*100)),conf=num(Math.round(s.min_confidence*100));
    const adv=el('details');adv.open=!!folds.limits;adv.ontoggle=()=>{folds.limits=adv.open};
    adv.append(el('summary',null,'Limits and safety: '+s.max_per_day+' posts a day, '+s.gap_min+' min apart, '+s.delay_min+' min to cancel'),row('How to post',prov));
    const lim=el('div','row');
    lim.append(el('span','mut','At most'),per,el('span','mut','posts per 24 h, at least'),gap,el('span','mut','min apart, wait'),delay,el('span','mut','min before posting (your time to veto), never older than'),age,el('span','mut','h'));
    adv.append(lim);
    const sco=el('div','row'),minS=num(s.min_score),trS=num(s.trash_below);
    sco.append(el('span','mut','Post only clips scoring at least'),minS,el('span','mut','. Move clips scoring under'),trS,el('span','mut','to Trash (0 = never; you can Undo). Scores in between wait for you.'));
    adv.append(sco);
    const req=el('div','row');
    req.append(el('span','mut','It must have been right at least'),prec,el('span','mut','% of the time on your past clips, and be at least'),conf,el('span','mut','% sure of each clip'));
    adv.append(req);
    const stL=el('label'),useSt=el('input');useSt.type='checkbox';useSt.checked=s.use_style;stL.append(useSt,'Use my style (trim, captions, title, description, usual platforms, skip rules) - see "My style" below');adv.append(stL);
    const capL=el('label'),cap=el('input');cap.type='checkbox';cap.checked=s.captions;capL.append(cap,'Add captions when autopilot has to export a clip itself (needs faster-whisper)');adv.append(capL);sec.append(adv);
    const save=el('button','primary','Save'),m2=el('div','note');save.style.alignSelf='flex-start';
    if(flash){m2.className='note '+flash.cls;m2.textContent=flash.text;flash=null}
    save.onclick=async()=>{
      const body={mode:mode.value,platforms:Object.keys(pb).filter(k=>pb[k].checked),provider:prov.value,channels:Object.keys(cb2).filter(k=>cb2[k].checked),
        max_per_day:+per.value,gap_min:+gap.value,delay_min:+delay.value,max_age_h:+age.value,min_precision:(+prec.value)/100,min_confidence:(+conf.value)/100,captions:cap.checked,use_style:useSt.checked,min_score:+minS.value,trash_below:+trS.value};
      if(body.mode==='post'&&(!body.platforms.length||!body.channels.length)){m2.className='note bad';m2.textContent='Tick at least one platform and one streamer first.';return}
      if(body.mode==='post'&&s.mode!=='post'&&!confirm('Switch on automatic posting?'+NL+NL+'Clips it is sure about will be published to '+body.platforms.join(', ')+' WITHOUT you pressing anything, after the waiting time you set, for: '+body.channels.join(', ')+'.'+NL+NL+(body.trash_below>0?'Clips scoring under '+body.trash_below+' from these streamers are moved to Trash (you can Undo).'+NL+NL:'')+'You can stop any single clip with "Don\'t post" or by trashing it, and it pauses itself if you trash its posts.'))return;
      const x=await fetch('/api/autopilot/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!x.ok){m2.className='note bad';m2.textContent=await x.text();return}
      flash={cls:'good',text:'Saved.'};await refresh()};
    sec.append(save,m2);
    sec.append(el('div','hint','Autopilot posts while the clipper is running ("python -m autoclip run ..."), or on its own with "python -m autoclip autopilot run". Posts need a 9:16 export; it makes one if the recorder has not.'));
    return sec}

  function activity(){
    const sec=el('div','acc');
    sec.append(el('div','hint',S.posted_24h+' posted in the last 24 hours.'+(S.regret.decided?' Of its last '+S.regret.decided+' posts that you judged, you trashed '+S.regret.trashed+'.':'')));
    if(S.queue.length){sec.append(el('div','mut','Waiting to be posted:'));
      for(const q of S.queue)sec.append(el('div',null,apTime(q.at)+'  '+q.channel+'  '+pct(q.p||0)+'  '+(q.title||q.id)))}
    if(S.recent.length){sec.append(el('div','mut','Recent:'));
      for(const e of S.recent)sec.append(el('div','hint',new Date(e.t*1000).toLocaleString()+'  '+e.action+(e.platforms?' '+e.platforms.join(', '):'')+(e.title?'  '+e.title:'')+(e.message?'  - '+e.message:'')))}
    else sec.append(el('div','hint','Nothing yet.'));
    return sec}

  function mystyle(){
    const sec=el('div','acc'),st=S.style;
    if(!st){sec.append(el('div','hint','Could not read your style files.'));return sec}
    sec.append(el('div',null,'Style: '+st.examples+' examples, updated '+(st.updated?new Date(st.updated*1000).toLocaleString():'never')+(S.settings.use_style?'':' (switched off)')));
    sec.append(el('div','hint','Every Keep, Trash, title or description edit, export and post of yours is an example. A rule changes only when at least 5 examples agree 70% of the time. "taught" rules come from the autoclip-teacher skill.'));
    const show=v=>v===null||v===undefined?'-':Array.isArray(v)?(v.length?v.join(', '):'-'):typeof v==='boolean'?(v?'yes':'no'):String(v);
    for(const f of st.fields){if(f.source==='default')continue;
      sec.append(el('div','hint',f.field+': '+show(f.value)+'  ('+f.source+')'))}
    if(!st.fields.some(f=>f.source!=='default'))sec.append(el('div','hint','Nothing learned yet: it uses the normal suggestions.'));
    for(const pr of st.problems||[])sec.append(el('div','note bad',pr));
    return sec}

  function paint(){body.replaceChildren(controls(),fold('activity','Activity ('+S.queue.length+' waiting, '+S.posted_24h+' posted in 24 h)',activity(),S.queue.length>0),
    fold('learned','What it has learned ('+S.counts.total+' decisions)',learned()),fold('style','My style',mystyle()))}
  await refresh();
}
document.getElementById('ap').onclick=openAutopilot;
"""
