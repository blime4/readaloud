"""Local text extraction for documents imported into ReadAloud."""

import io
import re
import struct
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from bs4 import BeautifulSoup


SUPPORTED_EXTENSIONS = {".txt", ".epub", ".mobi", ".pdf"}
MAX_IMPORT_BYTES = 80 * 1024 * 1024
MAX_EPUB_FILES = 2_000
MAX_EPUB_EXPANDED_BYTES = 200 * 1024 * 1024


class DocumentImportError(ValueError):
    """A document cannot be converted into readable text."""


def _plain_text(markup: bytes | str) -> str:
    if isinstance(markup, bytes):
        markup = _decode_text(markup)
    soup = BeautifulSoup(markup, "html.parser")
    for node in soup(["script", "style", "nav", "head"]):
        node.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def _extract_pdf(data: bytes) -> str:
    try:
        import fitz
    except ImportError as error:
        raise DocumentImportError("PDF 导入组件未安装。") from error
    try:
        document = fitz.open(stream=data, filetype="pdf")
        text = "\n\n".join(page.get_text("text") for page in document)
        document.close()
        return text.strip()
    except Exception as error:
        raise DocumentImportError("无法读取这个 PDF。") from error


def _extract_epub(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_EPUB_FILES or sum(entry.file_size for entry in entries) > MAX_EPUB_EXPANDED_BYTES:
                raise DocumentImportError("EPUB 解压后的内容过大。")
            names = {entry.filename for entry in entries}
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(node.attrib["full-path"] for node in container.iter() if node.tag.endswith("rootfile"))
            package = ElementTree.fromstring(archive.read(rootfile))
            manifest = {
                node.attrib["id"]: node.attrib.get("href", "")
                for node in package.iter()
                if node.tag.endswith("item")
            }
            base = PurePosixPath(rootfile).parent
            spine = [node.attrib.get("idref", "") for node in package.iter() if node.tag.endswith("itemref")]
            paths = [str(base / manifest[item]) for item in spine if manifest.get(item)]
            if not paths:
                paths = sorted(name for name in names if name.lower().endswith((".xhtml", ".html", ".htm")))
            parts = [_plain_text(archive.read(path)) for path in paths if path in names]
        return "\n\n".join(part for part in parts if part).strip()
    except DocumentImportError:
        raise
    except Exception as error:
        raise DocumentImportError("无法读取这个 EPUB。") from error


def _extract_mobi(data: bytes) -> str:
    """Extract the common unencrypted PalmDOC/MOBI record format."""
    if len(data) < 94 or data[60:68] != b"BOOKMOBI":
        raise DocumentImportError("MOBI 文件格式无效。")
    record_count = struct.unpack_from(">H", data, 76)[0]
    if record_count < 2 or len(data) < 78 + record_count * 8:
        raise DocumentImportError("MOBI 记录表无效。")
    offsets = [struct.unpack_from(">I", data, 78 + index * 8)[0] for index in range(record_count)]
    header_start = offsets[0]
    if header_start + 16 > len(data):
        raise DocumentImportError("MOBI 页头无效。")
    compression, _, text_length, text_records, record_size, _ = struct.unpack_from(
        ">HHIHHI", data, header_start)
    encryption = struct.unpack_from(">H", data, header_start + 12)[0]
    if encryption:
        raise DocumentImportError("这个 MOBI 带有 DRM 加密，读不出正文。")
    if compression not in (1, 2):
        # 17480 = HUFF/CDIC，这种压缩要额外的哈夫曼表，暂不支持。
        raise DocumentImportError("这个 MOBI 用了暂不支持的压缩格式（HUFF/CDIC）。")
    if not 0 < record_size <= 8192:
        record_size = 4096
    last = min(text_records + 1, record_count)
    chunks = []
    produced = 0
    for index in range(1, last):
        start = offsets[index]
        end = offsets[index + 1] if index + 1 < record_count else len(data)
        if not (0 <= start <= end <= len(data)):
            raise DocumentImportError("MOBI 文本记录无效。")
        chunk = _unpack_palmdoc(data[start:end]) if compression == 2 else data[start:end]
        # 很多转换工具会在每条记录末尾多解出几个字节的填充。不按声明长度
        # 截断的话，UTF-8 会在每个 4 KB 边界错位，中文整段变成乱码。
        # 最后一条记录只取到声明正文长度的位置。
        limit = min(record_size, text_length - produced) if text_length else record_size
        if limit <= 0:
            break
        chunk = chunk[:limit]
        produced += len(chunk)
        chunks.append(chunk)
    return _plain_text(_decode_text(b"".join(chunks)))


def _unpack_palmdoc(data: bytes) -> bytes:
    """Decompress one PalmDOC record (compression type 2)."""
    output = bytearray()
    position = 0
    size = len(data)
    while position < size:
        byte = data[position]
        position += 1
        if 1 <= byte <= 8:
            output.extend(data[position:position + byte])
            position += byte
        elif byte < 128:
            output.append(byte)
        elif byte >= 192:
            output.extend((32, byte ^ 128))
        elif position < size:
            pair = (byte << 8) | data[position]
            position += 1
            distance = (pair >> 3) & 0x7FF
            count = (pair & 7) + 3
            if not output:
                continue  # 没有可回溯的历史，跳过这段引用而不是毁掉整本书
            if distance == 0:
                distance = 1  # 畸形但常见的旧书用 0 表示距离 1
            if distance > len(output):
                continue  # 越界回溯（部分老书跨记录引用），按空引用处理
            if distance >= count:
                start = len(output) - distance
                output.extend(output[start:start + count])
            else:
                # 距离小于长度时源区间与自己重叠，必须逐字节复制，
                # 否则 distance == 1 的重复字符会被整段丢掉。
                for _ in range(count):
                    output.append(output[-distance])
    return bytes(output)


def extract_document(filename: str, data: bytes) -> dict:
    """Return display metadata and plain text for one supported local document."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = "、".join(ext[1:].upper() for ext in sorted(SUPPORTED_EXTENSIONS))
        raise DocumentImportError(f"支持的文件类型：{supported}。")
    if not data:
        raise DocumentImportError("文件为空。")
    if len(data) > MAX_IMPORT_BYTES:
        raise DocumentImportError("文件不能超过 80 MB。")
    if suffix == ".txt":
        text = _decode_text(data)
    elif suffix == ".pdf":
        text = _extract_pdf(data)
    elif suffix == ".epub":
        text = _extract_epub(data)
    else:
        text = _extract_mobi(data)
    if not text:
        raise DocumentImportError("没有从文件中提取到可朗读的文本。")
    return {"title": Path(filename).stem, "type": suffix[1:], "text": text}
