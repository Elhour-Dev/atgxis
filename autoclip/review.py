"""Local review page: ranked clips with players, keep / trash / make-vertical buttons. Binds to 127.0.0.1 only."""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import Config
from .captions import CaptionsUnavailable, build_ass, transcribe
from .cutter import export_clip

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
</style>
<header><h1>Clip review</h1>
<select id=f><option value=new>To review</option><option value=keep>Kept</option><option value=trash>Trash</option><option value=all>All</option></select>
<select id=s><option value=score>Best score first</option><option value=created>Newest first</option></select>
<button id=purge>Delete trashed files</button><span class=mut id=count></span></header>
<div class=grid id=g></div>
<script>
let clips=[],sig='';const trims=new Map();const g=document.getElementById('g');
const enc=id=>id.split('/').map(encodeURIComponent).join('/');
const fmt=t=>Math.floor(t/60)+':'+String(Math.floor(t%60)).padStart(2,'0');
const fileUrl=(c,f)=>'/files/'+enc(c.dir)+'/'+encodeURIComponent(f);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
async function load(force){
  const next=await (await fetch('/api/clips')).json();
  const ns=JSON.stringify(next.map(c=>[c.id,c.status,(c.exports||[]).length]));
  const playing=[...document.querySelectorAll('video')].some(v=>!v.paused);
  clips=next;if(ns!==sig&&(force||!playing)){sig=ns;render()}}
function numInput(v,on){const i=el('input');i.type='number';i.step='0.1';i.min='0';i.value=v;i.oninput=()=>on(parseFloat(i.value));return i}
function render(){
  const f=document.getElementById('f').value,s=document.getElementById('s').value;
  let list=clips.filter(c=>f==='all'||c.status===f);
  list.sort((a,b)=>s==='score'?b.score-a.score:b.created.localeCompare(a.created));
  g.replaceChildren();document.getElementById('count').textContent=list.length+' clip(s)';
  for(const c of list){
    if(!trims.has(c.id))trims.set(c.id,{start:c.suggest_start_s,end:c.suggest_end_s,vertical:true,captions:false});
    const t=trims.get(c.id);
    const card=el('div','card '+c.status);
    const v=el('video');v.controls=true;v.preload='metadata';v.src=fileUrl(c,c.file);
    v.onloadedmetadata=()=>{v.currentTime=t.start};
    v.ontimeupdate=()=>{if(!v.paused&&v.currentTime>t.end+0.2)v.currentTime=t.start};
    card.append(v);
    const r=el('div','row');r.append(el('span','score',Math.round(c.score)),el('span','mut',c.channel+' · '+c.created.replace('T',' ')+' · draft '+fmt(c.duration_s)+' · moment at '+fmt(c.moment_s)));card.append(r);
    const sp=c.spike;card.append(el('div','mut',sp.peak+' chatters at once (normal '+Math.round(sp.baseline)+') · '+sp.msgs+' msgs'+(c.loudness_jump_db!=null?' · audio '+(c.loudness_jump_db>0?'+':'')+c.loudness_jump_db+' dB':'')));
    if(sp.top&&sp.top.length)card.append(el('div','chat',sp.top.map(x=>x[0]+' ×'+x[1]).join('   ')));
    const tr=el('div','trim'),len=el('span','mut'),upd=()=>{len.textContent='= '+Math.max(t.end-t.start,0).toFixed(1)+' s'};
    const a=numInput(t.start,x=>{t.start=x;upd()}),b=numInput(t.end,x=>{t.end=x;upd()});
    const setA=el('button',null,'◀ start = player');setA.onclick=()=>{t.start=+v.currentTime.toFixed(1);a.value=t.start;upd()};
    const setB=el('button',null,'end = player ▶');setB.onclick=()=>{t.end=+v.currentTime.toFixed(1);b.value=t.end;upd()};
    const r1=el('div','row');r1.append(el('span','mut','Trim'),a,setA,b,setB,len);upd();
    const r2=el('div','row');
    for(const [k,label] of [['vertical','Vertical 9:16'],['captions','Captions']]){const l=el('label'),cb=el('input');cb.type='checkbox';cb.checked=t[k];cb.onchange=()=>{t[k]=cb.checked};l.append(cb,label);r2.append(l)}
    const ex=el('button','primary','Export clip');ex.onclick=async()=>{ex.disabled=true;ex.textContent='Encoding…';const ok=await post('/api/export',{id:c.id,start:t.start,end:t.end,vertical:t.vertical,captions:t.captions});ex.disabled=false;ex.textContent='Export clip';if(ok)load(true)};
    r2.append(ex);tr.append(r1,r2);card.append(tr);
    if((c.exports||[]).length){const e=el('div','row');e.append(el('span','mut','Exports:'));for(const x of c.exports){const l=el('a',null,(x.vertical?'9:16 ':'')+(x.captions?'CC ':'')+fmt(x.start)+'-'+fmt(x.end));l.href=fileUrl(c,x.file);l.target='_blank';e.append(l)}card.append(e)}
    const bt=el('div','row');
    for(const [label,st] of [['Keep','keep'],['Trash','trash'],['Undo','new']]){if(st===c.status||(st==='new'&&c.status==='new'))continue;
      const x=el('button',null,label);x.onclick=async()=>{await post('/api/status',{id:c.id,status:st});load(true)};bt.append(x)}
    const src=el('a',null,'Channel');src.href=c.source_url;src.target='_blank';bt.append(src);
    card.append(bt);g.append(card)}}
async function post(u,d){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok){alert(await r.text());return false}return true}
document.getElementById('f').onchange=()=>render();document.getElementById('s').onchange=()=>render();
document.getElementById('purge').onclick=async()=>{if(confirm('Permanently delete every trashed clip file?')){await post('/api/purge',{});load(true)}};
load(true);setInterval(()=>load(false),10000);
</script>"""


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Store:
    def __init__(self, root: Path, cfg: Config):
        self.root, self.cfg, self.lock = root.resolve(), cfg, threading.Lock()

    def _metas(self):
        for p in self.root.rglob("*.clip.json"):
            try:
                yield p, json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

    def list(self) -> list[dict]:
        out = []
        for p, m in self._metas():
            m["dir"] = p.parent.relative_to(self.root).as_posix()
            out.append(m)
        return out

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

    def export(self, cid: str, start: float, end: float, vertical: bool, captions: bool) -> str:
        """Precise trim (+ optional 9:16, + optional burned captions) in ONE encode. Returns the new file name."""
        p, m = self.find(cid)
        if not p:
            raise ValueError("clip not found")
        src = p.parent / m["file"]
        if not (0 <= start < end <= m["duration_s"] + 1) or end - start > 600:
            raise ValueError(f"bad trim {start}-{end} (draft is {m['duration_s']}s)")
        ass = None
        if captions:
            words = transcribe(src, self.cfg)                      # may raise CaptionsUnavailable
            if not words:
                raise ValueError("no speech found in this clip, so there is nothing to caption")
            ass = build_ass(words, start, end, vertical, self.cfg.caption_font)
        name = f"{src.stem}_{int(start * 10)}-{int(end * 10)}{'_v' if vertical else ''}{'_cc' if captions else ''}.mp4"
        if not export_clip(src, p.parent / name, start, end, self.cfg, vertical, ass):
            raise RuntimeError("ffmpeg failed; see the console running `review`")
        with self.lock:
            p, m = self.find(cid)
            m.setdefault("exports", [])
            m["exports"] = [e for e in m["exports"] if e["file"] != name] + [
                {"file": name, "start": start, "end": end, "vertical": vertical, "captions": captions}]
            _write_json(p, m)
        return name

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
            if path.startswith("/files/"):
                f = (store.root / path[7:]).resolve()
                if store.root not in f.parents or not f.is_file() or f.suffix.lower() != ".mp4":
                    return self._send(404, b"not found", "text/plain")
                return self._serve_file(f)
            self._send(404, b"not found", "text/plain")

        def _serve_file(self, f: Path):          # supports Range so seeking in <video> works
            size, start, end = f.stat().st_size, 0, f.stat().st_size - 1
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = min(int(b), size - 1) if b else size - 1
            with open(f, "rb") as fh:
                fh.seek(start)
                data = fh.read(end - start + 1)
            self._send(206 if rng else 200, data, mimetypes.guess_type(f.name)[0] or "video/mp4",
                       {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}"})

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
            elif path == "/api/purge":
                store.purge()
                ok = True
            self._send(200 if ok else 400, b"{}" if ok else b"failed", "application/json" if ok else "text/plain")

    return H


def serve(root: Path, cfg: Config, port: int = 8765, open_browser: bool = True):
    root.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Store(root, cfg)))
    url = f"http://127.0.0.1:{port}/"
    print(f"Review page: {url}   (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
