"""Regression checks for local document extraction."""

import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import bookshelf
import document_import
import server


class BookshelfIsolated(unittest.TestCase):
    """凡是走 /api/import 的用例都会自动入库，别把测试书写进真书架。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = bookshelf.BOOKS_DIR
        bookshelf.BOOKS_DIR = Path(self._tmp.name) / "books"

    def tearDown(self):
        bookshelf.BOOKS_DIR = self._old
        self._tmp.cleanup()


def make_mobi(text, *, compression=1, encryption=0, extra=b"", record_size=4096):
    """Make a tiny PalmDOC/MOBI book for parser coverage.

    只处理单条正文记录：compression=1 时正文原样写入，compression=2 时用
    字面量块编码（不需要真正的压缩器）。extra 模拟转换工具留下的尾部填充。
    """
    payload = text.encode("utf-8") if isinstance(text, str) else text
    body = palmdoc_literals(payload) if compression == 2 else payload
    record0 = bytearray(32)
    struct.pack_into(">HHIHHI", record0, 0, compression, 0, len(payload), 1,
                     record_size, encryption << 16)
    record0[16:24] = b"MOBI" + (232).to_bytes(4, "big")
    record0.extend(b"\0" * (248 - len(record0)))
    offsets = [94, 94 + len(record0)]
    header = bytearray(78 + 16)
    header[:8] = b"TestBook"
    header[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", header, 76, 2)
    for index, offset in enumerate(offsets):
        struct.pack_into(">I", header, 78 + index * 8, offset)
    return bytes(header) + bytes(record0) + body + extra


def palmdoc_literals(data):
    """把字节流编成 PalmDOC 字面量块（每块最多 8 字节）。"""
    out = bytearray()
    for start in range(0, len(data), 8):
        chunk = data[start:start + 8]
        out.append(len(chunk))
        out.extend(chunk)
    return bytes(out)


def palmdoc_pair(distance, count):
    """生成一个回溯引用：distance 最大 2047，count 3..10。"""
    assert 0 < distance < 2048 and 3 <= count <= 10
    pair = ((distance & 0x7FF) << 3) | (count - 3) | 0x8000
    return bytes([pair >> 8, pair & 0xFF])


class DocumentImportTests(BookshelfIsolated):
    def test_extracts_utf8_txt(self):
        result = document_import.extract_document("notes.txt", "第一行\n第二行".encode())
        self.assertEqual(result["text"], "第一行\n第二行")
        self.assertEqual(result["type"], "txt")

    def test_extracts_plain_mobi(self):
        result = document_import.extract_document("book.mobi", make_mobi("<html><body>烧纸 正文</body></html>"))
        self.assertEqual(result["type"], "mobi")
        self.assertIn("烧纸 正文", result["text"])

    def test_extracts_pdf(self):
        import fitz
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "ReadAloud PDF import")
        result = document_import.extract_document("book.pdf", document.tobytes())
        self.assertEqual(result["type"], "pdf")
        self.assertIn("ReadAloud PDF import", result["text"])
        document.close()

    def test_extracts_epub(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("META-INF/container.xml", '''<?xml version="1.0"?>
                <container><rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles></container>''')
            archive.writestr("OPS/package.opf", '''<?xml version="1.0"?>
                <package><manifest><item id="chapter" href="chapter.xhtml"/></manifest>
                <spine><itemref idref="chapter"/></spine></package>''')
            archive.writestr("OPS/chapter.xhtml", "<h1>章节</h1><p>EPUB 导入内容</p>")
        result = document_import.extract_document("book.epub", buffer.getvalue())
        self.assertEqual(result["type"], "epub")
        self.assertIn("EPUB 导入内容", result["text"])

    def test_rejects_unknown_extension(self):
        with self.assertRaises(document_import.DocumentImportError):
            document_import.extract_document("notes.doc", b"ignored")

    def test_rejects_epub_zip_bomb(self):
        buffer = io.BytesIO()
        original_limit = document_import.MAX_EPUB_EXPANDED_BYTES
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("oversized.xhtml", b"x" * 21)
        try:
            document_import.MAX_EPUB_EXPANDED_BYTES = 20
            with self.assertRaisesRegex(document_import.DocumentImportError, "解压后的内容过大"):
                document_import.extract_document("book.epub", buffer.getvalue())
        finally:
            document_import.MAX_EPUB_EXPANDED_BYTES = original_limit

    def test_import_endpoint_returns_text(self):
        client = server.app.test_client()
        response = client.post(
            "/api/import",
            data={"file": (io.BytesIO("导入成功".encode()), "notes.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["text"], "导入成功")

    def test_import_endpoint_requires_file(self):
        client = server.app.test_client()
        response = client.post("/api/import", data={}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["ok"])

    def test_import_endpoint_rejects_oversized_upload(self):
        # 真正发 80 MB 太慢，改成把上限压到 1 KB 再超一点点。
        original = server.app.config["MAX_CONTENT_LENGTH"]
        server.app.config["MAX_CONTENT_LENGTH"] = 1024
        try:
            client = server.app.test_client()
            response = client.post(
                "/api/import",
                data={"file": (io.BytesIO(b"x" * 4096), "big.txt")},
                content_type="multipart/form-data",
            )
        finally:
            server.app.config["MAX_CONTENT_LENGTH"] = original
        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.json["ok"])

    def test_palmdoc_repeats_single_byte(self):
        # distance == 1 是重复字符的常见写法，源区间与自己重叠。
        record = b"A" + palmdoc_pair(1, 3)
        self.assertEqual(document_import._unpack_palmdoc(record), b"AAAA")

    def test_palmdoc_overlapping_backreference(self):
        record = b"ab" + palmdoc_pair(2, 6)
        self.assertEqual(document_import._unpack_palmdoc(record), b"abababab")

    def test_palmdoc_handles_reference_without_history(self):
        record = palmdoc_pair(200, 4) + b"tail"
        self.assertEqual(document_import._unpack_palmdoc(record), b"tail")

    def test_extracts_compressed_mobi(self):
        result = document_import.extract_document(
            "book.mobi", make_mobi("<p>压缩正文</p>", compression=2))
        self.assertIn("压缩正文", result["text"])

    def test_mobi_trims_trailing_record_padding(self):
        # 转换工具常在记录尾部留几字节，不能当成正文。
        result = document_import.extract_document(
            "book.mobi", make_mobi("<p>正文到此处结束</p>", extra=b"\x00JUNK\xff"))
        self.assertEqual(result["text"], "正文到此处结束")

    def test_rejects_corrupt_mobi(self):
        with self.assertRaisesRegex(document_import.DocumentImportError, "MOBI 文件格式无效"):
            document_import.extract_document("book.mobi", b"BOOKMOBI" * 4)

    def test_rejects_drm_mobi(self):
        with self.assertRaisesRegex(document_import.DocumentImportError, "DRM"):
            document_import.extract_document(
                "book.mobi", make_mobi("<p>加密</p>", encryption=1))

    def test_rejects_unsupported_compression(self):
        with self.assertRaisesRegex(document_import.DocumentImportError, "HUFF"):
            document_import.extract_document(
                "book.mobi", make_mobi("<p>正文</p>", compression=17480))

    def test_rejects_corrupt_epub(self):
        with self.assertRaisesRegex(document_import.DocumentImportError, "EPUB"):
            document_import.extract_document("book.epub", b"definitely not a zip file")

    def test_rejects_corrupt_pdf(self):
        with self.assertRaisesRegex(document_import.DocumentImportError, "PDF"):
            document_import.extract_document("book.pdf", b"%PDF-1.4 truncated")

    def test_rejects_empty_file(self):
        with self.assertRaisesRegex(document_import.DocumentImportError, "文件为空"):
            document_import.extract_document("notes.txt", b"")

    def test_rejects_epub_with_too_many_entries(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for index in range(5):
                archive.writestr(f"page{index}.xhtml", "<p>x</p>")
        original = document_import.MAX_EPUB_FILES
        document_import.MAX_EPUB_FILES = 3
        try:
            with self.assertRaisesRegex(document_import.DocumentImportError, "解压后的内容过大"):
                document_import.extract_document("book.epub", buffer.getvalue())
        finally:
            document_import.MAX_EPUB_FILES = original


if __name__ == "__main__":
    unittest.main()
