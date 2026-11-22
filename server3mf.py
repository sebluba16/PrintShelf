#!/usr/bin/env python3
"""Serveur web de fichiers 3MF : index, tags, aperçu, description, ouverture Bambu Studio.

Usage: python3 server3mf.py [DOSSIER_3MF] [--port 8000] [--host 0.0.0.0]
Sans dépendance externe (stdlib uniquement).
"""
import argparse, array, hashlib, json, os, re, shutil, sqlite3, sys, threading, uuid, zipfile
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

THUMBS = ["Metadata/plate_1.png", "Metadata/thumbnail.png", "Metadata/top_1.png",
          "Metadata/plate_no_light_1.png", "Metadata/pick_1.png",
          "Thumbnails/thumbnail.png", "/Metadata/plate_1.png"]
LOCK = threading.Lock()
MAX_UPLOAD = 2048 << 20      # taille max d'un fichier envoyé (option --max-mb)
MAX_JSON = 1 << 20           # taille max d'un corps JSON
MAX_THUMB = 20 << 20         # taille max d'une miniature lue dans un 3MF (anti zip-bomb)
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
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


def inside_root(path):
    root = os.path.realpath(ROOT)
    return os.path.realpath(path).startswith(root + os.sep)


def read_small(z, name):
    return z.read(name) if z.getinfo(name).file_size <= MAX_THUMB else None


def extract_thumb(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for t in THUMBS:
                if t in names:
                    return read_small(z, t)
            pngs = sorted(n for n in names if n.lower().endswith(".png"))
            if pngs:
                return read_small(z, pngs[0])
    except Exception:
        pass
    return None


# ---------- aperçu 3D : extraction du maillage d'un 3MF ----------
MAX_MODEL = 64 << 20     # taille max (décompressée) d'un fichier .model lu
MAX_TRIS = 2_000_000
MESH_CACHE = {}
IDENT = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def ln(tag):
    return tag.rsplit("}", 1)[-1]


def parse_tf(s):
    if not s:
        return IDENT
    f = [float(x) for x in s.split()]
    if len(f) != 12:
        raise ValueError("transformation invalide")
    return [f[0:3] + [0], f[3:6] + [0], f[6:9] + [0], f[9:12] + [1]]


def matmul(a, b):  # a appliquée en premier (vecteurs-lignes, convention 3MF)
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def load_model(z, name):
    info = z.getinfo(name)
    if info.file_size > MAX_MODEL:
        raise ValueError("modèle trop gros pour l'aperçu 3D")
    data = z.read(name)
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:65536]:
        raise ValueError("fichier refusé")
    root = ET.fromstring(data)
    objs, build = {}, []
    for sec in root:
        t = ln(sec.tag)
        if t == "resources":
            for o in sec:
                if ln(o.tag) != "object":
                    continue
                mesh = next((m for m in o if ln(m.tag) == "mesh"), None)
                if mesh is not None:
                    verts, tris = [], []
                    for part in mesh:
                        pt = ln(part.tag)
                        if pt == "vertices":
                            verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in part]
                        elif pt == "triangles":
                            tris = [(int(q.get("v1")), int(q.get("v2")), int(q.get("v3"))) for q in part]
                    objs[o.get("id")] = {"mesh": (verts, tris)}
                else:
                    comps = []
                    for cs in o:
                        if ln(cs.tag) == "components":
                            for cc in cs:
                                path = next((v for k, v in cc.attrib.items() if ln(k) == "path"), None)
                                comps.append((cc.get("objectid"), path, cc.get("transform")))
                    objs[o.get("id")] = {"components": comps}
        elif t == "build":
            build = [(it.get("objectid"), it.get("transform")) for it in sec if ln(it.tag) == "item"]
    return objs, build


def build_mesh(path):
    """Retourne les triangles du 3MF : float32 little-endian, 9 valeurs par triangle."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        main = next((n for n in names if n.lower() == "3d/3dmodel.model"), None) or \
            next((n for n in names if n.lower().startswith("3d/") and n.lower().endswith(".model")), None)
        if not main:
            raise ValueError("aucun modèle 3D dans ce fichier")
        models = {}

        def get(name):
            name = name.lstrip("/")
            if name not in models:
                models[name] = load_model(z, name)
            return models[name]

        out = array.array("f")

        def emit(oid, mname, tf, depth):
            if depth > 8:
                raise ValueError("assemblage trop profond")
            o = get(mname)[0].get(oid)
            if o is None:
                return
            if "mesh" in o:
                verts, tris = o["mesh"]
                if len(out) // 9 + len(tris) > MAX_TRIS:
                    raise ValueError("modèle trop détaillé pour l'aperçu 3D")
                m = tf
                tv = [(x * m[0][0] + y * m[1][0] + z_ * m[2][0] + m[3][0],
                       x * m[0][1] + y * m[1][1] + z_ * m[2][1] + m[3][1],
                       x * m[0][2] + y * m[1][2] + z_ * m[2][2] + m[3][2]) for x, y, z_ in verts]
                for a, b, c in tris:
                    out.extend(tv[a]); out.extend(tv[b]); out.extend(tv[c])
            else:
                for cid, cpath, ctf in o["components"]:
                    emit(cid, cpath.lstrip("/") if cpath else mname, matmul(parse_tf(ctf), tf), depth + 1)

        objs, build = get(main)
        if build:
            for oid, tf in build:
                emit(oid, main, parse_tf(tf), 0)
        else:
            for oid, o in objs.items():
                if "mesh" in o:
                    emit(oid, main, IDENT, 0)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def scan():
    """Indexe récursivement ROOT. Garde tags/description, retire les fichiers disparus."""
    found = set()
    with LOCK, db() as c:
        for d, _, fs in os.walk(ROOT):
            for f in fs:
                if not f.lower().endswith(".3mf"):
                    continue
                full = os.path.join(d, f)
                if not inside_root(full):  # lien symbolique vers l'extérieur
                    continue
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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        if p == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8", {"Cache-Control": "no-store"})
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
        m = re.match(r"^/api/mesh/(\d+)$", p)
        if m:
            with db() as c:
                r = c.execute("SELECT path,mtime,size FROM files WHERE id=?", (m.group(1),)).fetchone()
            full = os.path.join(ROOT, r["path"]) if r else ""
            if not r or not inside_root(full) or not os.path.isfile(full):
                return self.send(404, {"error": "introuvable"})
            key = (r["path"], r["mtime"], r["size"])
            data = MESH_CACHE.get(key)
            if data is None:
                try:
                    data = build_mesh(full)
                except ValueError as e:
                    return self.send(422, {"error": str(e)})
                except Exception:
                    return self.send(422, {"error": "modèle 3D illisible"})
                if len(MESH_CACHE) >= 6:
                    MESH_CACHE.pop(next(iter(MESH_CACHE)))
                MESH_CACHE[key] = data
            return self.send(200, data, "application/octet-stream", {"Cache-Control": "private, max-age=300"})
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
            if not inside_root(full) or not os.path.isfile(full):
                return self.send(404, "not found", "text/plain")
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", "model/3mf")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(os.path.basename(full)))
            self.end_headers()
            with open(full, "rb") as f:
                while chunk := f.read(1 << 16):
                    self.wfile.write(chunk)
            return
        self.send(404, "not found", "text/plain")

    def csrf_ok(self):
        """Anti-CSRF : en-tête personnalisé (impose un preflight CORS, jamais accepté) + Origin identique."""
        if self.headers.get("X-Requested-With") != "PrintShelf":
            return False
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == self.headers.get("Host")

    def upload(self):
        """POST /api/upload : corps = fichier brut, nom dans l'en-tête X-Filename."""
        name = os.path.basename(unquote(self.headers.get("X-Filename", "")).replace("\\", "/"))
        name = re.sub(r"[^\w.\- ()\[\]]", "_", name).strip()
        if not name.lower().endswith(".3mf"):
            return self.send(400, {"error": "fichier .3mf requis"})
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = -1
        if n <= 0:
            return self.send(400, {"error": "corps vide"})
        if n > MAX_UPLOAD:
            return self.send(413, {"error": "fichier trop gros"})
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
        if not self.csrf_ok():
            return self.send(403, {"error": "requête refusée"})
        if p == "/api/upload":
            return self.upload()
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 <= n <= MAX_JSON:
                raise ValueError
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "requête invalide"})
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
        if p == "/api/folders/rename":
            try:
                old = safe_rel(data.get("path", ""))
                new = safe_rel(data.get("name", ""))
            except ValueError:
                return self.send(400, {"error": "nom invalide"})
            if not old or not new or "/" in new:
                return self.send(400, {"error": "nom invalide"})
            parent = os.path.dirname(old)
            newrel = (parent + "/" if parent else "") + new
            src, dst = os.path.join(ROOT, old), os.path.join(ROOT, newrel)
            if not os.path.isdir(src):
                return self.send(404, {"error": "dossier introuvable"})
            if os.path.exists(dst):
                return self.send(409, {"error": "ce nom existe déjà"})
            pre = old + "/"
            with LOCK, db() as c:
                os.rename(src, dst)
                for r in c.execute("SELECT id,path FROM files WHERE substr(path,1,?)=?", (len(pre), pre)).fetchall():
                    c.execute("UPDATE files SET path=? WHERE id=?", (newrel + "/" + r["path"][len(pre):], r["id"]))
            return self.send(200, {"ok": True, "path": newrel})
        if p == "/api/folders/delete":
            try:
                rel = safe_rel(data.get("path", ""))
            except ValueError:
                return self.send(400, {"error": "chemin invalide"})
            target = os.path.join(ROOT, rel)
            if not rel or not os.path.isdir(target):
                return self.send(404, {"error": "dossier introuvable"})
            if any(fs for _, _, fs in os.walk(target)):
                return self.send(409, {"error": "dossier non vide : déplace d'abord ses fichiers"})
            shutil.rmtree(target)
            return self.send(200, {"ok": True})
        m = re.match(r"^/api/files/(\d+)/delete$", p)
        if m:
            with LOCK, db() as c:
                r = c.execute("SELECT path FROM files WHERE id=?", (m.group(1),)).fetchone()
                if not r:
                    return self.send(404, {"error": "introuvable"})
                full = os.path.join(ROOT, r["path"])
                if not inside_root(full):
                    return self.send(400, {"error": "chemin invalide"})
                if os.path.isfile(full):
                    os.remove(full)
                c.execute("DELETE FROM files WHERE id=?", (m.group(1),))
            return self.send(200, {"ok": True})
        m = re.match(r"^/api/files/(\d+)/rename$", p)
        if m:
            name = os.path.basename(str(data.get("name", "")).replace("\\", "/")).strip()
            name = re.sub(r"[^\w.\- ()\[\]]", "_", name)
            if not name.lower().endswith(".3mf"):
                name += ".3mf"
            if name.lower() == ".3mf":
                return self.send(400, {"error": "nom invalide"})
            with LOCK, db() as c:
                r = c.execute("SELECT path FROM files WHERE id=?", (m.group(1),)).fetchone()
                if not r:
                    return self.send(404, {"error": "introuvable"})
                src = os.path.join(ROOT, r["path"])
                dst = os.path.join(os.path.dirname(src), name)
                if dst == src:
                    return self.send(200, {"ok": True})
                if os.path.exists(dst):
                    return self.send(409, {"error": "ce nom existe déjà"})
                os.rename(src, dst)
                c.execute("UPDATE files SET path=?,name=? WHERE id=?", (os.path.relpath(dst, ROOT), name, m.group(1)))
            return self.send(200, {"ok": True})
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
#fbar{display:none;padding:10px 16px 0;gap:8px;align-items:center}
</style></head><body>
<header><h1>📦 3MF</h1><input id="q" placeholder="Rechercher (nom, tag, description)…">
<button id="nf">📁 Nouveau dossier</button><button class="pri" id="imp">⬆ Importer depuis ce PC</button><button id="scan">Réindexer</button><span class="mut" id="cnt"></span></header>
<datalist id="fl"></datalist><div id="main"><aside id="tree"></aside><section><div id="fbar"></div><div id="tags"></div><div id="grid"></div></section></div>
<input type="file" id="picker" webkitdirectory multiple hidden>
<div id="modal"><div class="box" id="box"></div></div>
<script>
const _f=window.fetch.bind(window);
window.fetch=(u,o={})=>_f(u,{...o,headers:{...(o.headers||{}),'X-Requested-With':'PrintShelf'}});
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
const M4={
 mul(a,b){const o=new Float32Array(16);for(let c=0;c<4;c++)for(let r=0;r<4;r++){let s=0;for(let k=0;k<4;k++)s+=a[k*4+r]*b[c*4+k];o[c*4+r]=s}return o},
 persp(f,asp,n,fa){const t=1/Math.tan(f/2),o=new Float32Array(16);o[0]=t/asp;o[5]=t;o[10]=(fa+n)/(n-fa);o[11]=-1;o[14]=2*fa*n/(n-fa);return o},
 look(e,c,u){let z=[e[0]-c[0],e[1]-c[1],e[2]-c[2]];const nz=Math.hypot(...z);z=z.map(x=>x/nz);
  let x=[u[1]*z[2]-u[2]*z[1],u[2]*z[0]-u[0]*z[2],u[0]*z[1]-u[1]*z[0]];const nx=Math.hypot(...x);x=x.map(v=>v/nx);
  const y=[z[1]*x[2]-z[2]*x[1],z[2]*x[0]-z[0]*x[2],z[0]*x[1]-z[1]*x[0]];
  return new Float32Array([x[0],y[0],z[0],0,x[1],y[1],z[1],0,x[2],y[2],z[2],0,
   -(x[0]*e[0]+x[1]*e[1]+x[2]*e[2]),-(y[0]*e[0]+y[1]*e[1]+y[2]*e[2]),-(z[0]*e[0]+z[1]*e[1]+z[2]*e[2]),1])}
};
function view3d(cv,buf){
  const gl=cv.getContext('webgl2');if(!gl)return{err:'WebGL2 indisponible sur ce navigateur'};
  const v=new Float32Array(buf);if(!v.length)return{err:'modèle vide'};
  const mn=[1e30,1e30,1e30],mx=[-1e30,-1e30,-1e30];
  for(let i=0;i<v.length;i+=3)for(let k=0;k<3;k++){const x=v[i+k];if(x<mn[k])mn[k]=x;if(x>mx[k])mx[k]=x}
  const c=mn.map((m,k)=>(m+mx[k])/2),rad=Math.hypot(mx[0]-mn[0],mx[1]-mn[1],mx[2]-mn[2])/2||1;
  const sh=(t,src)=>{const s=gl.createShader(t);gl.shaderSource(s,src);gl.compileShader(s);return s};
  const pr=gl.createProgram();
  gl.attachShader(pr,sh(gl.VERTEX_SHADER,`#version 300 es
in vec3 p;uniform mat4 mv,mvp;out vec3 vp;void main(){vp=(mv*vec4(p,1.)).xyz;gl_Position=mvp*vec4(p,1.);}`));
  gl.attachShader(pr,sh(gl.FRAGMENT_SHADER,`#version 300 es
precision highp float;in vec3 vp;out vec4 o;void main(){
vec3 n=normalize(cross(dFdx(vp),dFdy(vp)));float l=abs(dot(n,normalize(vec3(.3,.5,.8))));
o=vec4(vec3(.0,.68,.26)*(.25+.75*l)+vec3(.12)*pow(l,8.),1.);}`));
  gl.linkProgram(pr);if(!gl.getProgramParameter(pr,gl.LINK_STATUS))return{err:'shader invalide'};
  gl.useProgram(pr);
  const vb=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,vb);gl.bufferData(gl.ARRAY_BUFFER,v,gl.STATIC_DRAW);
  const loc=gl.getAttribLocation(pr,'p');gl.enableVertexAttribArray(loc);gl.vertexAttribPointer(loc,3,gl.FLOAT,false,0,0);
  gl.enable(gl.DEPTH_TEST);
  let yaw=-0.8,pitch=0.6,dist=rad*2.6;
  function draw(){
    const d=window.devicePixelRatio||1,w=Math.max(1,Math.round(cv.clientWidth*d)),h=Math.max(1,Math.round(cv.clientHeight*d));
    if(cv.width!==w||cv.height!==h){cv.width=w;cv.height=h}
    gl.viewport(0,0,w,h);gl.clearColor(.17,.18,.22,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
    const e=[c[0]+dist*Math.cos(pitch)*Math.cos(yaw),c[1]+dist*Math.cos(pitch)*Math.sin(yaw),c[2]+dist*Math.sin(pitch)];
    const mv=M4.look(e,c,[0,0,1]),pj=M4.persp(0.8,w/h,Math.max(rad*0.02,dist-rad*2),dist+rad*2);
    gl.uniformMatrix4fv(gl.getUniformLocation(pr,'mv'),false,mv);
    gl.uniformMatrix4fv(gl.getUniformLocation(pr,'mvp'),false,M4.mul(pj,mv));
    gl.drawArrays(gl.TRIANGLES,0,v.length/3);
  }
  let drag=null;cv.style.touchAction='none';cv.style.cursor='grab';
  cv.addEventListener('pointerdown',e=>{cv.setPointerCapture(e.pointerId);drag=[e.clientX,e.clientY]});
  cv.addEventListener('pointerup',()=>drag=null);
  cv.addEventListener('pointermove',e=>{if(!drag)return;
    yaw-=(e.clientX-drag[0])*0.01;pitch=Math.max(-1.5,Math.min(1.5,pitch+(e.clientY-drag[1])*0.01));
    drag=[e.clientX,e.clientY];draw()});
  cv.addEventListener('wheel',e=>{e.preventDefault();dist=Math.max(rad*0.4,Math.min(rad*12,dist*Math.exp(e.deltaY*0.001)));draw()},{passive:false});
  draw();
  return{dims:[0,1,2].map(k=>(mx[k]-mn[k]).toFixed(1)).join(' × ')};
}
async function api(url,body){
  const r=await fetch(url,{method:'POST',body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){alert(j.error||'Erreur');return null}
  return j}
function fbar(){
  const b=$('#fbar');if(!folder){b.style.display='none';return}
  b.style.display='flex';
  b.innerHTML=`<b>📁 ${esc(folder)}</b><button id="frn">✏ Renommer</button><button id="fdel">🗑 Supprimer</button>`;
  $('#frn').onclick=async()=>{const n=prompt('Nouveau nom du dossier',folder.split('/').pop());if(!n)return;
    const j=await api('/api/folders/rename',{path:folder,name:n});if(j){folder=j.path;load()}};
  $('#fdel').onclick=async()=>{if(!confirm('Supprimer le dossier '+folder+' ? (seulement s\'il est vide)'))return;
    const j=await api('/api/folders/delete',{path:folder});if(j){folder=folder.split('/').slice(0,-1).join('/');load()}};
}
async function loadTree(){
  const d=await (await fetch('/api/folders')).json();FOLDERS=d.folders.map(x=>x.path);
  $('#fl').innerHTML=FOLDERS.map(p=>`<option value="${esc(p)}">`).join('');
  const row=(p,label,n,dep)=>`<div class="fi ${p===folder?'on':''}" data-f="${esc(p)}" style="padding-left:${dep*14+8}px">${label} <span class="mut">${n}</span></div>`;
  $('#tree').innerHTML=row('','📚 Tout',d.total,0)+d.folders.map(x=>row(x.path,'📁 '+esc(x.path.split('/').pop()),x.count,x.path.split('/').length)).join('');
  fbar();
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
  $('#box').innerHTML=`<div id="pv"><div id="pvbox">${cur.has_thumb?`<img src="/thumb/${cur.id}">`:''}</div>
  <div class="row" style="margin-top:8px;align-items:center"><button id="v3d">🧊 Voir en 3D</button><span class="mut" id="pvmsg"></span></div></div>
  <div class="f"><b>${esc(cur.name)}</b><span class="mut">${esc(cur.path)} · ${(cur.size/1048576).toFixed(2)} Mo</span>
  <div class="row"><a class="btn" href="${bs}">🖨 Ouvrir dans Bambu Studio</a><a class="btn sec" href="${url}">Télécharger</a></div>
  <label>Tags (séparés par des virgules)<input id="etags" style="width:100%" value="${esc(cur.tags.join(', '))}"></label>
  <label>Nom du fichier<input id="ename" style="width:100%" value="${esc(cur.name)}"></label>
  <label>Dossier<input id="efolder" list="fl" style="width:100%" value="${esc(cur.folder)}" placeholder="(racine)"></label>
  <label>Description<textarea id="edesc">${esc(cur.description)}</textarea></label>
  <div class="row"><button class="pri" id="save">Enregistrer</button><button id="close">Fermer</button><button id="del" style="margin-left:auto">🗑 Supprimer</button></div></div>`;
  $('#modal').classList.add('on');
  $('#close').onclick=close;
  let on3d=false;
  $('#v3d').onclick=async()=>{
    const box=$('#pvbox'),msg=$('#pvmsg');
    if(on3d){box.innerHTML=cur.has_thumb?`<img src="/thumb/${cur.id}">`:'';$('#v3d').textContent='🧊 Voir en 3D';msg.textContent='';on3d=false;return}
    msg.textContent='Chargement…';
    const r=await fetch('/api/mesh/'+cur.id);
    if(!r.ok){const j=await r.json().catch(()=>({}));msg.textContent=j.error||'Erreur';return}
    const buf=await r.arrayBuffer();
    box.innerHTML='<canvas style="width:100%;aspect-ratio:1;background:#2b2f38;border-radius:10px;display:block"></canvas>';
    const res=view3d(box.firstChild,buf);
    if(res.err){box.innerHTML=cur.has_thumb?`<img src="/thumb/${cur.id}">`:'';msg.textContent=res.err;return}
    on3d=true;$('#v3d').textContent='🖼 Image';msg.textContent=res.dims+' mm · glisser : tourner · molette : zoom';
  };
  $('#del').onclick=async()=>{if(!confirm('Supprimer définitivement "'+cur.name+'" du dépôt ?'))return;
    if(await api('/api/files/'+cur.id+'/delete',{})){close();load()}};
  $('#save').onclick=async()=>{
    const nn=$('#ename').value.trim();
    if(nn&&nn!==cur.name){if(!await api('/api/files/'+cur.id+'/rename',{name:nn}))return}
    const nf=$('#efolder').value.trim();
    if(nf!==cur.folder)await fetch('/api/files/'+cur.id+'/move',{method:'POST',body:JSON.stringify({folder:nf})});
    await fetch('/api/files/'+cur.id,{method:'POST',body:JSON.stringify({description:$('#edesc').value,tags:$('#etags').value.split(',')})});
    close();load()};
}
function close(){$('#modal').classList.remove('on')}
$('#imp').onclick=()=>{$('#picker').value='';$('#picker').click()};
$('#picker').onchange=()=>{
  const all=[...$('#picker').files];if(!all.length)return;
  const list=all.filter(f=>f.name.toLowerCase().endsWith('.3mf'))
    .sort((a,b)=>(a.webkitRelativePath||a.name).localeCompare(b.webkitRelativePath||b.name));
  if(!list.length){
    const ext={};all.forEach(f=>{const m=f.name.match(/[.][^.]+$/);const e=m?m[0].toLowerCase():'(sans extension)';ext[e]=(ext[e]||0)+1});
    alert('Aucun fichier .3mf trouvé parmi '+all.length+' fichier(s) du dossier.\nExtensions vues : '+Object.entries(ext).map(([e,n])=>e+' x'+n).join(', '));return}
  $('#box').innerHTML=`<div class="f" style="grid-column:1/-1"><b>${list.length} fichier(s) 3MF trouvé(s)${all.length>list.length?' ('+(all.length-list.length)+' autre(s) fichier(s) ignoré(s))':''}</b>
   <div class="row" style="align-items:center"><label>Dossier de destination <input id="dest" list="fl" value="${esc(folder)}" placeholder="(racine)"></label>
   <label><input type="checkbox" id="keep" checked> garder l'arborescence du dossier source</label></div>
   <div class="row" style="align-items:center"><button class="pri" id="goall">⚡ Tout importer (${list.length})</button>
   <button id="all">Tout cocher</button><button id="none">Tout décocher</button><span class="mut" id="sel"></span></div>
   <progress id="prog" value="0" max="1" style="width:100%;display:none"></progress>
   <div id="ilist" style="max-height:45vh;overflow:auto;display:flex;flex-direction:column;gap:4px">
   ${list.map((f,i)=>`<label class="row" style="align-items:center;gap:8px"><input type="checkbox" data-i="${i}" checked>
     <span style="flex:1;word-break:break-all">${esc(f.webkitRelativePath||f.name)}</span>
     <span class="mut">${(f.size/1048576).toFixed(1)} Mo</span><span class="mut" id="st${i}"></span></label>`).join('')}</div>
   <div class="row" style="align-items:center"><button class="pri" id="go">Transférer la sélection</button><button id="close">Fermer</button><span class="mut" id="sum"></span></div></div>`;
  $('#modal').classList.add('on');$('#close').onclick=()=>{close();load()};
  const boxes=()=>[...document.querySelectorAll('#ilist input')];
  const upd=()=>$('#sel').textContent=boxes().filter(b=>b.checked&&!b.disabled).length+' sélectionné(s)';
  $('#ilist').onchange=upd;upd();
  $('#all').onclick=()=>{boxes().forEach(b=>{if(!b.disabled)b.checked=true});upd()};
  $('#none').onclick=()=>{boxes().forEach(b=>b.checked=false);upd()};
  const run=async(items)=>{
    if(!items.length)return;
    const dest=$('#dest').value.trim(),keep=$('#keep').checked;
    $('#go').disabled=$('#goall').disabled=true;
    const pr=$('#prog');pr.style.display='block';pr.max=items.length;pr.value=0;
    const cnt={added:0,dup:0,err:0};let idx=0;
    const worker=async()=>{while(idx<items.length){
      const i=items[idx++],b=document.querySelector('#ilist input[data-i="'+i+'"]'),st=$('#st'+i),f=list[i];
      b.disabled=true;st.textContent='⏳';
      const rp=(f.webkitRelativePath||'').split('/');rp.pop();rp.shift();
      const target=[dest,keep?rp.join('/'):''].filter(Boolean).join('/');
      try{
        const r=await fetch('/api/upload',{method:'POST',headers:{'X-Filename':encodeURIComponent(f.name),'X-Folder':encodeURIComponent(target)},body:f});
        const j=await r.json().catch(()=>({}));
        if(r.ok&&j.status==='added'){st.textContent='✅ ajouté';cnt.added++}
        else if(r.ok&&j.status==='duplicate'){st.textContent='↩ déjà présent';cnt.dup++}
        else{st.textContent='❌ '+(j.error||'erreur '+r.status)+(r.status===403?' (recharge la page : Ctrl+F5)':'');cnt.err++;b.disabled=false;b.checked=true}
      }catch(e){st.textContent='❌ lecture impossible (fichier verrouillé ou réseau coupé ?)';cnt.err++;b.disabled=false;b.checked=true}
      pr.value++;$('#sum').textContent=pr.value+' / '+items.length;
    }};
    await Promise.all([worker(),worker(),worker()]);
    $('#sum').textContent='Terminé : '+cnt.added+' ajouté(s), '+cnt.dup+' déjà présent(s), '+cnt.err+' erreur(s)';
    $('#go').disabled=$('#goall').disabled=false;upd();load();
  };
  $('#go').onclick=()=>run(boxes().filter(b=>b.checked&&!b.disabled).map(b=>+b.dataset.i));
  $('#goall').onclick=()=>{boxes().forEach(b=>{if(!b.disabled)b.checked=true});run(boxes().filter(b=>!b.disabled).map(b=>+b.dataset.i))};
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
    ap.add_argument("--max-mb", type=int, default=2048, help="taille max d'un fichier importé (Mo)")
    a = ap.parse_args()
    global MAX_UPLOAD
    MAX_UPLOAD = a.max_mb << 20
    ROOT = os.path.abspath(a.root)
    os.makedirs(ROOT, exist_ok=True)
    DB = os.path.join(ROOT, ".library.db")
    init_db()
    print("Indexation de %s : %d fichier(s)" % (ROOT, scan()))
    print("http://%s:%d" % ("localhost" if a.host == "0.0.0.0" else a.host, a.port))
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
