"""Local review page: ranked clips with players, keep / trash / make-vertical buttons. Binds to 127.0.0.1 only."""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import Config
from .cutter import make_vertical

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Clip review</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--card:#f4f4f6;--mut:#666;--acc:#6d28d9}
@media(prefers-color-scheme:dark){:root{--bg:#121214;--fg:#eee;--card:#1d1d21;--mut:#9a9aa3}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
header{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
h1{font-size:20px;margin:0 12px 0 0} select,button{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid #8884;background:var(--card);color:var(--fg);cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
.card{background:var(--card);border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:8px}
.card.keep{outline:2px solid #16a34a}.card.trash{opacity:.45}
video{width:100%;border-radius:8px;background:#000} .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.score{font-weight:700;color:var(--acc);font-size:18px}.mut{color:var(--mut);font-size:13px}
.chat{font-size:13px;background:#8881;border-radius:8px;padding:6px 8px;max-height:84px;overflow:auto}
</style>
<header><h1>Clip review</h1>
<select id=f><option value=new>To review</option><option value=keep>Kept</option><option value=trash>Trash</option><option value=all>All</option></select>
<select id=s><option value=score>Best score first</option><option value=created>Newest first</option></select>
<button id=purge>Delete trashed files</button><span class=mut id=count></span></header>
<div class=grid id=g></div>
<script>
let clips=[];const g=document.getElementById('g');
const enc=id=>id.split('/').map(encodeURIComponent).join('/');
async function load(){clips=await (await fetch('/api/clips')).json();render()}
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function render(){
  const f=document.getElementById('f').value,s=document.getElementById('s').value;
  let list=clips.filter(c=>f==='all'||c.status===f);
  list.sort((a,b)=>s==='score'?b.score-a.score:b.created.localeCompare(a.created));
  g.replaceChildren();document.getElementById('count').textContent=list.length+' clip(s)';
  for(const c of list){
    const card=el('div','card '+c.status);
    const v=el('video');v.controls=true;v.preload='metadata';v.src='/files/'+enc(c.dir)+'/'+encodeURIComponent(c.file);card.append(v);
    const r=el('div','row');r.append(el('span','score',Math.round(c.score)),el('span','mut',c.channel+' · '+c.created.replace('T',' ')+' · '+c.duration_s+'s'));card.append(r);
    const sp=c.spike;card.append(el('div','mut',sp.peak+' chatters at once (normal '+Math.round(sp.baseline)+') · '+sp.msgs+' msgs'+(c.loudness_jump_db!=null?' · audio '+(c.loudness_jump_db>0?'+':'')+c.loudness_jump_db+' dB':'')));
    if(sp.top&&sp.top.length)card.append(el('div','chat',sp.top.map(t=>t[0]+' ×'+t[1]).join('   ')));
    const b=el('div','row');
    for(const [label,st] of [['Keep','keep'],['Trash','trash'],['Undo','new']]){if(st===c.status||(st==='new'&&c.status==='new'))continue;
      const x=el('button',null,label);x.onclick=async()=>{await post('/api/status',{id:c.id,status:st});load()};b.append(x)}
    if(c.vertical){const a=el('a',null,'Vertical file');a.href='/files/'+enc(c.dir)+'/'+encodeURIComponent(c.vertical);a.target='_blank';b.append(a)}
    else{const x=el('button',null,'Make vertical 9:16');x.onclick=async()=>{x.textContent='Working…';x.disabled=true;await post('/api/vertical',{id:c.id});load()};b.append(x)}
    const src=el('a',null,'Channel');src.href=c.source_url;src.target='_blank';b.append(src);
    card.append(b);g.append(card)}}
async function post(u,d){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok)alert(await r.text())}
document.getElementById('f').onchange=render;document.getElementById('s').onchange=render;
document.getElementById('purge').onclick=async()=>{if(confirm('Permanently delete every trashed clip file?')){await post('/api/purge',{});load()}};
load();setInterval(load,15000);
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

    def vertical(self, cid: str) -> bool:
        with self.lock:
            p, m = self.find(cid)
        if not p:
            return False
        src = p.parent / m["file"]
        dst = src.with_name(src.stem + "_vertical.mp4")
        if not dst.exists() and not make_vertical(src, dst, self.cfg):
            return False
        with self.lock:
            m["vertical"] = dst.name
            _write_json(p, m)
        return True

    def purge(self) -> int:
        n = 0
        with self.lock:
            for p, m in list(self._metas()):
                if m.get("status") == "trash":
                    for name in (m.get("file"), m.get("vertical")):
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
            elif path == "/api/vertical":
                ok = store.vertical(str(body.get("id")))
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
