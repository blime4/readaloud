"""书架：把导入的书留在本机，电脑和手机共用同一份。

以前导入一本书只在内存里过一遍，页面一刷就没了 —— 手机上想接着听，
只能重新选文件、重新找位置。这里把正文按书落盘，进度也记在书本身，
于是"在哪台设备读到第几句"变成同一份数据。

存储布局（每本一个目录，删一本不会牵动别的）：
    books/<id>/meta.json    标题、字数、来源、进度、时间
    books/<id>/text.txt     正文（UTF-8）

正文只用 .txt 存，不存原始 EPUB/MOBI：取稿在导入时就做完了，
书架要的是"能立刻读的文字"，不是文件柜。
"""

import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
BOOKS_DIR = BASE / "books"

MAX_BOOKS = 300                        # 再多就该整理了，别把磁盘塞满
MAX_TEXT_CHARS = 8_000_000             # 单本正文上限（约 24 MB UTF-8）
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_TRACK_TITLE = re.compile(r"[\x00-\x1f]")


class BookError(ValueError):
    """书架操作失败（给用户看的中文原因）。"""


def _root() -> Path:
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    return BOOKS_DIR


def _dir(book_id: str) -> Path:
    # id 来自我们自己的 uuid4().hex[:12]，仍然严格校验 —— 目录拼接最怕穿越
    if not _ID_RE.match(str(book_id or "")):
        raise BookError("书的编号无效。")
    return _root() / book_id


def _clean_title(title: str, fallback: str = "未命名") -> str:
    text = _TRACK_TITLE.sub("", str(title or "")).strip()
    return (text or fallback)[:120]


def _read_meta(path: Path) -> dict | None:
    try:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(meta, dict) or not _ID_RE.match(str(meta.get("id", ""))):
        return None
    return meta


def _write(book_dir: Path, text: str, meta: dict) -> None:
    """先写临时文件再替换，中途断电不会留下半本坏书。"""
    book_dir.mkdir(parents=True, exist_ok=True)
    tmp_text = book_dir / "text.part"
    tmp_meta = book_dir / "meta.part"
    tmp_text.write_text(text, encoding="utf-8")
    tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_text.replace(book_dir / "text.txt")
    tmp_meta.replace(book_dir / "meta.json")


def _public(meta: dict) -> dict:
    prog = meta.get("progress") or {}
    total = int(prog.get("total") or 0)
    nxt = int(prog.get("next") or 0)
    pct = round(100 * nxt / total) if total else 0
    return {
        "id": meta["id"],
        "title": meta.get("title") or "未命名",
        "type": meta.get("type") or "txt",
        "chars": int(meta.get("chars") or 0),
        "source": meta.get("source") or "",
        "created": meta.get("created") or 0,
        "updated": meta.get("updated") or 0,
        "progress": {"next": nxt, "total": total, "ts": prog.get("ts") or 0, "pct": pct},
        "finished": bool(total and nxt >= total),
        "cover": meta.get("cover") or "",
    }


def list_books() -> list:
    """按"最近动过"排序。排序键统一用 updated，翻书也算动过。"""
    items = []
    for path in _root().iterdir():
        if not path.is_dir():
            continue
        meta = _read_meta(path)
        if meta:
            items.append(_public(meta))
    items.sort(key=lambda b: b.get("updated") or 0, reverse=True)
    return items


def get_book(book_id: str) -> dict:
    book_dir = _dir(book_id)
    meta = _read_meta(book_dir)
    if not meta:
        raise BookError("这本书不在书架上了。")
    try:
        text = (book_dir / "text.txt").read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise BookError("这本书的正文丢了，删掉重新导入吧。") from error
    return {**_public(meta), "text": text}


def add_book(title: str, text: str, type_: str = "txt", source: str = "") -> dict:
    """入库。同一份正文重复导入时复用原来那本，进度不丢。"""
    content = str(text or "")
    if not content.strip():
        raise BookError("没有可入库的正文。")
    if len(content) > MAX_TEXT_CHARS:
        raise BookError(f"单本正文最多 {MAX_TEXT_CHARS // 10000} 万字。")

    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
    now = round(time.time(), 3)          # 秒级会让"同一秒入库的两本书"分不出先后
    for path in _root().iterdir():
        meta = _read_meta(path) if path.is_dir() else None
        if meta and meta.get("sha1") == digest:
            meta["updated"] = now
            if title:
                meta["title"] = _clean_title(title)
            _write(path, content, meta)
            return _public(meta)

    if len(list_books()) >= MAX_BOOKS:
        raise BookError(f"书架最多放 {MAX_BOOKS} 本，先删几本再导入。")

    book_id = uuid.uuid4().hex[:12]
    meta = {
        "id": book_id,
        "title": _clean_title(title, "未命名"),
        "type": type_ or "txt",
        "source": str(source or "")[:400],
        "chars": len(content),
        "sha1": digest,
        "created": now,
        "updated": now,
        "progress": {"next": 0, "total": 0, "ts": 0},
    }
    _write(_dir(book_id), content, meta)
    return _public(meta)


def save_progress(book_id: str, next_index: int, total: int) -> dict:
    """记"下一句该读第几句"。故意允许倒退 —— 用户往回拖也是有效进度。"""
    book_dir = _dir(book_id)
    meta = _read_meta(book_dir)
    if not meta:
        raise BookError("这本书不在书架上了。")
    meta["progress"] = {
        "next": max(0, int(next_index or 0)),
        "total": max(0, int(total or 0)),
        "ts": round(time.time(), 3),
    }
    meta["updated"] = meta["progress"]["ts"]
    tmp = book_dir / "meta.part"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(book_dir / "meta.json")
    return _public(meta)


def delete_book(book_id: str) -> None:
    book_dir = _dir(book_id)
    if not book_dir.exists():
        raise BookError("这本书不在书架上了。")
    shutil.rmtree(book_dir, ignore_errors=True)


# ---------------------------------------------------------------- 分段缓存
# 打开一本书 = 重新解析整本正文（清洗/规整/逐句），几万字的书要等几秒。
# 把分段结果按「正文指纹 + 分段参数」缓存下来，第二次点开就是秒开。
PREP_CACHE = 2                          # 每本最多留几份（换语速/长度参数重试也能命中）
PREP_MAX_CHARS = 2_000_000              # 超大文本不缓存，别把 prep.json 撑爆


def _prep_path(book_id: str) -> Path:
    return _dir(book_id) / "prep.json"


def get_prep(book_id: str, key: str) -> dict | None:
    try:
        data = json.loads(_prep_path(book_id).read_text(encoding="utf-8"))
        entry = data.get(key) if isinstance(data, dict) else None
        return entry if isinstance(entry, dict) and entry.get("items") else None
    except Exception:
        return None


def put_prep(book_id: str, key: str, entry: dict, chars: int) -> None:
    if chars > PREP_MAX_CHARS:
        return
    try:
        data = json.loads(_prep_path(book_id).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.pop(key, None)
    data[key] = entry
    while len(data) > PREP_CACHE:       # dict 保序，最旧的那份先出局
        data.pop(next(iter(data)))
    book_dir = _dir(book_id)
    try:
        tmp = book_dir / "prep.part"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(book_dir / "prep.json")
    except Exception:
        pass                            # 缓存写不进去不影响朗读


def stats() -> dict:
    books = list_books()
    size = 0
    for path in _root().iterdir():
        txt = path / "text.txt"
        if path.is_dir() and txt.exists():
            size += txt.stat().st_size
    return {"count": len(books), "bytes": size, "limit": MAX_BOOKS}
