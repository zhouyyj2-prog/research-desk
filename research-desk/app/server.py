# -*- coding: utf-8 -*-
"""
研究台 · 本地服务
只用 Python 标准库（3.8+）。双击“启动研究台.command”运行，浏览器打开 http://127.0.0.1:8765
数据全部存在 ../data/ 下：library.db（SQLite）、config.json（API Key）、files/（上传的文件）、inbox/（定时任务投递）
"""
import base64, datetime, hmac, secrets, socket, time, glob, hashlib, html, io, json, mimetypes, os, re, shutil, sqlite3, subprocess
import sys, tempfile, threading, time, traceback, urllib.error, urllib.parse, urllib.request, uuid, zipfile
import concurrent.futures
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP_DIR)
DATA = os.path.join(ROOT, "data")
FILES = os.path.join(DATA, "files")
INBOX = os.path.join(DATA, "inbox")
DONE = os.path.join(INBOX, "done")
STATIC = os.path.join(APP_DIR, "static")
DB_PATH = os.path.join(DATA, "library.db")
CFG_PATH = os.path.join(DATA, "config.json")
HOME = os.path.expanduser("~")
PORT = int(os.environ.get("RD_PORT", "8765"))

DEFAULT_CFG = {
    "provider": "deepseek",
    "baseUrl": "https://api.deepseek.com",
    "apiKey": "",
    "model": "deepseek-flash",
    "deepModel": "deepseek-v4-pro",
    "thinking": False,
    # 下面几个文件夹都可以在“设置”里改；默认放在程序文件夹旁边，没有也不影响启动
    "zoteroDir": "",  # 用 Zotero 的人在“设置”里填（Mac 一般是 ~/Zotero）；留空就不同步
    "meetingDir": "",  # 组会汇报文件夹，选填；留空就不读
    "paperDir": os.path.join(ROOT, "论文库"),
    "briefDir": os.path.join(ROOT, "每日简报"),
    "pdf2zhDir": "",  # 选填：已经装了 zotero-pdf2zh 的，填它的 server 文件夹；一般不用填
    # 我的研究方向（一句话，AI 解读论文时会结合它）和论文分类，都在“设置”里填，默认为空
    "focus": "",
    "dirs": [],
}
COLLECTIONS = ["papers", "ideas", "briefs", "collections", "topics", "topicDocs", "translations", "fulltext", "plans"]
for d in (DATA, FILES, INBOX, DONE):
    os.makedirs(d, exist_ok=True)

LOCK = threading.RLock()

# macOS 从终端启动时文件句柄上限只有 256；pdf2zh_next 翻整篇论文会超过它（Too many open files），这里调高，子进程会继承
try:
    import resource
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    _want = 10240 if _hard == resource.RLIM_INFINITY else min(10240, _hard)
    if _soft < _want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (_want, _hard))
except Exception:
    pass


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- config
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_cfg(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(CFG_PATH, 0o600)
    except Exception:
        pass


# ---------------------------------------------------------------- database
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS docs (col TEXT, id TEXT, data TEXT, updated TEXT, PRIMARY KEY(col,id))")
    return c


def all_docs(col):
    with LOCK:
        c = db()
        rows = c.execute("SELECT id,data FROM docs WHERE col=?", (col,)).fetchall()
        c.close()
    out = []
    for i, d in rows:
        try:
            x = json.loads(d)
        except Exception:
            x = {}
        x["id"] = i
        out.append(x)
    return out


def get_doc(col, id):
    with LOCK:
        c = db()
        r = c.execute("SELECT data FROM docs WHERE col=? AND id=?", (col, id)).fetchone()
        c.close()
    return json.loads(r[0]) if r else None


def set_doc(col, id, data):
    data = {k: v for k, v in data.items() if k != "id"}
    with LOCK:
        c = db()
        c.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)", (col, id, json.dumps(data, ensure_ascii=False), now()))
        c.commit()
        c.close()
    mark_dirty()


def merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            merge(a[k], v)
        elif v is None:
            a.pop(k, None)
        else:
            a[k] = v
    return a


def update_doc(col, id, patch):
    with LOCK:
        cur = get_doc(col, id)
        if cur is None:
            raise KeyError(id)
        set_doc(col, id, merge(cur, patch))


def del_doc(col, id):
    with LOCK:
        c = db()
        c.execute("DELETE FROM docs WHERE col=? AND id=?", (col, id))
        c.commit()
        c.close()
    mark_dirty()


# snapshot.json：给每天的定时任务读（去重、创新点状态、组会专题）
_dirty = threading.Event()


def mark_dirty():
    _dirty.set()


def snapshot_loop():
    while True:
        _dirty.wait()
        time.sleep(2)
        _dirty.clear()
        try:
            write_snapshot()
        except Exception:
            traceback.print_exc()


def write_snapshot():
    papers = [{k: p.get(k) for k in ("id", "arxiv", "title", "kind", "readAt", "direction", "briefDate", "source", "pdfName", "tags")} for p in all_docs("papers")]
    ideas = [{k: i.get(k) for k in ("id", "date", "title", "kind", "direction", "source", "status", "note")} for i in all_docs("ideas")]
    topics = [{k: t.get(k) for k in ("id", "name", "date", "status", "folder", "mainPaper")} for t in all_docs("topics")]
    briefs = sorted([{"id": b["id"], "deepDirection": (b.get("deepRead") or {}).get("direction")} for b in all_docs("briefs")], key=lambda x: x["id"])
    snap = {"updated": now(), "papers": papers, "ideas": ideas, "topics": topics, "briefs": briefs}
    tmp = os.path.join(DATA, "snapshot.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(DATA, "snapshot.json"))


# ---------------------------------------------------------------- file access
def allowed_roots():
    cfg = load_cfg()
    roots = [FILES, cfg.get("zoteroDir"), cfg.get("meetingDir"), cfg.get("paperDir"), cfg.get("briefDir"),
             (os.path.join(pdf2zh_paths()[0], "translated") if pdf2zh_paths()[0] else None)]
    return [os.path.realpath(os.path.expanduser(r)) for r in roots if r]


def path_ok(p):
    rp = os.path.realpath(p)
    return any(rp == r or rp.startswith(r + os.sep) for r in allowed_roots())


def path_token(p):
    return "p-" + base64.urlsafe_b64encode(p.encode("utf-8")).decode("ascii").rstrip("=")


def token_path(tok):
    if tok.startswith("p-"):
        s = tok[2:]
        s += "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8")
    if re.fullmatch(r"[A-Za-z0-9_\-.]+", tok):
        hits = glob.glob(os.path.join(FILES, tok + "*"))
        return hits[0] if hits else os.path.join(FILES, tok)
    return None


# ---------------------------------------------------------------- office text
def docx_text(path):
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for p in root.iter(W + "p"):
        t = "".join(x.text or "" for x in p.iter(W + "t")).strip()
        if not t:
            continue
        st = p.find(W + "pPr/" + W + "pStyle")
        if st is not None and "heading" in (st.get(W + "val") or "").lower():
            t = "## " + t
        lines.append(t)
    return "\n".join(lines)


def pptx_text(path):
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted([n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)], key=lambda n: int(re.findall(r"\d+", n)[0]))
        for i, n in enumerate(names, 1):
            root = ET.fromstring(z.read(n))
            paras = ["".join(t.text or "" for t in p.iter(A + "t")).strip() for p in root.iter(A + "p")]
            out.append("【第 %d 页】\n" % i + "\n".join(x for x in paras if x))
    return "\n\n".join(out)


def find_soffice():
    for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice", shutil.which("soffice") or "", shutil.which("libreoffice") or ""):
        if p and os.path.exists(p):
            return p
    return None


def pptx_to_pdf(path):
    so = find_soffice()
    if not so:
        return None
    h = hashlib.md5((path + str(os.path.getmtime(path))).encode()).hexdigest()[:12]
    out = os.path.join(FILES, "slides-" + h + ".pdf")
    if os.path.exists(out):
        return out
    tmpd = tempfile.mkdtemp()
    try:
        subprocess.run([so, "--headless", "--convert-to", "pdf", "--outdir", tmpd, path], timeout=180, capture_output=True)
        pdfs = glob.glob(os.path.join(tmpd, "*.pdf"))
        if pdfs:
            shutil.move(pdfs[0], out)
            return out
    except Exception:
        traceback.print_exc()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    return None


# ---------------------------------------------------------------- Zotero sync
def norm_title(t):
    return re.sub(r"[^a-z0-9一-鿿]", "", re.sub(r"<[^>]+>", "", t or "").lower())[:80]


def clean(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t or "")).strip()


def strip_html(h):
    h = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>", "\n", h or "")
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


def guess_direction(text):
    """在标题和 Zotero 分类名里找用户自己设置的研究方向名，找到就归进去；找不到留空（“未归入方向”）。"""
    t = (text or "").lower()
    for d in load_cfg().get("dirs") or []:
        d = str(d).strip()
        if d and d != "其他" and d.lower() in t:
            return d
    return None


def zotero_sync():
    cfg = load_cfg()
    zdir = (cfg.get("zoteroDir") or "").strip()
    if not zdir:
        return {"ok": True, "skipped": True}
    zdir = os.path.expanduser(zdir)
    src = os.path.join(zdir, "zotero.sqlite")
    if not os.path.exists(src):
        return {"ok": False, "error": "找不到 Zotero 数据库：" + src}
    tmpd = tempfile.mkdtemp()
    try:
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(src + suf):
                shutil.copy(src + suf, os.path.join(tmpd, "z.sqlite" + suf))
        c = sqlite3.connect(os.path.join(tmpd, "z.sqlite"))
        c.row_factory = sqlite3.Row
        q = lambda s, a=(): c.execute(s, a).fetchall()
        deleted = {r[0] for r in q("SELECT itemID FROM deletedItems")}
        cols = {r["collectionID"]: dict(name=r["collectionName"], parent=r["parentCollectionID"], key=r["key"]) for r in q("SELECT * FROM collections")}

        def cpath(cid):
            n = []
            while cid:
                n.append(cols[cid]["name"]); cid = cols[cid]["parent"]
            return "/".join(reversed(n))

        # collections
        existing_cols = {x["id"]: x for x in all_docs("collections")}
        for cid, cinfo in cols.items():
            did = "zc-" + cinfo["key"]
            doc = dict(name=cinfo["name"], parent=("zc-" + cols[cinfo["parent"]]["key"]) if cinfo["parent"] else None, source="Zotero", order=1000 + cid)
            old = existing_cols.get(did)
            if not old or any(old.get(k) != v for k, v in doc.items()):
                set_doc("collections", did, dict(old or {}, **doc))
        items = q("SELECT i.itemID,i.key,i.dateAdded,it.typeName FROM items i JOIN itemTypes it USING(itemTypeID) WHERE it.typeName NOT IN ('attachment','note','annotation')")
        groups = {}
        for r in items:
            if r["itemID"] in deleted or r["typeName"] == "computerProgram":
                continue
            f = {x["fieldName"]: x["value"] for x in q("SELECT f.fieldName,v.value FROM itemData d JOIN fields f USING(fieldID) JOIN itemDataValues v USING(valueID) WHERE d.itemID=?", (r["itemID"],))}
            cr = q("SELECT c.firstName,c.lastName,ct.creatorType FROM itemCreators ic JOIN creators c USING(creatorID) JOIN creatorTypes ct USING(creatorTypeID) WHERE ic.itemID=? ORDER BY ic.orderIndex", (r["itemID"],))
            txt = " ".join([f.get("url", ""), f.get("DOI", ""), f.get("extra", ""), f.get("archiveID", "")])
            m = re.search(r"(?:arxiv[.:/ ]*(?:abs/|pdf/)?|10\.48550/arXiv\.)(\d{4}\.\d{4,5})", txt, re.I)
            if not m and "arxiv" in f.get("url", "").lower():
                m = re.search(r"\b(\d{4}\.\d{4,5})", f.get("url", ""))
            atts = []
            for a in q("SELECT i.itemID,i.key,ia.path,ia.contentType FROM itemAttachments ia JOIN items i USING(itemID) WHERE ia.parentItemID=?", (r["itemID"],)):
                if a["itemID"] in deleted:
                    continue
                p = a["path"] or ""
                fp = os.path.join(zdir, "storage", a["key"], p[8:]) if p.startswith("storage:") else p
                ann = [dict(text=(n["text"] or "").strip()[:2000], comment=(n["comment"] or "").strip()[:1000], color=n["color"] or "", page=n["pageLabel"] or "")
                       for n in q("SELECT text,comment,color,pageLabel FROM itemAnnotations WHERE parentItemID=? ORDER BY sortIndex", (a["itemID"],)) if (n["text"] or n["comment"])]
                atts.append(dict(path=fp, ctype=a["contentType"], ann=ann, exists=bool(fp) and os.path.exists(fp)))
            notes = [strip_html(x[0]) for x in q("SELECT note FROM itemNotes WHERE parentItemID=?", (r["itemID"],)) if x[0]]
            it = dict(key=r["key"], added=r["dateAdded"], title=f.get("title", ""), authors=", ".join((x["firstName"] + " " + x["lastName"]).strip() for x in cr if x["creatorType"] in ("author", "programmer", "editor")),
                      date=f.get("date", ""), venue=f.get("publicationTitle") or f.get("proceedingsTitle") or f.get("repository") or f.get("conferenceName", ""), doi=f.get("DOI", ""), url=f.get("url", ""),
                      abstract=f.get("abstractNote", ""), arxiv=m.group(1) if m else "", tags=[x[0] for x in q("SELECT t.name FROM itemTags it JOIN tags t USING(tagID) WHERE it.itemID=?", (r["itemID"],))],
                      colids=[x[0] for x in q("SELECT collectionID FROM collectionItems WHERE itemID=?", (r["itemID"],))], atts=atts, notes=notes)
            groups.setdefault(norm_title(it["title"]), []).append(it)
        c.close()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    existing = {p["id"]: p for p in all_docs("papers")}
    ignored = {d["id"] for d in all_docs("ignored")}  # 用户在文库里删除过的条目，不再从 Zotero 重新导入
    by_key = {}
    for pid, p in existing.items():
        for k in p.get("zoteroKeys") or []:
            by_key[k] = pid
    added = updated = 0
    for g in groups.values():
        g.sort(key=lambda i: (not i["arxiv"], -sum(len(a["ann"]) for a in i["atts"]), -len(i["atts"])))
        p0 = g[0]
        ax = next((i["arxiv"] for i in g if i["arxiv"]), "")
        pid = next((by_key[i["key"]] for i in g if i["key"] in by_key), None) or (ax if ax else "z-" + p0["key"])
        if pid in ignored:
            continue
        pdfs = [a for i in g for a in i["atts"] if a["ctype"] == "application/pdf" and a["exists"]]
        is_tr = lambda a: bool(re.search(r"\.(compare|no_watermark|dual|mono|cut)\b", os.path.basename(a["path"])))
        orig = [a for a in pdfs if not is_tr(a)]
        tr = sorted([a for a in pdfs if is_tr(a) and ".cut." not in a["path"]], key=lambda a: (0 if ("dual" in a["path"] or "compare" in a["path"]) else 1, -len(a["ann"])))
        o = max(orig, key=lambda a: len(a["ann"])) if orig else (tr[0] if tr else None)
        t = tr[0] if (tr and orig) else None
        ex = []
        for i in g:
            for a in i["atts"]:
                on = "双语版" if is_tr(a) else "原文"
                for n in a["ann"]:
                    ex.append(dict(n, source="Zotero", on=on))
        colids = sorted({x for i in g for x in i["colids"]})
        zdoc = dict(title=clean(p0["title"]), authors=next((i["authors"] for i in g if i["authors"]), ""), date=next((i["date"] for i in g if i["date"]), "")[:10],
                    venue=clean(next((i["venue"] for i in g if i["venue"]), "")), doi=next((i["doi"] for i in g if i["doi"]), ""), arxiv=ax,
                    abstract=next((i["abstract"] for i in g if i["abstract"]), "")[:8000], source="Zotero", zoteroKeys=[i["key"] for i in g],
                    zoteroCollections=sorted({cpath(x) for x in colids if x in cols}), zoteroTags=sorted({tg for i in g for tg in i["tags"]}),
                    zoteroNotes="\n\n---\n\n".join(n for i in g for n in i["notes"])[:60000])
        url = next((i["url"] for i in g if i["url"]), "")
        zdoc["link"] = ("https://arxiv.org/abs/" + ax) if ax else (url or (("https://doi.org/" + zdoc["doi"]) if zdoc["doi"] else ""))
        if o:
            zdoc.update(assetId=path_token(o["path"]), pdfName=os.path.basename(o["path"]), pdfSize=os.path.getsize(o["path"]), pdfPath=o["path"])
        if t:
            zdoc["pdfAlt"] = dict(assetId=path_token(t["path"]), name=os.path.basename(t["path"]), size=os.path.getsize(t["path"]), label="中文对照", path=t["path"])
        zcols = ["zc-" + cols[x]["key"] for x in colids if x in cols]
        old = existing.get(pid)
        if old:
            new = dict(old)
            new.update(zdoc)
            user_ex = [e for e in (old.get("excerpts") or []) if e.get("source") != "Zotero"]
            new["excerpts"] = ex + user_ex
            mine = [c for c in (old.get("collections") or []) if not c.startswith("zc-")]
            new["collections"] = zcols + mine
            new["tags"] = sorted(set(old.get("tags") or []) | set(zdoc["zoteroTags"]))
            if new != old:
                set_doc("papers", pid, new); updated += 1
        else:
            text = zdoc["title"] + " " + " ".join(zdoc["zoteroCollections"])
            d = dict(zdoc, direction=guess_direction(text) or "", kind="已读" if ex else "待读",
                     addedAt=min(i["added"] for i in g).replace(" ", "T") + "Z", tags=list(zdoc["zoteroTags"]), collections=zcols, notes="", excerpts=ex)
            set_doc("papers", pid, d); added += 1
    return {"ok": True, "added": added, "updated": updated, "groups": len(groups)}


# ---------------------------------------------------------------- 组会文件夹
SKIP_DIRS = {".git", ".venv", "__pycache__", "raw_logs", "raw", "node_modules", ".idea", "latents", "images", "taming", "ldm", "open_clip", "pytorch_fid"}


def slug(s):
    s2 = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s2[:40] or hashlib.md5(s.encode()).hexdigest()[:10]


def meeting_scan():
    cfg = load_cfg()
    base = os.path.expanduser((cfg.get("meetingDir") or "").strip())
    if not base:
        return {"ok": True, "skipped": True}
    if not os.path.isdir(base):
        return {"ok": False, "error": "找不到组会文件夹：" + base}
    topics = {t["id"]: t for t in all_docs("topics")}
    by_folder = {}
    for t in topics.values():
        if t.get("folderAbs"):
            by_folder[os.path.realpath(t["folderAbs"])] = t["id"]
        elif t.get("folder"):
            by_folder[os.path.realpath(os.path.join(HOME, "Desktop", t["folder"]))] = t["id"]
    by_name = {}
    for t in topics.values():
        fa = t.get("folderAbs") or t.get("folder") or ""
        if fa:
            by_name.setdefault(os.path.basename(fa.rstrip("/")).strip(), t["id"])
    tdocs = {d["id"]: d for d in all_docs("topicDocs")}
    known_paths = {}
    for d in tdocs.values():
        if d.get("absPath"):
            known_paths[d["absPath"]] = d
    rel_known = {}
    for d in tdocs.values():
        if d.get("topicId") and d.get("path"):
            rel_known[(d["topicId"], d["path"])] = d
    new_topics = new_docs = 0
    papers = all_docs("papers")
    for name in sorted(os.listdir(base)):
        folder = os.path.join(base, name)
        if not os.path.isdir(folder) or name.startswith("."):
            continue
        tid = by_folder.get(os.path.realpath(folder))
        if not tid and name.strip() in by_name:
            tid = by_name[name.strip()]
            update_doc("topics", tid, {"folderAbs": folder})
        files = []
        for dp, dns, fns in os.walk(folder):
            dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".") and not os.path.isdir(os.path.join(dp, d, ".git"))]
            depth = dp[len(folder):].count(os.sep)
            if depth > 3:
                dns[:] = []
            for fn in fns:
                if fn.startswith("~$") or fn.startswith("."):
                    continue
                if fn.lower().endswith((".md", ".docx", ".pptx", ".pdf")):
                    files.append(os.path.join(dp, fn))
        if not tid:
            tid = "t-" + slug(name)
            if tid in topics:
                tid += "-" + hashlib.md5(folder.encode()).hexdigest()[:4]
            pptx = [f for f in files if f.lower().endswith(".pptx")]
            date = ""
            if pptx:
                try:
                    first = pptx_text(pptx[0])[:600]
                    m = re.search(r"(20\d\d)[.\-/年](\d{1,2})(?:[.\-/月](\d{1,2}))?", first)
                    if m:
                        date = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3) or 1)) if m.group(3) else "%s-%02d" % (m.group(1), int(m.group(2)))
                except Exception:
                    pass
            main = ""
            nt = norm_title(name)
            for p in papers:
                pt = norm_title(p.get("title"))
                if len(nt) >= 8 and (nt in pt or (len(pt) >= 8 and pt[:30] in nt)):
                    main = p["id"]; break
            if not main:
                low = name.strip().lower()
                for p in papers:
                    pn = (p.get("pdfName") or "").lower()
                    if pn and (pn.startswith(low + "_") or pn.startswith(low + ".") or pn.startswith(low + "-")):
                        main = p["id"]; break
            today = datetime.date.today().isoformat()
            status = "准备中" if not date else ("已汇报" if date[:10] < today else ("本次组会" if date[:10] == today else "准备中"))
            set_doc("topics", tid, dict(name=name, short=name, date=date, status=status, presenter="", venue="", mainPaper=main, papers=[main] if main else [],
                                        background=[], folderAbs=folder, folder=os.path.relpath(folder, os.path.join(HOME, "Desktop")), summary="（由程序扫描文件夹自动创建，可在下方补充说明。）",
                                        questions=[], todos=[], files=[os.path.relpath(f, folder) for f in files[:60]], images=[], discussion="", notesFromMeeting="", order=int(time.time())))
            topics[tid] = get_doc("topics", tid); topics[tid]["id"] = tid
            new_topics += 1
        t = topics[tid]
        # slides
        pptx = sorted([f for f in files if f.lower().endswith(".pptx")], key=os.path.getmtime, reverse=True)
        if pptx:
            pdf = pptx_to_pdf(pptx[0])
            sl = t.get("slides") or {}
            if pdf and sl.get("src") != pptx[0] + "@" + str(int(os.path.getmtime(pptx[0]))):
                update_doc("topics", tid, {"slides": {"assetId": path_token(pdf), "name": os.path.basename(pptx[0]) + "（已转为 PDF）", "src": pptx[0] + "@" + str(int(os.path.getmtime(pptx[0]))), "pptxPath": pptx[0]}})
            elif not pdf and not sl.get("assetId"):
                update_doc("topics", tid, {"slides": {"name": os.path.basename(pptx[0]), "pptxPath": pptx[0]}})
            elif sl.get("assetId") and not sl.get("pptxPath"):
                update_doc("topics", tid, {"slides": {"pptxPath": pptx[0]}})
        # materials
        for f in files:
            low = f.lower()
            if low.endswith(".pdf"):
                continue
            mt = int(os.path.getmtime(f))
            kd = known_paths.get(f) or rel_known.get((tid, os.path.relpath(f, folder)))
            if kd and kd.get("mtime", 0) >= mt:
                continue
            try:
                if low.endswith(".md"):
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    kind = "idea" if ("思路" in f or "idea" in low) else "result"
                elif low.endswith(".docx"):
                    text = docx_text(f); kind = "qa" if "问答" in f else ("script" if "逐字稿" in f else "doc")
                else:
                    text = pptx_text(f); kind = "slides"
            except Exception as e:
                text = "（读取失败：%s）" % e; kind = "doc"
            title = os.path.splitext(os.path.basename(f))[0]
            if kind == "slides": title = "PPT 文字大纲 · " + title
            did = kd["id"] if kd else "m-" + hashlib.md5(f.encode()).hexdigest()[:10]
            set_doc("topicDocs", did, dict(topicId=tid, title=title, kind=kind, path=os.path.relpath(f, folder), absPath=f, mtime=mt, text=text[:200000], order=(kd or {}).get("order", 100 + new_docs)))
            new_docs += 1
    return {"ok": True, "newTopics": new_topics, "docs": new_docs}


def link_existing_topic_docs():
    """把从网页版迁移来的 topicDocs 与本地文件对应起来，避免扫描时重复。"""
    cfg = load_cfg()
    for d in all_docs("topicDocs"):
        if d.get("absPath"):
            continue
        t = get_doc("topics", d.get("topicId", ""))
        if not t:
            continue
        cands = []
        folder = os.path.join(HOME, "Desktop", t.get("folder", "")) if t.get("folder") else None
        if folder:
            cands.append(os.path.join(folder, d.get("path", "")))
            cands.append(os.path.join(os.path.dirname(folder), os.path.basename(d.get("path", ""))))
            if d.get("path", "").startswith("组会汇报/"):
                cands.append(os.path.join(cfg["meetingDir"], d["path"].split("/", 1)[1]))
        for c in cands:
            if c and os.path.isfile(c):
                update_doc("topicDocs", d["id"], {"absPath": c, "mtime": int(os.path.getmtime(c))})
                break
        if t.get("folder") and not t.get("folderAbs") and folder and os.path.isdir(folder):
            update_doc("topics", t["id"], {"folderAbs": folder})


# ---------------------------------------------------------------- 论文库 PDF
def paper_dir_scan():
    cfg = load_cfg()
    base = cfg["paperDir"]
    if not os.path.isdir(base):
        return {"ok": True, "linked": 0}
    papers = {p["id"]: p for p in all_docs("papers")}
    by_ax = {p.get("arxiv"): p for p in papers.values() if p.get("arxiv")}
    linked = created = 0
    for dp, dns, fns in os.walk(base):
        for fn in fns:
            if not fn.lower().endswith(".pdf"):
                continue
            fp = os.path.join(dp, fn)
            if any(p.get("pdfPath") == fp for p in papers.values()):
                continue
            m = re.search(r"\d{4}\.\d{4,5}", fn)
            p = by_ax.get(m.group(0)) if m else None
            sub = os.path.relpath(dp, base).split(os.sep)[0]
            if p:
                if not p.get("assetId") or not p.get("pdfPath") or not os.path.exists(p.get("pdfPath", "")):
                    update_doc("papers", p["id"], {"assetId": path_token(fp), "pdfName": fn, "pdfSize": os.path.getsize(fp), "pdfPath": fp}); linked += 1
            else:
                pid = m.group(0) if m else "u-" + hashlib.md5(fp.encode()).hexdigest()[:10]
                if pid in papers:
                    continue
                set_doc("papers", pid, dict(title=("arXiv:" + m.group(0) + "（待补标题）") if m else os.path.splitext(fn)[0], authors="", date="", arxiv=m.group(0) if m else "",
                                            link=("https://arxiv.org/abs/" + m.group(0)) if m else "", direction=(sub if sub not in (".", "") else ""),
                                            kind="待读", addedAt=now(), tags=[], collections=[], notes="", excerpts=[], assetId=path_token(fp), pdfName=fn, pdfSize=os.path.getsize(fp), pdfPath=fp))
                created += 1
    return {"ok": True, "linked": linked, "created": created}


# ---------------------------------------------------------------- 全文中英对照翻译（调用本机 zotero-pdf2zh 的 pdf2zh_next）
PDF2ZH_SERVICES = ("siliconflowfree", "openai", "aliyundashscope", "google", "bing", "deepl", "deepseek", "ollama", "xinference", "azureopenai",
                   "modelscope", "zhipu", "siliconflow", "tencentmechinetranslation", "gemini", "azure", "anythingllm", "dify", "grok", "groq",
                   "qwenmt", "openaicompatible", "claudecode")
TR_JOBS = {}
TR_STAGES = [("parse pdf", 5, "解析 PDF"), ("layout", 10, "识别版面"), ("paragraph", 15, "整理段落"), ("term extraction", 20, "提取术语"),
             ("start to translate", 25, "翻译段落"), ("translate paragraphs", 30, "翻译段落"), ("typesetting", 85, "排版"),
             ("save pdf", 95, "保存 PDF"), ("finish translate", 98, "收尾")]
ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


TRANSLATOR = os.path.join(ROOT, "translator")  # “安装全文翻译”脚本把翻译程序装在这里


def _venv_exe(venv):
    # Mac / Linux 在 bin/ 下；Windows 在 Scripts\ 下，文件名带 .exe
    return os.path.join(venv, "Scripts", "pdf2zh_next.exe") if sys.platform.startswith("win") else os.path.join(venv, "bin", "pdf2zh_next")


def pdf2zh_paths():
    """按顺序找翻译程序：① 研究台自带的 translator/；② 设置里填的 zotero-pdf2zh；③ 默认位置的 zotero-pdf2zh；④ 系统 PATH。"""
    cands = [(TRANSLATOR, _venv_exe(os.path.join(TRANSLATOR, "venv")), os.path.join(TRANSLATOR, "config.toml"))]
    for b in (os.path.expanduser((load_cfg().get("pdf2zhDir") or "").strip()), os.path.join(HOME, "zotero-pdf2zh", "server")):
        if b:
            cands.append((b, _venv_exe(os.path.join(b, "zotero-pdf2zh-next-venv")), os.path.join(b, "config", "config.toml")))
    for base, exe, conf in cands:
        if os.path.exists(exe):
            return base, exe, conf
    w = shutil.which("pdf2zh_next")
    if w:
        return "", w, os.path.join(TRANSLATOR, "config.toml")
    return "", cands[0][1], cands[0][2]


def pdf2zh_service(conf):
    # config.toml 顶部（第一个 [section] 之前）里值为 true 的翻译服务，就是 Zotero 插件当前选用的
    try:
        for line in open(conf, encoding="utf-8"):
            if line.strip().startswith("["):
                break
            m = re.match(r"\s*(\w+)\s*=\s*true\b", line)
            if m and m.group(1) in PDF2ZH_SERVICES:
                return m.group(1)
    except OSError:
        pass
    return "siliconflowfree"


def find_dual(folder, stem):
    c = [f for f in glob.glob(os.path.join(folder, glob.escape(stem) + "*.pdf")) if re.search(r"(dual|compare)\.pdf$", f) and ".cut." not in f]
    # 优先用 dual（左右/上下对照），其次 compare
    return min(c, key=lambda f: (0 if "dual" in os.path.basename(f) else 1, -os.path.getmtime(f))) if c else None


def set_alt(pid, fp):
    update_doc("papers", pid, {"pdfAlt": dict(assetId=path_token(fp), name=os.path.basename(fp), size=os.path.getsize(fp), label="中英对照", path=fp)})


def tr_run(pid, src):
    job = TR_JOBS[pid]
    base, exe, conf = pdf2zh_paths()
    out = os.path.join(FILES, "translated", re.sub(r"[^\w.\-]", "_", pid))
    os.makedirs(out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    cmd = [exe, src, "--" + pdf2zh_service(conf), "--output", out, "--lang-in", "en", "--lang-out", "zh-CN",
           "--watermark-output-mode", "no_watermark", "--no-mono"]
    if os.path.exists(conf):
        cmd += ["--config-file", conf]
    env = dict(os.environ, PYTHONUNBUFFERED="1", COLUMNS="160", PYTHONIOENCODING="utf-8")
    log = []
    lf = open(os.path.join(out, "translate.log"), "a", encoding="utf-8")
    lf.write("\n==== %s %s\n" % (now(), " ".join(cmd)))
    try:
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=out, env=env)
        job["pid"] = pr.pid
        buf = b""
        while True:
            ch = os.read(pr.stdout.fileno(), 4096)
            if not ch:
                break
            buf += ch
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                ln = ANSI_RE.sub("", raw.decode("utf-8", "replace")).strip()
                if not ln:
                    continue
                log.append(ln); del log[:-40]
                lf.write(ln + "\n"); lf.flush()
                # 不在终端里运行时 rich 不刷新进度条，只能按日志里的阶段估算
                for key, pg, name in TR_STAGES:
                    if key in ln.lower() and pg > job.get("progress", 0):
                        job["progress"], job["stage"] = pg, name
                if re.search(r"\bERROR\b|BabeldocError|Too many open files|Traceback", ln):
                    job["err"] = ln[-300:]
                job["msg"] = ln[-160:]
        rc = pr.wait()
        fp = find_dual(out, stem)
        if rc == 0 and fp:
            set_alt(pid, fp)
            job.update(state="done", progress=100, msg="完成", file=os.path.basename(fp))
        else:
            # pdf2zh_next 出错时退出码也可能是 0，所以以日志里的 ERROR 为准
            job.update(state="error", msg="翻译没有完成：" + (job.get("err") or "退出码 %s，没有生成对照版 PDF" % rc), log=log[-15:])
    except Exception as e:
        job.update(state="error", msg="启动翻译失败：" + str(e), log=log[-15:])
    lf.write("==== 结束：%s %s\n" % (job.get("state"), job.get("msg"))); lf.close()
    job["ended"] = now()


def tr_start(pid):
    p = get_doc("papers", pid)
    if not p:
        return {"error": "找不到这篇论文"}
    src = p.get("pdfPath") or (token_path(p["assetId"]) if p.get("assetId") else "")
    if not src or not os.path.exists(src):
        return {"error": "这篇论文没有可用的原文 PDF"}
    j = TR_JOBS.get(pid)
    if j and j.get("state") == "running":
        return j
    base, exe, conf = pdf2zh_paths()
    # zotero-pdf2zh 以前翻译过的，直接关联，不再重复翻译
    stem = os.path.splitext(os.path.basename(src))[0]
    old = find_dual(os.path.join(base, "translated"), stem) if base else None
    if old:
        set_alt(pid, old)
        TR_JOBS[pid] = dict(state="done", progress=100, msg="已关联之前翻译好的版本", file=os.path.basename(old))
        return TR_JOBS[pid]
    if not os.path.exists(exe):
        return {"error": "还没有安装全文翻译：关掉研究台，双击 research-desk 文件夹里的「安装全文翻译」，装好后重新打开研究台再试。"}
    TR_JOBS[pid] = dict(state="running", progress=0, msg="正在启动翻译引擎…", started=now(), t0=time.time())
    threading.Thread(target=tr_run, args=(pid, src), daemon=True).start()
    return TR_JOBS[pid]


# ---------------------------------------------------------------- 相关论文推荐（Semantic Scholar，免费、无需 Key）
S2 = os.environ.get("RD_S2", "https://api.semanticscholar.org")
S2_FIELDS = "title,year,authors,externalIds,citationCount,abstract,url,venue"


def s2_get(path):
    req = urllib.request.Request(S2 + path, headers={"User-Agent": "research-desk/1.0"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < 2:  # 免费接口限速，稍等重试
                time.sleep(2 + 2 * i); continue
            if e.code == 404:
                return None
            raise


def s2_slim(p):
    if not p or not p.get("title"):
        return None
    ex = p.get("externalIds") or {}
    return dict(title=p.get("title"), year=p.get("year"), venue=p.get("venue") or "", cites=p.get("citationCount") or 0,
                authors=", ".join(a.get("name", "") for a in (p.get("authors") or [])[:4]) + (" 等" if len(p.get("authors") or []) > 4 else ""),
                arxiv=ex.get("ArXiv") or "", doi=ex.get("DOI") or "", abstract=(p.get("abstract") or "")[:1200],
                link=("https://arxiv.org/abs/" + ex["ArXiv"]) if ex.get("ArXiv") else (p.get("url") or ""))


def related(pid, force=False):
    key = "related__" + re.sub(r"[^\w.\-]", "_", pid)
    old = get_doc("translations", key)
    if old and not force and time.time() - old.get("ts", 0) < 7 * 86400:
        return old
    p = get_doc("papers", pid)
    if not p:
        return {"error": "找不到这篇论文"}
    q = urllib.parse.quote
    if p.get("arxiv"):
        sid = "arXiv:" + re.sub(r"v\d+$", "", p["arxiv"])
    elif p.get("doi"):
        sid = "DOI:" + p["doi"]
    else:
        m = s2_get("/graph/v1/paper/search/match?fields=paperId&query=" + q(p.get("title") or ""))
        if not m or not m.get("data"):
            return {"error": "在 Semantic Scholar 上没找到这篇论文（可以给它补上 arXiv 编号或 DOI）"}
        sid = m["data"][0]["paperId"]
    sid_q = q(sid, safe=":")
    out = dict(ts=time.time(), at=now(), rec=[], refs=[], cites=[])
    errs = []
    # 三个查询同时发出（以前是一个接一个，要等 20 秒左右）
    jobs = {"rec": "/recommendations/v1/papers/forpaper/%s?limit=15&fields=%s" % (sid_q, S2_FIELDS),
            "refs": "/graph/v1/paper/%s/references?limit=100&fields=%s" % (sid_q, S2_FIELDS),
            "cites": "/graph/v1/paper/%s/citations?limit=100&fields=%s" % (sid_q, S2_FIELDS)}
    names = {"rec": "推荐", "refs": "参考文献", "cites": "被引"}
    res = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {k: ex.submit(s2_get, path) for k, path in jobs.items()}
        for k, f in futs.items():
            try:
                res[k] = f.result() or {}
            except Exception as e:
                res[k] = None; errs.append("%s：%s" % (names[k], e))
    if res.get("rec") is not None:
        out["rec"] = [x for x in map(s2_slim, res["rec"].get("recommendedPapers") or []) if x]
    if res.get("refs") is not None:
        rows = res["refs"].get("data") or []
        refs = [x for x in (s2_slim(d.get("citedPaper")) for d in rows) if x]
        out["refs"] = sorted(refs, key=lambda x: -x["cites"])[:12]
        if not refs:
            out["refsNote"] = ("Semantic Scholar 没有公开这篇的参考文献列表（常见于出版方限制或刚上线的论文），可以直接看论文最后的参考文献。"
                               if not rows or res["refs"].get("citingPaperInfo") is None else "没有查到参考文献。")
    if res.get("cites") is not None:
        cs = [x for x in (s2_slim(d.get("citingPaper")) for d in res["cites"].get("data") or []) if x]
        out["cites"] = sorted(cs, key=lambda x: (-(x["year"] or 0), -x["cites"]))[:12]
        if not cs:
            out["citesNote"] = "还没有查到引用它的论文（新论文很常见）。"
    if errs and not (out["rec"] or out["refs"] or out["cites"]):
        return {"error": "连不上 Semantic Scholar（" + "；".join(errs)[:200] + "）"}
    set_doc("translations", key, out)
    return out


# ---------------------------------------------------------------- 活动记录（进度页用：阅读时长、打开论文、改阅读状态）
def log_activity(ev):
    day = datetime.date.today().isoformat()
    with LOCK:
        d = get_doc("activity", day) or {"read": {}, "events": []}
        pid = str(ev.get("pid") or "")
        if ev.get("type") == "read":
            d.setdefault("read", {})[pid] = d.get("read", {}).get(pid, 0) + int(min(max(ev.get("sec", 0), 0), 120))
        else:
            d.setdefault("events", []).append(dict(t=now(), type=ev.get("type", ""), pid=pid, v=str(ev.get("v", ""))[:40]))
            del d["events"][:-500]
        set_doc("activity", day, d)


# ---------------------------------------------------------------- inbox（定时任务投递）
# ---------------------------------------------------------------- 图解（交互式讲解页，手动导入或 AI 生成，存在 data/explainers/）
EXPL_DIR = os.path.join(DATA, "explainers")
EXPL_INDEX = os.path.join(EXPL_DIR, "index.json")


def _expl_read():
    try:
        with open(EXPL_INDEX, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def explainer_list():
    out = [dict(x, url="/explainers/" + x["file"]) for x in _expl_read() if x.get("file")]
    try:  # 兼容老位置 app/static/explainers/
        with open(os.path.join(STATIC, "explainers", "index.json"), encoding="utf-8") as fh:
            out += [dict(x, url="/static/explainers/" + x["file"], builtin=True) for x in json.load(fh) if x.get("file")]
    except (OSError, ValueError):
        pass
    return sorted(out, key=lambda x: x.get("date") or "", reverse=True)


def explainer_save(b):
    html_text = b.get("html") or ""
    if len(html_text) < 20:
        return {"error": "图解内容是空的"}
    if len(html_text) > 3 * 1024 * 1024:
        return {"error": "图解文件太大（超过 3 MB）"}
    os.makedirs(EXPL_DIR, exist_ok=True)
    eid = "e" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
    fn = eid + ".html"
    with open(os.path.join(EXPL_DIR, fn), "w", encoding="utf-8") as fh:
        fh.write(html_text)
    items = _expl_read()
    item = {"id": eid, "file": fn, "title": (b.get("title") or "未命名图解").strip()[:80], "desc": (b.get("desc") or "").strip()[:300],
            "paper": (b.get("paper") or "").strip(), "dir": (b.get("dir") or "").strip(), "source": b.get("source") or "manual",
            "date": datetime.date.today().isoformat()}
    items.append(item)
    with open(EXPL_INDEX, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    return {"ok": True, "item": dict(item, url="/explainers/" + fn)}


def explainer_delete(eid):
    items = _expl_read()
    hit = [x for x in items if x.get("id") == eid]
    if not hit:
        return {"error": "找不到这个图解（自带的图解不能在这里删除）"}
    try:
        os.remove(os.path.join(EXPL_DIR, os.path.basename(hit[0]["file"])))
    except OSError:
        pass
    with open(EXPL_INDEX, "w", encoding="utf-8") as fh:
        json.dump([x for x in items if x.get("id") != eid], fh, ensure_ascii=False, indent=1)
    return {"ok": True}


# ---------------------------------------------------------------- arXiv 检索（官方免费接口，给“AI 生成简报”找新论文）
ARXIV_API = os.environ.get("RD_ARXIV", "https://export.arxiv.org/api/query")


def arxiv_search(q, days=14, maxn=30):
    terms = [t.strip() for t in re.split(r"[,，;；、\n]+", q or "") if t.strip()][:8]
    if not terms:
        raise ValueError("请先填写检索关键词（英文效果最好）")
    query = " OR ".join('all:"%s"' % t.replace('"', "") if " " in t else "all:%s" % t for t in terms)
    url = ARXIV_API + "?" + urllib.parse.urlencode({"search_query": query, "sortBy": "submittedDate", "sortOrder": "descending",
                                                    "start": 0, "max_results": max(5, min(int(maxn), 60))})
    req = urllib.request.Request(url, headers={"User-Agent": "research-desk/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    cutoff = (datetime.date.today() - datetime.timedelta(days=max(1, int(days)))).isoformat()
    out = []
    for e in root.findall("a:entry", ns):
        aid = re.sub(r"v\d+$", "", (e.findtext("a:id", "", ns) or "").rsplit("/abs/", 1)[-1])
        pub = (e.findtext("a:published", "", ns) or "")[:10]
        if not aid or (pub and pub < cutoff):
            continue
        out.append({"arxiv": aid, "title": re.sub(r"\s+", " ", e.findtext("a:title", "", ns) or "").strip(),
                    "authors": ", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)[:6]),
                    "date": pub, "abstract": re.sub(r"\s+", " ", e.findtext("a:summary", "", ns) or "").strip()[:1500],
                    "link": "https://arxiv.org/abs/" + aid})
    return out


# ---------------------------------------------------------------- 论文基本信息自动获取（arXiv + Semantic Scholar，免费、不用 Key）
def arxiv_meta(aid):
    url = ARXIV_API + "?" + urllib.parse.urlencode({"id_list": aid, "max_results": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "research-desk/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        root = ET.fromstring(r.read())
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    e = root.find("a:entry", ns)
    if e is None or not (e.findtext("a:title", "", ns) or "").strip() or (e.findtext("a:title", "", ns) or "").strip() == "Error":
        return {}
    return {"title": re.sub(r"\s+", " ", e.findtext("a:title", "", ns)).strip(),
            "authors": ", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)),
            "date": (e.findtext("a:published", "", ns) or "")[:10],
            "abstract": re.sub(r"\s+", " ", e.findtext("a:summary", "", ns) or "").strip(),
            "doi": (e.findtext("x:doi", "", ns) or "").strip(),
            "venue": (e.findtext("x:journal_ref", "", ns) or "").strip(),
            "link": "https://arxiv.org/abs/" + aid}


S2_META = "title,year,venue,publicationDate,externalIds,authors,abstract,journal,url"


def s2_meta(sid):
    d = s2_get("/graph/v1/paper/%s?fields=%s" % (urllib.parse.quote(sid, safe=":"), S2_META))
    if not d or not d.get("title"):
        return {}
    ex = d.get("externalIds") or {}
    j = d.get("journal") or {}
    return {"title": d.get("title") or "", "authors": ", ".join(a.get("name", "") for a in d.get("authors") or []),
            "date": d.get("publicationDate") or (str(d["year"]) if d.get("year") else ""),
            "venue": d.get("venue") or j.get("name") or "", "doi": ex.get("DOI") or "", "arxiv": ex.get("ArXiv") or "",
            "abstract": d.get("abstract") or "", "link": ("https://arxiv.org/abs/" + ex["ArXiv"]) if ex.get("ArXiv") else (d.get("url") or "")}


def paper_meta(arxiv="", doi="", title=""):
    """按 arXiv 编号、DOI 或标题查论文的基本信息；能查到多少返回多少，查不到返回空。"""
    arxiv = re.sub(r"v\d+$", "", (arxiv or "").strip().replace("arXiv:", ""))
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip())
    out, errs = {}, []
    jobs = {}
    if arxiv:
        jobs["arxiv"] = (arxiv_meta, arxiv)
        jobs["s2"] = (s2_meta, "arXiv:" + arxiv)
    elif doi:
        jobs["s2"] = (s2_meta, "DOI:" + doi)
    elif title and len(title) > 8:
        def by_title(t):
            m = s2_get("/graph/v1/paper/search/match?fields=paperId&query=" + urllib.parse.quote(t))
            return s2_meta(m["data"][0]["paperId"]) if m and m.get("data") else {}
        jobs["s2"] = (by_title, title)
    if not jobs:
        return {"ok": False, "error": "没有可以查询的 arXiv 编号、DOI 或标题"}
    res = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = {k: ex.submit(f, a) for k, (f, a) in jobs.items()}
        for k, f in futs.items():
            try:
                res[k] = f.result() or {}
            except Exception as e:
                res[k] = {}; errs.append("%s：%s" % (k, e))
    # arXiv 的标题、作者、摘要更准；出处、DOI 以 Semantic Scholar 为准（arXiv 上常常是空的）
    for k in ("title", "authors", "date", "abstract", "link", "doi", "venue", "arxiv"):
        a, b = res.get("arxiv", {}).get(k), res.get("s2", {}).get(k)
        out[k] = (b or a) if k in ("venue", "doi") else (a or b)
    if arxiv:
        out["arxiv"] = arxiv
    out = {k: v for k, v in out.items() if v}
    if not out:
        return {"ok": False, "error": "没有查到这篇论文的信息" + ("（" + "；".join(errs)[:160] + "）" if errs else "")}
    out["ok"] = True
    return out


def import_inbox():
    results = []
    for f in sorted(glob.glob(os.path.join(INBOX, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                pkg = json.load(fh)
            mode = pkg.get("mode", "create")  # create：只新建，不覆盖已有；replace：迁移用
            n = 0
            for col in COLLECTIONS:
                for doc in pkg.get(col, []) or []:
                    did = doc.get("id")
                    if not did:
                        continue
                    if mode == "create" and get_doc(col, did) is not None:
                        continue
                    set_doc(col, did, doc); n += 1
            for upd in pkg.get("updates", []) or []:
                col, did, patch = upd.get("col"), upd.get("id"), upd.get("patch") or {}
                if col in COLLECTIONS and get_doc(col, did) is not None:
                    update_doc(col, did, patch); n += 1
            shutil.move(f, os.path.join(DONE, os.path.basename(f)))
            results.append({"file": os.path.basename(f), "docs": n})
        except Exception as e:
            results.append({"file": os.path.basename(f), "error": str(e)})
    return results


# ---------------------------------------------------------------- LLM
def llm_error_text(code, raw, model):
    """把模型服务商返回的错误换成一句看得懂的话；原始内容另放在 detail 里。"""
    try:
        m = json.loads(raw).get("error") or {}
        detail = m.get("message") if isinstance(m, dict) else str(m)
    except Exception:
        detail = raw
    detail = (detail or "").strip()[:200]
    low = detail.lower()
    if code == 401 or "api key" in low or "authentication" in low:
        tip = "API Key 不对或已经失效：到“设置”里重新粘贴一次。"
    elif code == 402 or "balance" in low or "insufficient" in low:
        tip = "模型账户余额不足：到服务商网站充值后再试。"
    elif code == 429:
        tip = "请求太频繁或超出额度：等一会儿再试。"
    elif "model" in low and (code in (400, 404) or "not exist" in low or "not found" in low):
        tip = ("模型名“%s”服务商不认识：到“设置”里改一下模型名。DeepSeek 常用的有 deepseek-chat（日常）和 deepseek-reasoner（深度思考），"
               "其他名字以服务商官网的模型列表为准。" % model)
    elif code in (400, 422):
        tip = "这次请求的内容服务商不接受（可能是内容太长或格式不对）。可以换一篇论文或缩短后再试。"
    elif code >= 500:
        tip = "模型服务商那边暂时出错了，稍后再试。"
    else:
        tip = "模型接口返回了错误（%s）。" % code
    return tip + ("（服务商原话：%s）" % detail if detail else "")


def llm_stream(handler, body):
    cfg = load_cfg()
    if not cfg.get("apiKey"):
        handler.send_json({"error": "还没有填写 API Key：点右上角“设置”。"}, 400); return
    msgs = [m for m in (body.get("messages") or []) if isinstance(m, dict) and str(m.get("content") or "").strip()]
    if not msgs:
        handler.send_json({"error": "没有要发给 AI 的内容。"}, 400); return
    model = body.get("model") or (cfg.get("deepModel") if body.get("tier") == "complex" else cfg.get("model"))
    payload = {"model": model, "messages": msgs, "stream": True}
    thinking = body.get("thinking", cfg.get("thinking", False))
    if cfg.get("provider") == "deepseek":
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if body.get("max_tokens"):
        payload["max_tokens"] = int(body["max_tokens"])
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    url = cfg["baseUrl"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["apiKey"], "Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:600]
        handler.send_json({"error": llm_error_text(e.code, raw, model), "upstream": e.code, "detail": raw}, e.code if e.code in (400, 401, 402, 403, 404, 429) else 502); return
    except Exception as e:
        handler.send_json({"error": "连接模型接口失败：%s" % e}, 502); return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    usage = None
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    handler.wfile.write(piece.encode("utf-8")); handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        return
    finally:
        resp.close()
    if usage:
        try:
            log = os.path.join(DATA, "usage.log")
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": now(), "model": model, "usage": usage}, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------- HTTP
# ---------------------------------------------------------------- 手机访问（局域网）
# 手机访问密码防暴力猜测：10 分钟内全局最多错 10 次（多线程服务器下单靠 sleep 拦不住并发猜测）
LOGIN_FAILS = []
LOGIN_LOCK = threading.Lock()
LAN_BOUND = [False]


def lan_secret():
    fp = os.path.join(DATA, "lan_secret")
    if not os.path.exists(fp):
        with open(fp, "w") as f:
            f.write(secrets.token_hex(32))
    with open(fp) as f:
        return f.read().strip()


def lan_token(cfg):
    return hmac.new(lan_secret().encode(), (cfg.get("lanPassword") or "").encode("utf-8"), hashlib.sha256).hexdigest()


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); ips.append(s.getsockname()[0]); s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>研究台</title><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="研究台">
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f3f5f8;font-family:-apple-system,"PingFang SC",sans-serif;color:#18202c}
form{background:#fff;border:1px solid #dde2ea;border-radius:16px;padding:28px 24px;width:min(340px,88vw);box-shadow:0 6px 24px rgba(20,40,80,.08)}
h1{font-size:1.3rem;margin:0 0 4px}p{color:#5b6678;font-size:.9rem;margin:0 0 18px}input{width:100%;box-sizing:border-box;font-size:1.05rem;padding:12px;border:1px solid #dde2ea;border-radius:10px}
button{margin-top:12px;width:100%;font-size:1.05rem;padding:12px;border:0;border-radius:10px;background:#2b54c9;color:#fff}#e{color:#a33b3b;font-size:.9rem;min-height:1.2em;margin-top:8px}
@media (prefers-color-scheme:dark){body{background:#10141b;color:#e4e8ef}form{background:#171c25;border-color:#2a313d}input{background:#10141b;color:#e4e8ef;border-color:#2a313d}}</style></head>
<body><form id="f"><h1>◈ 研究台</h1><p>输入你在电脑上设置的访问密码</p><input id="pw" type="password" autocomplete="current-password" autofocus placeholder="访问密码"><button>进入</button><div id="e"></div></form>
<script>document.getElementById("f").onsubmit=async e=>{e.preventDefault();const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pw:document.getElementById("pw").value})});if(r.ok)location.reload();else document.getElementById("e").textContent="密码不对，再试一次"}</script></body></html>"""


class H(BaseHTTPRequestHandler):
    server_version = "ResearchDesk/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def jbody(self):
        b = self.body()
        return json.loads(b.decode("utf-8")) if b else {}

    def is_remote(self):
        return not (self.client_address[0] in ("127.0.0.1", "::1") or self.client_address[0].startswith("::ffff:127."))

    def lan_ok(self):
        """同一 Wi-Fi / 热点里的手机访问：需在设置里开启并输入访问密码。"""
        cfg = load_cfg()
        if not cfg.get("lanAccess") or not cfg.get("lanPassword"):
            self.send_json({"error": "研究台没有开启手机访问"}, 403); return False
        host = self.headers.get("Host") or ""
        hn = host.split(":")[0]
        if not (re.match(r"^(10|172\.(1[6-9]|2\d|3[01])|192\.168|169\.254)\.\d+\.\d+(\.\d+)?$", hn) or hn.endswith(".local")):
            self.send_json({"error": "forbidden"}, 403); return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            self.send_json({"error": "forbidden"}, 403); return False
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/api/login", "/manifest.json") or u.path.startswith("/static/katex/") or u.path == "/static/icon.svg":
            return True
        ck = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1); ck[k] = v
        if hmac.compare_digest(ck.get("rd_auth", ""), lan_token(cfg)):
            if self.command == "POST" and u.path in ("/api/config", "/api/open", "/api/pdf_translate"):
                self.send_json({"error": "手机上不能做这个操作，请在电脑上进行"}, 403); return False
            return True
        if self.command == "GET" and u.path in ("/", "/index.html"):
            b = LOGIN_HTML.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return False
        self.send_json({"error": "需要先输入访问密码"}, 401); return False

    def local_only(self):
        if self.is_remote():
            return self.lan_ok()
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_json({"error": "forbidden"}, 403); return False
        origin = self.headers.get("Origin")
        if origin and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self.send_json({"error": "forbidden"}, 403); return False
        return True

    def serve_file(self, path, download_name=None, extra=None):
        if not path or not os.path.isfile(path):
            self.send_json({"error": "文件不存在：%s" % (path or "")}, 404); return
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1): start = int(m.group(1))
                if m.group(2): end = min(int(m.group(2)), size - 1)
                elif not m.group(1): start = max(0, size - int(m.group(2) or 0))
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if download_name:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(download_name))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk: break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_GET(self):
        if not self.local_only(): return
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self.serve_file(os.path.join(STATIC, "index.html"))
            if p.startswith("/static/"):
                fp = os.path.realpath(os.path.join(STATIC, p[len("/static/"):]))
                if not fp.startswith(os.path.realpath(STATIC) + os.sep): return self.send_json({"error": "forbidden"}, 403)
                return self.serve_file(fp)
            if p.startswith("/_blob/"):
                tok = urllib.parse.unquote(p[len("/_blob/"):])
                fp = token_path(tok)
                if not fp or not path_ok(fp): return self.send_json({"error": "不允许访问这个路径"}, 403)
                return self.serve_file(fp, download_name=os.path.basename(fp) if qs.get("dl") else None)
            if p.startswith("/explainers/"):
                # 图解页可能是 AI 生成的，按沙盒方式提供：脚本能运行，但不能读写研究台的数据
                fp = os.path.realpath(os.path.join(EXPL_DIR, urllib.parse.unquote(p[len("/explainers/"):])))
                if not fp.startswith(os.path.realpath(EXPL_DIR) + os.sep): return self.send_json({"error": "forbidden"}, 403)
                return self.serve_file(fp, extra={"Content-Security-Policy": "sandbox allow-scripts allow-popups"})
            if p == "/api/ping":
                local = self.client_address[0] in ("127.0.0.1", "::1") or self.client_address[0].startswith("::ffff:127.")
                return self.send_json({"ok": True, "app": "research-desk", "root": ROOT} if local else {"ok": True})
            if p == "/api/meta":
                try:
                    return self.send_json(paper_meta(qs.get("arxiv", [""])[0], qs.get("doi", [""])[0], qs.get("title", [""])[0]))
                except Exception as e:
                    return self.send_json({"ok": False, "error": "查询失败：%s" % e})
            if p == "/api/explainers":
                return self.send_json(explainer_list())
            if p == "/api/arxiv":
                try:
                    return self.send_json({"ok": True, "items": arxiv_search(qs.get("q", [""])[0], int(qs.get("days", ["14"])[0] or 14), int(qs.get("max", ["30"])[0] or 30))})
                except Exception as e:
                    return self.send_json({"ok": False, "error": "检索 arXiv 失败：%s" % e})
            if p == "/api/all":
                return self.send_json({c: all_docs(c) for c in COLLECTIONS if c not in ("fulltext",)})
            if p == "/api/list":
                return self.send_json(all_docs(qs.get("c", [""])[0]))
            if p == "/api/get":
                d = get_doc(qs.get("c", [""])[0], qs.get("id", [""])[0])
                return self.send_json({"exists": d is not None, "data": d})
            if p == "/api/config":
                cfg = load_cfg()
                k = cfg.get("apiKey") or ""
                cfg["apiKey"] = ("已填写（尾号 " + k[-4:] + "）") if k else ""
                cfg["lanPassword"] = "已设置" if cfg.get("lanPassword") else ""
                cfg["hasKey"] = bool(k)
                cfg["soffice"] = bool(find_soffice())
                return self.send_json(cfg)
            if p == "/api/pdf_translate":
                j = dict(TR_JOBS.get(qs.get("id", [""])[0]) or {"state": "none"})
                if j.get("t0"): j["elapsed"] = int(time.time() - j["t0"])
                return self.send_json(j)
            if p == "/api/related":
                return self.send_json(related(qs.get("id", [""])[0], force=bool(qs.get("force"))))
            if p == "/api/usage":
                tot = {"prompt": 0, "completion": 0, "calls": 0}
                lp = os.path.join(DATA, "usage.log")
                if os.path.exists(lp):
                    for line in open(lp, encoding="utf-8"):
                        try:
                            u2 = json.loads(line)["usage"]
                            tot["prompt"] += u2.get("prompt_tokens", 0); tot["completion"] += u2.get("completion_tokens", 0); tot["calls"] += 1
                        except Exception:
                            pass
                return self.send_json(tot)
            self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        if not self.local_only(): return
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/api/login":
                b = self.jbody(); cfg = load_cfg()
                now = time.time()
                with LOGIN_LOCK:
                    LOGIN_FAILS[:] = [t for t in LOGIN_FAILS if now - t < 600]
                    if len(LOGIN_FAILS) >= 10:
                        return self.send_json({"error": "密码错误次数太多，请 10 分钟后再试"}, 429)
                if cfg.get("lanPassword") and hmac.compare_digest(str(b.get("pw", "")).encode("utf-8"), str(cfg["lanPassword"]).encode("utf-8")):
                    self.send_response(200); self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", "rd_auth=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict" % lan_token(cfg))
                    self.end_headers(); self.wfile.write(b'{"ok":true}'); return
                with LOGIN_LOCK:
                    LOGIN_FAILS.append(now)
                time.sleep(1.5); return self.send_json({"error": "密码不对"}, 403)
            if p == "/api/lan_info":
                cfg = load_cfg()
                return self.send_json({"enabled": bool(cfg.get("lanAccess")), "hasPassword": bool(cfg.get("lanPassword")), "listening": LAN_BOUND[0], "urls": ["http://%s:%d" % (ip, PORT) for ip in lan_ips()]})
            if p == "/api/explainer":
                b = self.jbody(); return self.send_json(explainer_save(b))
            if p == "/api/explainer_delete":
                b = self.jbody(); return self.send_json(explainer_delete(b.get("id") or ""))
            if p == "/api/set":
                b = self.jbody(); set_doc(b["c"], b["id"], b["data"]); return self.send_json({"ok": True})
            if p == "/api/update":
                b = self.jbody()
                try:
                    update_doc(b["c"], b["id"], b["patch"])
                    if b["c"] == "papers" and "kind" in (b.get("patch") or {}):
                        log_activity({"type": "kind", "pid": b["id"], "v": b["patch"]["kind"]})
                except KeyError:
                    return self.send_json({"error": "文档不存在"}, 404)
                return self.send_json({"ok": True})
            if p == "/api/delete":
                b = self.jbody(); del_doc(b["c"], b["id"]); return self.send_json({"ok": True})
            if p == "/api/upload":
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "file.pdf")
                data = self.body()
                ext = os.path.splitext(name)[1].lower() or ".bin"
                fid = "f" + uuid.uuid4().hex[:16]
                fp = os.path.join(FILES, fid + ext)
                with open(fp, "wb") as f:
                    f.write(data)
                return self.send_json({"id": fid, "url": "/_blob/" + fid, "sizeBytes": len(data), "contentType": mimetypes.guess_type(fp)[0] or ""})
            if p == "/api/delete_file":
                b = self.jbody(); fp = token_path(b.get("id", ""))
                if fp and os.path.realpath(fp).startswith(os.path.realpath(FILES) + os.sep) and os.path.exists(fp):
                    os.remove(fp); return self.send_json({"deleted": True})
                return self.send_json({"deleted": False})
            if p == "/api/open":
                b = self.jbody(); fp = token_path(b["id"]) if b.get("id") else b.get("path")
                if not fp or not path_ok(fp) or not os.path.exists(fp): return self.send_json({"error": "不允许打开这个路径"}, 403)
                try:
                    if sys.platform.startswith("win"):
                        if b.get("reveal"):
                            subprocess.Popen(["explorer", "/select,", os.path.normpath(fp)])
                        else:
                            os.startfile(fp)  # noqa
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", "-R", fp] if b.get("reveal") else ["open", fp])
                    else:
                        opener = shutil.which("xdg-open")
                        if not opener or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                            return self.send_json({"error": "这台电脑没有图形界面（或没装 xdg-open），没法用默认程序打开。文件位置：" + fp}, 501)
                        subprocess.Popen([opener, os.path.dirname(fp) if b.get("reveal") else fp])
                except Exception as e:
                    return self.send_json({"error": "打开失败：%s" % e}, 500)
                return self.send_json({"ok": True})
            if p == "/api/config":
                b = self.jbody(); cfg = load_cfg()
                for k in ("provider", "baseUrl", "model", "deepModel", "thinking", "zoteroDir", "meetingDir", "paperDir", "briefDir", "lanAccess", "focus", "dirs"):
                    if k in b: cfg[k] = b[k]
                if b.get("lanPassword") and b["lanPassword"] != "已设置":
                    cfg["lanPassword"] = str(b["lanPassword"]).strip()
                if b.get("apiKey") and not b["apiKey"].startswith("已填写"):
                    cfg["apiKey"] = b["apiKey"].strip()
                if b.get("clearKey"):
                    cfg["apiKey"] = ""
                save_cfg(cfg); return self.send_json({"ok": True})
            if p == "/api/llm":
                return llm_stream(self, self.jbody())
            if p == "/api/sync":
                b = self.jbody(); out = {}
                what = b.get("what") or ["inbox", "zotero", "papers", "meetings"]
                if "inbox" in what: out["inbox"] = import_inbox()
                if "zotero" in what: out["zotero"] = zotero_sync()
                if "papers" in what: out["papers"] = paper_dir_scan()
                if "meetings" in what:
                    link_existing_topic_docs(); out["meetings"] = meeting_scan()
                return self.send_json(out)
            if p == "/api/pdf_translate":
                return self.send_json(tr_start(self.jbody().get("id", "")))
            if p == "/api/activity":
                log_activity(self.jbody()); return self.send_json({"ok": True})
            if p == "/api/ping":
                # 本机请求时附上程序所在文件夹，启动脚本据此判断端口上跑的是不是这一份研究台
                local = self.client_address[0] in ("127.0.0.1", "::1") or self.client_address[0].startswith("::ffff:127.")
                return self.send_json({"ok": True, "app": "research-desk", "root": ROOT} if local else {"ok": True})
            self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)


def backup_db():
    """每天第一次把 library.db 备份到 data/backup/library-日期.db（用 SQLite 自带的在线备份，运行中也安全），保留最近 14 份。
    数据库已经损坏时不备份，免得坏文件挤掉好备份，并在终端里提示。"""
    bdir = os.path.join(DATA, "backup")
    os.makedirs(bdir, exist_ok=True)
    out = os.path.join(bdir, "library-%s.db" % datetime.date.today().strftime("%Y%m%d"))
    if os.path.exists(out) or not os.path.exists(DB_PATH):
        return
    src = sqlite3.connect(DB_PATH, timeout=30)
    try:
        ok = src.execute("PRAGMA quick_check").fetchone()[0]
        if ok != "ok":
            print("[注意] 数据库检查没通过（%s），今天不备份；最近的好备份在 data/backup/" % ok, flush=True)
            return
        dst = sqlite3.connect(out + ".tmp")
        src.backup(dst); dst.close()
        os.replace(out + ".tmp", out)
    finally:
        src.close()
    old = sorted(glob.glob(os.path.join(bdir, "library-*.db")))
    for f in old[:-14]:
        try: os.remove(f)
        except OSError: pass


def backup_loop():
    while True:
        try:
            backup_db()
        except Exception:
            traceback.print_exc()
        time.sleep(6 * 3600)


def main():
    threading.Thread(target=backup_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    try:
        import_inbox()
        write_snapshot()
    except Exception:
        traceback.print_exc()
    cfg = load_cfg()
    lan = bool(cfg.get("lanAccess") and cfg.get("lanPassword"))
    srv = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", PORT), H)
    LAN_BOUND[0] = lan
    print("研究台已启动： http://127.0.0.1:%d  （关闭这个窗口即退出）" % PORT, flush=True)
    if lan:
        for ip in lan_ips():
            print("手机（同一 Wi-Fi / 热点）浏览器打开： http://%s:%d" % (ip, PORT), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
