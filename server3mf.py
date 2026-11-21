#!/usr/bin/env python3
"""Serveur web de fichiers 3MF : index, tags, aperçu, description, ouverture Bambu Studio.

Usage: python3 server3mf.py [DOSSIER_3MF] [--port 8000] [--host 0.0.0.0]
Sans dépendance externe (stdlib uniquement).
"""
import argparse, hashlib, json, os, re, shutil, sqlite3, threading, uuid, zipfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

THUMBS = ["Metadata/plate_1.png", "Metadata/thumbnail.png", "Metadata/top_1.png",
          "Metadata/plate_no_light_1.png", "Metadata/pick_1.png",
          "Thumbnails/thumbnail.png", "/Metadata/plate_1.png"]
LOCK = threading.Lock()
ROOT = "."
DB = None


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, size INTEGER,
            mtime REAL, thumb BLOB, description TEXT DEFAULT '', tags TEXT DEFAULT '');
        """)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(files)")]
        if "sha256" not in cols:
            c.execute("ALTER TABLE files ADD COLUMN sha256 TEXT")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def extract_thumb(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for t in THUMBS:
                if t in names:
                    return z.read(t)
            pngs = sorted(n for n in names if n.lower().endswith(".png"))
            if pngs:
                return z.read(pngs[0])
    except Exception:
        pass
    return None


def scan():
    """Indexe récursivement ROOT. Garde tags/description, retire les fichiers disparus."""
    found = set()
    with LOCK, db() as c:
        for d, _, fs in os.walk(ROOT):
            for f in fs:
                if not f.lower().endswith(".3mf"):
                    continue
                full = os.path.join(d, f)
                rel = os.path.relpath(full, ROOT)
                found.add(rel)
                st = os.stat(full)
                row = c.execute("SELECT id,mtime,size,sha256 FROM files WHERE path=?", (rel,)).fetchone()
                if row and row["mtime"] == st.st_mtime and row["size"] == st.st_size and row["sha256"]:
                    continue
                th = extract_thumb(full)
                sh = sha256_file(full)
                if row:
                    c.execute("UPDATE files SET name=?,size=?,mtime=?,thumb=?,sha256=? WHERE id=?",
                              (f, st.st_size, st.st_mtime, th, sh, row["id"]))
                else:
                    c.execute("INSERT INTO files(path,name,size,mtime,thumb,sha256) VALUES(?,?,?,?,?,?)",
                              (rel, f, st.st_size, st.st_mtime, th, sh))
        for r in c.execute("SELECT id,path FROM files").fetchall():
            if r["path"] not in found:
                c.execute("DELETE FROM files WHERE id=?", (r["id"],))
    return len(found)


def safe_rel(p):
    """Chemin relatif de dossier, sûr (pas de .., pas de dossier caché)."""
    p = (p or "").replace("\\", "/")
    parts = [x.strip() for x in p.split("/") if x.strip() and x.strip() != "."]
    if any(x == ".." or x.startswith(".") for x in parts):
        raise ValueError("chemin invalide")
    return "/".join(re.sub(r"[^\w.\- ()\[\]]", "_", x) for x in parts)


def folder_of(path):
    return os.path.dirname(path).replace(os.sep, "/")


def list_folders(rows):
    out = []
    for d, dirs, _ in os.walk(ROOT):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        rel = os.path.relpath(d, ROOT)
        if rel != ".":
            out.append(rel.replace(os.sep, "/"))
    res = []
    for f in sorted(out, key=str.lower):
        n = sum(1 for r in rows if r["folder"] == f or r["folder"].startswith(f + "/"))
        res.append({"path": f, "count": n})
    return {"total": len(rows), "folders": res}


def row_json(r):
    return {"id": r["id"], "name": r["name"], "path": r["path"], "folder": folder_of(r["path"]),
            "size": r["size"],
            "description": r["description"],
            "tags": [t for t in r["tags"].split(",") if t],
            "has_thumb": r["thumb"] is not None}


def clean_tags(tags):
    out = []
    for t in tags:
        t = re.sub(r"[,\s]+", " ", str(t)).strip().lower()
        if t and t not in out:
            out.append(t)
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        if p == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if p == "/api/files":
            s = (q.get("q", [""])[0]).lower()
            tag = q.get("tag", [""])[0]
            fol = q.get("folder", [""])[0].strip("/")
            with db() as c:
                rows = [row_json(r) for r in c.execute("SELECT * FROM files ORDER BY name COLLATE NOCASE")]
            if fol:
                rows = [r for r in rows if r["folder"] == fol or r["folder"].startswith(fol + "/")]
            if s:
                rows = [r for r in rows if s in (r["name"] + " " + r["description"] + " " + " ".join(r["tags"])).lower()]
            if tag:
                rows = [r for r in rows if tag in r["tags"]]
            return self.send(200, rows)
        if p == "/api/folders":
            with db() as c:
                rows = [row_json(r) for r in c.execute("SELECT * FROM files")]
            return self.send(200, list_folders(rows))
        if p == "/api/tags":
            cnt = {}
            with db() as c:
                for r in c.execute("SELECT tags FROM files"):
                    for t in r["tags"].split(","):
                        if t:
                            cnt[t] = cnt.get(t, 0) + 1
            return self.send(200, sorted(cnt.items()))
        m = re.match(r"^/thumb/(\d+)$", p)
        if m:
            with db() as c:
                r = c.execute("SELECT thumb FROM files WHERE id=?", (m.group(1),)).fetchone()
            if r and r["thumb"]:
                return self.send(200, r["thumb"], "image/png", {"Cache-Control": "max-age=3600"})
            return self.send(404, "no thumb", "text/plain")
        m = re.match(r"^/files/(\d+)/", p)
        if m:  # /files/<id>/<nom>.3mf  (URL lue par Bambu Studio)
            with db() as c:
                r = c.execute("SELECT path FROM files WHERE id=?", (m.group(1),)).fetchone()
            if not r:
                return self.send(404, "not found", "text/plain")
            full = os.path.join(ROOT, r["path"])
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", "model/3mf")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(full))
            self.end_headers()
            with open(full, "rb") as f:
                while chunk := f.read(1 << 16):
                    self.wfile.write(chunk)
            return
        self.send(404, "not found", "text/plain")

    def upload(self):
        """POST /api/upload : corps = fichier brut, nom dans l'en-tête X-Filename."""
        name = os.path.basename(unquote(self.headers.get("X-Filename", "")).replace("\\", "/"))
        name = re.sub(r"[^\w.\- ()\[\]]", "_", name).strip()
        if not name.lower().endswith(".3mf"):
            return self.send(400, {"error": "fichier .3mf requis"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            folder = safe_rel(unquote(self.headers.get("X-Folder", "uploads")))
        except ValueError:
            return self.send(400, {"error": "dossier invalide"})
        updir = os.path.join(ROOT, folder)
        os.makedirs(updir, exist_ok=True)
        tmp = os.path.join(updir, ".tmp-" + uuid.uuid4().hex)
        h = hashlib.sha256()
        with open(tmp, "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(1 << 16, left))
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                left -= len(chunk)
        if left > 0 or not zipfile.is_zipfile(tmp):
            os.remove(tmp)
            return self.send(400, {"error": "fichier incomplet ou invalide"})
        with LOCK, db() as c:
            dup = c.execute("SELECT name,path FROM files WHERE sha256=?", (h.hexdigest(),)).fetchone()
        if dup:
            os.remove(tmp)
            return self.send(200, {"status": "duplicate", "name": dup["name"]})
        base, ext = os.path.splitext(name)
        dest, i = os.path.join(updir, name), 1
        while os.path.exists(dest):
            dest = os.path.join(updir, "%s (%d)%s" % (base, i, ext))
            i += 1
        shutil.move(tmp, dest)
        scan()
        self.send(200, {"status": "added", "name": os.path.basename(dest)})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/upload":
            return self.upload()
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}")
        if p == "/api/rescan":
            return self.send(200, {"count": scan()})
        if p == "/api/folders":
            try:
                rel = safe_rel(data.get("path", ""))
            except ValueError:
                return self.send(400, {"error": "chemin invalide"})
            if not rel:
                return self.send(400, {"error": "nom vide"})
            os.makedirs(os.path.join(ROOT, rel), exist_ok=True)
            return self.send(200, {"ok": True, "path": rel})
        m = re.match(r"^/api/files/(\d+)/move$", p)
        if m:
            try:
                folder = safe_rel(data.get("folder", ""))
            except ValueError:
                return self.send(400, {"error": "chemin invalide"})
            with LOCK, db() as c:
                r = c.execute("SELECT path,name FROM files WHERE id=?", (m.group(1),)).fetchone()
                if not r:
                    return self.send(404, {"error": "introuvable"})
                src = os.path.join(ROOT, r["path"])
                os.makedirs(os.path.join(ROOT, folder), exist_ok=True)
                base, ext = os.path.splitext(r["name"])
                dest = os.path.join(ROOT, folder, r["name"])
                if os.path.abspath(dest) == os.path.abspath(src):
                    return self.send(200, {"ok": True})
                i = 1
                while os.path.exists(dest):
                    dest = os.path.join(ROOT, folder, "%s (%d)%s" % (base, i, ext))
                    i += 1
                shutil.move(src, dest)
                c.execute("UPDATE files SET path=?,name=?,mtime=? WHERE id=?",
                          (os.path.relpath(dest, ROOT), os.path.basename(dest), os.stat(dest).st_mtime, m.group(1)))
            return self.send(200, {"ok": True})
        m = re.match(r"^/api/files/(\d+)$", p)
        if m:
            tags = ",".join(clean_tags(data.get("tags", [])))
            with db() as c:
                c.execute("UPDATE files SET description=?, tags=? WHERE id=?",
                          (str(data.get("description", "")), tags, m.group(1)))
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bibliothèque 3MF</title>
<style>
:root{--bg:#14161a;--card:#1e2128;--fg:#e8eaed;--mut:#8b93a1;--acc:#00ae42}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
header{position:sticky;top:0;background:#0e1013;padding:12px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;z-index:5}
h1{font-size:18px;margin:0 8px 0 0}
input,textarea,button{font:inherit;color:var(--fg);background:var(--card);border:1px solid #333a45;border-radius:8px;padding:8px 10px}
button{cursor:pointer}button.pri{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
#q{flex:1;min-width:180px}
#tags{padding:10px 16px;display:flex;gap:6px;flex-wrap:wrap}
.tag{background:#2a303a;border-radius:99px;padding:3px 10px;font-size:13px;cursor:pointer}
.tag.on{background:var(--acc)}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;padding:16px}
.card{background:var(--card);border-radius:12px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column}
.card:hover{outline:2px solid var(--acc)}
.card img,.noimg{width:100%;aspect-ratio:1;object-fit:contain;background:#2b2f38}
.noimg{display:grid;place-items:center;color:var(--mut)}
.card .t{padding:8px 10px;font-size:13px;word-break:break-word}
.card .tg{padding:0 10px 10px;display:flex;gap:4px;flex-wrap:wrap}
.card .tg span{background:#2a303a;border-radius:99px;padding:1px 8px;font-size:11px;color:var(--mut)}
#modal{position:fixed;inset:0;background:#000b;display:none;place-items:center;z-index:10;padding:16px}
#modal.on{display:grid}
.box{background:var(--card);border-radius:14px;max-width:900px;width:100%;max-height:95vh;overflow:auto;display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px}
@media(max-width:700px){.box{grid-template-columns:1fr}}
.box img{width:100%;background:#2b2f38;border-radius:10px;object-fit:contain}
.f{display:flex;flex-direction:column;gap:10px}
textarea{min-height:110px;resize:vertical;width:100%}
.row{display:flex;gap:8px;flex-wrap:wrap}.mut{color:var(--mut);font-size:13px;word-break:break-all}
a.btn{display:inline-block;text-decoration:none;background:var(--acc);color:#fff;font-weight:700;padding:10px 14px;border-radius:8px}
a.btn.sec{background:#2a303a}
#main{display:flex}aside{width:240px;flex-shrink:0;padding:12px 8px 12px 16px;font-size:14px}
#main>section{flex:1;min-width:0}
.fi{padding:5px 8px;border-radius:8px;cursor:pointer}.fi:hover{background:#2a303a}.fi.on{background:var(--acc)}
@media(max-width:700px){#main{display:block}aside{width:auto}}
</style></head><body>
<header><h1>📦 3MF</h1><input id="q" placeholder="Rechercher (nom, tag, description)…">
<button id="nf">📁 Nouveau dossier</button><button class="pri" id="imp">⬆ Importer depuis ce PC</button><button id="scan">Réindexer</button><span class="mut" id="cnt"></span></header>
<datalist id="fl"></datalist><div id="main"><aside id="tree"></aside><section><div id="tags"></div><div id="grid"></div></section></div>
<input type="file" id="picker" webkitdirectory multiple hidden>
<div id="modal"><div class="box" id="box"></div></div>
<script>
let files=[],tag="",cur=null,folder="",FOLDERS=[];
const $=s=>document.querySelector(s),esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){
  const r=await fetch('/api/files?q='+encodeURIComponent($('#q').value)+'&tag='+encodeURIComponent(tag)+'&folder='+encodeURIComponent(folder));
  files=await r.json();$('#cnt').textContent=files.length+' fichier(s)';
  $('#grid').innerHTML=files.map(f=>`<div class="card" data-id="${f.id}">
    ${f.has_thumb?`<img loading="lazy" src="/thumb/${f.id}">`:'<div class="noimg">pas d\'image</div>'}
    <div class="t">${esc(f.name)}${f.folder?`<div class="mut">📁 ${esc(f.folder)}</div>`:''}</div><div class="tg">${f.tags.map(t=>`<span>${esc(t)}</span>`).join('')}</div></div>`).join('');
  await loadTree();
  const tr=await (await fetch('/api/tags')).json();
  $('#tags').innerHTML=tr.map(([t,n])=>`<span class="tag ${t===tag?'on':''}" data-t="${esc(t)}">${esc(t)} (${n})</span>`).join('');
}
async function loadTree(){
  const d=await (await fetch('/api/folders')).json();FOLDERS=d.folders.map(x=>x.path);
  $('#fl').innerHTML=FOLDERS.map(p=>`<option value="${esc(p)}">`).join('');
  const row=(p,label,n,dep)=>`<div class="fi ${p===folder?'on':''}" data-f="${esc(p)}" style="padding-left:${dep*14+8}px">${label} <span class="mut">${n}</span></div>`;
  $('#tree').innerHTML=row('','📚 Tout',d.total,0)+d.folders.map(x=>row(x.path,'📁 '+esc(x.path.split('/').pop()),x.count,x.path.split('/').length)).join('');
}
$('#tree').onclick=e=>{const t=e.target.closest('.fi');if(t){folder=t.dataset.f;load()}};
$('#nf').onclick=async()=>{const n=prompt('Nom du nouveau dossier'+(folder?' (dans '+folder+')':''));if(!n)return;
  await fetch('/api/folders',{method:'POST',body:JSON.stringify({path:(folder?folder+'/':'')+n})});load()};
$('#grid').onclick=e=>{const c=e.target.closest('.card');if(c)open(+c.dataset.id)};
$('#tags').onclick=e=>{const t=e.target.closest('.tag');if(t){tag=tag===t.dataset.t?'':t.dataset.t;load()}};
$('#q').oninput=()=>load();
$('#scan').onclick=async()=>{$('#scan').textContent='…';await fetch('/api/rescan',{method:'POST',body:'{}'});$('#scan').textContent='Réindexer';load()};
function open(id){
  cur=files.find(f=>f.id===id);
  const url=location.origin+'/files/'+cur.id+'/'+encodeURIComponent(cur.name);
  const bs='bambustudio://open?file='+encodeURIComponent(url);
  $('#box').innerHTML=`<div>${cur.has_thumb?`<img src="/thumb/${cur.id}">`:''}</div>
  <div class="f"><b>${esc(cur.name)}</b><span class="mut">${esc(cur.path)} · ${(cur.size/1048576).toFixed(2)} Mo</span>
  <div class="row"><a class="btn" href="${bs}">🖨 Ouvrir dans Bambu Studio</a><a class="btn sec" href="${url}">Télécharger</a></div>
  <label>Tags (séparés par des virgules)<input id="etags" style="width:100%" value="${esc(cur.tags.join(', '))}"></label>
  <label>Dossier<input id="efolder" list="fl" style="width:100%" value="${esc(cur.folder)}" placeholder="(racine)"></label>
  <label>Description<textarea id="edesc">${esc(cur.description)}</textarea></label>
  <div class="row"><button class="pri" id="save">Enregistrer</button><button id="close">Fermer</button></div></div>`;
  $('#modal').classList.add('on');
  $('#close').onclick=close;
  $('#save').onclick=async()=>{
    const nf=$('#efolder').value.trim();
    if(nf!==cur.folder)await fetch('/api/files/'+cur.id+'/move',{method:'POST',body:JSON.stringify({folder:nf})});
    await fetch('/api/files/'+cur.id,{method:'POST',body:JSON.stringify({description:$('#edesc').value,tags:$('#etags').value.split(',')})});
    close();load()};
}
function close(){$('#modal').classList.remove('on')}
$('#imp').onclick=()=>{$('#picker').value='';$('#picker').click()};
$('#picker').onchange=()=>{
  const list=[...$('#picker').files].filter(f=>f.name.toLowerCase().endsWith('.3mf'))
    .sort((a,b)=>(a.webkitRelativePath||a.name).localeCompare(b.webkitRelativePath||b.name));
  if(!list.length){alert('Aucun fichier .3mf dans ce dossier.');return}
  $('#box').innerHTML=`<div class="f" style="grid-column:1/-1"><b>Importer ${list.length} fichier(s) 3MF vers la bibliothèque</b>
   <div class="row" style="align-items:center"><label>Dossier de destination <input id="dest" list="fl" value="${esc(folder)}" placeholder="(racine)"></label>
   <label><input type="checkbox" id="keep" checked> garder l'arborescence du dossier source</label></div>
   <div class="row"><button id="all">Tout cocher</button><button id="none">Tout décocher</button><span class="mut" id="sel"></span></div>
   <div id="ilist" style="max-height:55vh;overflow:auto;display:flex;flex-direction:column;gap:4px">
   ${list.map((f,i)=>`<label class="row" style="align-items:center;gap:8px"><input type="checkbox" data-i="${i}" checked>
     <span style="flex:1;word-break:break-all">${esc(f.webkitRelativePath||f.name)}</span>
     <span class="mut">${(f.size/1048576).toFixed(1)} Mo</span><span class="mut" id="st${i}"></span></label>`).join('')}</div>
   <div class="row"><button class="pri" id="go">Transférer la sélection</button><button id="close">Fermer</button></div></div>`;
  $('#modal').classList.add('on');$('#close').onclick=()=>{close();load()};
  const boxes=()=>[...document.querySelectorAll('#ilist input')];
  const upd=()=>$('#sel').textContent=boxes().filter(b=>b.checked).length+' sélectionné(s)';
  $('#ilist').onchange=upd;upd();
  $('#all').onclick=()=>{boxes().forEach(b=>b.checked=true);upd()};
  $('#none').onclick=()=>{boxes().forEach(b=>b.checked=false);upd()};
  $('#go').onclick=async()=>{
    $('#go').disabled=true;
    for(const b of boxes().filter(b=>b.checked)){
      const i=+b.dataset.i,st=$('#st'+i);b.disabled=true;st.textContent='⏳';
      const rp=(list[i].webkitRelativePath||'').split('/');rp.pop();rp.shift();
      const target=[$('#dest').value.trim(),$('#keep').checked?rp.join('/'):''].filter(Boolean).join('/');
      try{const r=await fetch('/api/upload',{method:'POST',headers:{'X-Filename':encodeURIComponent(list[i].name),'X-Folder':encodeURIComponent(target)},body:list[i]});
        const j=await r.json();st.textContent=j.status==='added'?'✅ ajouté':j.status==='duplicate'?'↩ déjà présent':'❌ '+(j.error||'erreur')}
      catch(e){st.textContent='❌ réseau'}
    }
    $('#go').textContent='Terminé';
  };
};
$('#modal').onclick=e=>{if(e.target.id==='modal')close()};
document.onkeydown=e=>{if(e.key==='Escape')close()};
load();
</script></body></html>"""


def main():
    global ROOT, DB
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=os.environ.get("MF_ROOT", "./3mf"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    ROOT = os.path.abspath(a.root)
    os.makedirs(ROOT, exist_ok=True)
    DB = os.path.join(ROOT, ".library.db")
    init_db()
    print("Indexation de %s : %d fichier(s)" % (ROOT, scan()))
    print("http://%s:%d" % ("localhost" if a.host == "0.0.0.0" else a.host, a.port))
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
