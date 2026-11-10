"""CLO 2 local document store: the subset of the Firestore REST API that CLO uses, on SQLite on this Mac.

The bridge and the apps keep speaking Firestore REST (same paths, same {"fields": …} encoding); only where it lands
changed — here instead of Google. Paths are relative to ".../documents/", e.g. "clo/local/terminalInbox/<id>".

Supported: GET doc (?ifNoneMatch=<updateTime> → 304) · GET collection (pageSize, mask.fieldPaths) · PATCH (updateMask.fieldPaths = merge) · DELETE ·
POST <parent>:runQuery (from collectionId [allDescendants], where fieldFilter / compositeFilter AND, orderBy, limit,
select) · POST :commit (update / delete writes).
"""
import datetime as dt
import json
import os
import sqlite3
import threading
import urllib.parse

_lock = threading.RLock()
_db = None
listeners = []   # fn(path) after every write: the tunnel tells paired devices what changed (no polling)


def _changed(path):
    for fn in listeners:
        try:
            fn(path)
        except Exception:
            pass


def open_store(path):
    global _db
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    _db.execute("PRAGMA journal_mode=WAL")
    _db.execute("CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, parent TEXT NOT NULL, coll TEXT NOT NULL,"
                " fields TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)")
    _db.execute("CREATE INDEX IF NOT EXISTS docs_parent ON docs(parent, coll)")
    _db.execute("CREATE INDEX IF NOT EXISTS docs_coll ON docs(coll)")
    os.chmod(path, 0o600)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class NotFound(Exception):
    pass


def _split(path):
    path = path.strip("/")
    parent, _, doc_id = path.rpartition("/")
    coll_parent, _, coll = parent.rpartition("/")
    return coll_parent, coll, doc_id


def _doc_json(path, fields, created, updated, mask=None):
    if mask:
        fields = {k: v for k, v in fields.items() if k in mask}
    return {"name": "projects/local/databases/(default)/documents/" + path, "fields": fields,
            "createTime": created, "updateTime": updated}


def get(path, mask=None):
    with _lock:
        row = _db.execute("SELECT fields, created, updated FROM docs WHERE path=?", (path.strip("/"),)).fetchone()
    if not row:
        raise NotFound(path)
    return _doc_json(path.strip("/"), json.loads(row[0]), row[1], row[2], mask)


def list_coll(path, mask=None, page_size=None):
    parent, _, coll = path.strip("/").rpartition("/")
    with _lock:
        rows = _db.execute("SELECT path, fields, created, updated FROM docs WHERE parent=? AND coll=? ORDER BY updated DESC, path",
                           (parent, coll)).fetchall()
    # pageSize is ignored on purpose: no nextPageToken here and the app doesn't page, so a cap silently hid documents
    # (a Mac with >300 sessions lost pinned ones). Local reads cost nothing; return the whole collection, newest first.
    docs = [_doc_json(p, json.loads(f), c, u, mask) for p, f, c, u in rows]
    return {"documents": docs} if docs else {}


def patch(path, fields, merge_keys=None):
    path = path.strip("/")
    parent, coll, _ = _split(path)
    ts = _now()
    with _lock:
        row = _db.execute("SELECT fields, created FROM docs WHERE path=?", (path,)).fetchone()
        if merge_keys and row:
            cur = json.loads(row[0])
            for k in merge_keys:
                top = k.split(".")[0].strip("`")
                if top in fields:
                    cur[top] = fields[top]
                else:
                    cur.pop(top, None)
            fields = cur
        elif merge_keys:
            fields = {k: v for k, v in fields.items() if k in {m.split(".")[0].strip("`") for m in merge_keys}}
        created = row[1] if row else ts
        _db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?)",
                    (path, parent, coll, json.dumps(fields, ensure_ascii=False), created, ts))
    _changed(path)
    return _doc_json(path, fields, created, ts)


def delete(path):
    with _lock:
        _db.execute("DELETE FROM docs WHERE path=?", (path.strip("/"),))
    _changed(path.strip("/"))
    return {}


# ---------------------------------------------------------------- queries
def _val(v):
    """Comparable Python value of an encoded Firestore value."""
    if v is None:
        return None
    (kind, x), = v.items()
    if kind == "integerValue":
        return int(x)
    if kind == "doubleValue":
        return float(x)
    if kind == "timestampValue":
        try:
            return dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
        except ValueError:
            return x
    if kind == "arrayValue":
        return [_val(i) for i in x.get("values", [])]
    if kind == "nullValue":
        return None
    return x


def _field(fields, path):
    cur = {"mapValue": {"fields": fields}}
    for part in path.split("."):
        part = part.strip("`")
        cur = (cur or {}).get("mapValue", {}).get("fields", {}).get(part)
        if cur is None:
            return None
    return cur


def _match(fields, flt):
    if not flt:
        return True
    if "compositeFilter" in flt:
        subs = flt["compositeFilter"].get("filters", [])
        op = flt["compositeFilter"].get("op", "AND")
        res = [_match(fields, s) for s in subs]
        return all(res) if op == "AND" else any(res)
    if "unaryFilter" in flt:
        u = flt["unaryFilter"]
        v = _val(_field(fields, u["field"]["fieldPath"]))
        return {"IS_NULL": v is None, "IS_NOT_NULL": v is not None}.get(u["op"], False)
    f = flt["fieldFilter"]
    have = _field(fields, f["field"]["fieldPath"])
    if have is None:
        return False
    a, b, op = _val(have), _val(f["value"]), f["op"]
    try:
        if op == "EQUAL": return a == b
        if op == "NOT_EQUAL": return a != b
        if op == "GREATER_THAN": return a > b
        if op == "GREATER_THAN_OR_EQUAL": return a >= b
        if op == "LESS_THAN": return a < b
        if op == "LESS_THAN_OR_EQUAL": return a <= b
        if op == "IN": return a in b
        if op == "NOT_IN": return a not in b
        if op == "ARRAY_CONTAINS": return isinstance(a, list) and b in a
        if op == "ARRAY_CONTAINS_ANY": return isinstance(a, list) and any(x in a for x in b)
    except TypeError:
        return False
    return False


def run_query(parent, q):
    parent = parent.strip("/")
    frm = (q.get("from") or [{}])[0]
    coll, deep = frm.get("collectionId"), frm.get("allDescendants", False)
    with _lock:
        if deep:
            rows = _db.execute("SELECT path, fields, created, updated FROM docs WHERE coll=? AND (parent=? OR parent LIKE ?)",
                               (coll, parent, parent + "/%")).fetchall()
        else:
            rows = _db.execute("SELECT path, fields, created, updated FROM docs WHERE parent=? AND coll=?",
                               (parent, coll)).fetchall()
    docs = [(p, json.loads(f), c, u) for p, f, c, u in rows]
    docs = [d for d in docs if _match(d[1], q.get("where"))]
    for o in reversed(q.get("orderBy") or []):
        fp, desc = o["field"]["fieldPath"], o.get("direction") == "DESCENDING"
        docs.sort(key=lambda d: (_val(_field(d[1], fp)) is None, str(_val(_field(d[1], fp)))), reverse=desc)
    if q.get("limit"):
        docs = docs[: int(q["limit"])]
    mask = [s["fieldPath"] for s in (q.get("select") or {}).get("fields", [])] or None
    ts = _now()
    return [{"document": _doc_json(p, f, c, u, mask), "readTime": ts} for p, f, c, u in docs] or [{"readTime": ts}]


def commit(writes):
    for w in writes:
        if "update" in w:
            name = w["update"]["name"].split("/documents/", 1)[-1]
            patch(name, w["update"].get("fields", {}), (w.get("updateMask") or {}).get("fieldPaths"))
        elif "delete" in w:
            delete(w["delete"].split("/documents/", 1)[-1])
    return {"writeResults": [{} for _ in writes], "commitTime": _now()}


# ---------------------------------------------------------------- REST dispatch
def handle(method, url, body=None):
    """(status, json) for a Firestore-REST-shaped request. `url` may be a full Firestore URL or a bare path."""
    u = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(u.path)
    if "/documents" in path:
        path = path.split("/documents", 1)[1]
    path = path.strip("/")
    qs = urllib.parse.parse_qs(u.query)
    try:
        if method == "POST" and path.endswith(":runQuery"):
            return 200, run_query(path[: -len(":runQuery")], (body or {}).get("structuredQuery", {}))
        if method == "POST" and (path.endswith(":commit") or path == ":commit"):
            return 200, commit((body or {}).get("writes", []))
        depth = len([p for p in path.split("/") if p])
        mask = qs.get("mask.fieldPaths")
        if method == "GET":
            if depth % 2 == 1:
                return 200, list_coll(path, mask, int(qs["pageSize"][0]) if "pageSize" in qs else None)
            doc = get(path, mask)
            if (qs.get("ifNoneMatch") or [None])[0] == doc["updateTime"]:
                return 304, {}   # unchanged since the caller's copy: polling a big transcript costs nothing
            return 200, doc
        if method in ("PATCH", "POST"):
            if method == "POST":  # create with ?documentId=
                doc_id = (qs.get("documentId") or [None])[0]
                if not doc_id:
                    return 400, {"error": "documentId required"}
                path = f"{path}/{doc_id}"
                try:
                    get(path)
                    return 409, {"error": {"code": 409, "status": "ALREADY_EXISTS"}}
                except NotFound:
                    pass
            return 200, patch(path, (body or {}).get("fields", {}), qs.get("updateMask.fieldPaths"))
        if method == "DELETE":
            return 200, delete(path)
        return 400, {"error": f"unsupported {method}"}
    except NotFound:
        return 404, {"error": {"code": 404, "status": "NOT_FOUND"}}
