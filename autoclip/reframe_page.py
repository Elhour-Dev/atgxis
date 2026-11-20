"""The card's "Reframe & quality" box (JavaScript): zoom room / low-resolution warnings and a before / after still of the export.
Kept apart from review.PAGE (an ordinary Python string that cannot contain backslashes); spliced in at the /*__RF__*/ marker."""

JS = r"""
function rpost(u,d){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})}
function rfBox(c,t){
  const box=el('details','fine');box.open=!!openSt.get(c.id+'rf');
  box.append(el('summary',null,'Reframe & quality: before / after'));
  const info=el('div','hint'),imgs=el('div','row'),warn=el('div');
  const bef=el('img'),aft=el('img');for(const i of [bef,aft]){i.style.cssText='max-height:320px;max-width:48%;border-radius:8px;background:#0003'}
  bef.alt='Before: the stream frame';aft.alt='After: the 9:16 export frame';
  const lab=(x,s)=>{const d=el('div');d.style.cssText='display:flex;flex-direction:column;gap:2px;align-items:center';d.append(el('span','mut',s),x);return d};
  imgs.append(lab(bef,'Before'),lab(aft,'After (9:16)'));
  const go=el('button','sm','Show before / after');
  const show=async()=>{go.disabled=true;go.textContent='…';
    const q=new URLSearchParams({id:c.id,start:t.start,end:t.end,vertical:t.vertical?1:0});
    const r=await fetch('/api/quality?'+q);go.disabled=false;go.textContent='↻ Refresh';
    if(!r.ok){info.textContent=await r.text();return}
    const d=await r.json(),p=d.report.panels||{},z=d.report.zoom_room||{};
    info.textContent=(d.report.res?'Source '+d.report.res[0]+'x'+d.report.res[1]+' · ':'')+Object.keys(p).map(k=>k+' enlarged '+p[k]+'x (zoom room '+z[k]+'x)').join(' · ')+' · frame at '+fmt(d.t-t.start)+' of the clip';
    warn.replaceChildren(...d.report.warnings.map(w=>el('div','note warn',w)));
    bef.src='/api/frame?'+new URLSearchParams({id:c.id,t:d.t});
    aft.src='/api/after?'+new URLSearchParams({id:c.id,t:d.t,start:t.start,end:t.end,vertical:t.vertical?1:0,n:Date.now()})};
  go.onclick=show;
  box.ontoggle=()=>{openSt.set(c.id+'rf',box.open)};
  box.append(go,info,warn,imgs,el('div','hint','One pass from the original recording: lanczos scaling, a light sharpen only while zoomed, CRF 18 or better. A zoom never enlarges the stream more than 1.5x.'));
  return box}
"""
