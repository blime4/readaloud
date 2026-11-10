"""书架与语音引擎的回归检查。

书架必须做到三件事：正文真的落盘、同一本书不会重复入库（进度不丢）、
删一本不会牵连别的。语音引擎要能在 edge 与本机 TTS 之间切换，
并且各自的缓存不能串味。
"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bookshelf
import server


class FakeTTS(BaseHTTPRequestHandler):
    """一个最小的 OpenAI 兼容 TTS：够验证探测、合成、缓存三条路径。"""

    spoken = []
    fail = False

    def log_message(self, *_args):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._json({"object": "list", "data": [{"id": "fake-tts"}]})
        if self.path == "/v1/audio/voices":
            return self._json({"voices": ["xiaobei", "xiaoni"]})
        if self.path == "/health":
            return self._json({"status": "ok", "engine": "fake"})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.fail:
            return self._json({"error": "模型没加载"}, 500)
        type(self).spoken.append(body)
        audio = b"ID3" + b"\x00" * 64
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


class ShelfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_books = bookshelf.BOOKS_DIR
        bookshelf.BOOKS_DIR = Path(self.tmp.name) / "books"
        self.client = server.app.test_client()

    def tearDown(self):
        bookshelf.BOOKS_DIR = self.old_books
        self.tmp.cleanup()

    def add(self, title="烧纸", text="第一句。第二句。"):
        return bookshelf.add_book(title, text, "txt", "烧纸.mobi")

    def test_add_and_read_back(self):
        book = self.add()
        self.assertEqual(book["title"], "烧纸")
        self.assertEqual(book["chars"], len("第一句。第二句。"))
        got = bookshelf.get_book(book["id"])
        self.assertEqual(got["text"], "第一句。第二句。")
        self.assertEqual(len(bookshelf.list_books()), 1)

    def test_same_content_is_not_duplicated(self):
        first = self.add()
        bookshelf.save_progress(first["id"], 7, 20)
        again = self.add(title="换个名字")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(bookshelf.list_books()), 1)
        # 重复导入不该把进度清零
        self.assertEqual(again["progress"]["next"], 7)
        self.assertEqual(again["title"], "换个名字")

    def test_progress_and_percent(self):
        book = self.add()
        bookshelf.save_progress(book["id"], 5, 20)
        listed = bookshelf.list_books()[0]
        self.assertEqual(listed["progress"]["pct"], 25)
        self.assertFalse(listed["finished"])
        bookshelf.save_progress(book["id"], 20, 20)
        self.assertTrue(bookshelf.list_books()[0]["finished"])

    def test_progress_can_go_backwards(self):
        book = self.add()
        bookshelf.save_progress(book["id"], 9, 20)
        bookshelf.save_progress(book["id"], 2, 20)
        self.assertEqual(bookshelf.list_books()[0]["progress"]["next"], 2)

    def test_list_orders_by_recent(self):
        first = self.add("旧书", "旧书正文")
        second = self.add("新书", "新书正文")
        bookshelf.save_progress(first["id"], 1, 10)   # 翻过旧书 → 它应该排前面
        self.assertEqual(bookshelf.list_books()[0]["id"], first["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_rejects_bad_id(self):
        for bad in ("../etc", "..", "abc", "", "0123456789ABCDEF"):
            with self.assertRaises(bookshelf.BookError):
                bookshelf.get_book(bad)
            with self.assertRaises(bookshelf.BookError):
                bookshelf.delete_book(bad)

    def test_delete_only_removes_one(self):
        first = self.add("甲", "甲正文")
        second = self.add("乙", "乙正文")
        bookshelf.delete_book(first["id"])
        ids = [b["id"] for b in bookshelf.list_books()]
        self.assertEqual(ids, [second["id"]])

    def test_rejects_empty_text(self):
        with self.assertRaises(bookshelf.BookError):
            bookshelf.add_book("空书", "   ")

    def test_endpoints(self):
        book = self.add()
        listed = self.client.get("/api/books").json
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["count"], 1)
        got = self.client.get(f"/api/books/{book['id']}").json
        self.assertEqual(got["book"]["text"], "第一句。第二句。")
        saved = self.client.post(f"/api/books/{book['id']}/progress",
                                 json={"next": 1, "total": 2}).json
        self.assertEqual(saved["book"]["progress"]["pct"], 50)
        self.assertEqual(self.client.delete(f"/api/books/{book['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/books/{book['id']}").status_code, 404)

    def test_import_lands_on_shelf(self):
        response = self.client.post(
            "/api/import",
            data={"file": (__import__("io").BytesIO("导入的正文".encode()), "笔记.txt")},
            content_type="multipart/form-data",
        )
        payload = response.json
        self.assertTrue(payload["ok"])
        self.assertIsNotNone(payload["book"])
        self.assertEqual(payload["book"]["chars"], 5)
        self.assertEqual(payload["book"]["source"], "笔记.txt")


class TTSProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeTTS)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache = server.TTS_CACHE
        self.old_config_path = server.CONFIG_PATH
        server.TTS_CACHE = Path(self.tmp.name)
        # 这些用例会保存语音引擎设置，别写回用户真正的 config.json
        server.CONFIG_PATH = Path(self.tmp.name) / "config.json"
        self.saved = {k: server.CFG.get(k) for k in
                      ("tts_provider", "tts_local_url", "tts_local_model",
                       "tts_local_voice", "tts_local_key", "tts_local_format")}
        server.CFG.update({
            "tts_local_url": f"http://127.0.0.1:{self.port}",
            "tts_local_model": "fake-tts", "tts_local_voice": "xiaobei",
            "tts_local_key": "", "tts_local_format": "mp3",
        })
        FakeTTS.spoken.clear()
        FakeTTS.fail = False
        self.client = server.app.test_client()

    def tearDown(self):
        for key, value in self.saved.items():
            server.CFG[key] = value
        server.TTS_CACHE = self.old_cache
        server.CONFIG_PATH = self.old_config_path
        self.tmp.cleanup()

    def test_probe_reports_models_and_voices(self):
        payload = self.client.get("/api/tts/providers").json
        self.assertTrue(payload["ok"])
        self.assertEqual({p["id"] for p in payload["providers"]}, {"edge", "local"})
        self.assertTrue(payload["local"]["ok"])
        self.assertEqual(payload["local"]["models"], ["fake-tts"])
        self.assertEqual(payload["local"]["voices"], ["xiaobei", "xiaoni"])

    def test_local_synthesis_and_cache(self):
        url = "/api/tts?provider=local&text=你好&voice=xiaobei&rate=%2B8%25"
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.mimetype, "audio/mpeg")
        self.assertTrue(first.data.startswith(b"ID3"))
        self.assertEqual(len(FakeTTS.spoken), 1)
        self.assertEqual(FakeTTS.spoken[0]["input"], "你好")
        self.assertAlmostEqual(FakeTTS.spoken[0]["speed"], 1.08, places=3)
        # 同样的请求应命中缓存，不再打后端
        self.client.get(url)
        self.assertEqual(len(FakeTTS.spoken), 1)
        # 换音色就是另一份音频
        self.client.get("/api/tts?provider=local&text=你好&voice=xiaoni")
        self.assertEqual(len(FakeTTS.spoken), 2)

    def test_local_failure_is_reported(self):
        FakeTTS.fail = True
        response = self.client.get("/api/tts?provider=local&text=你好")
        self.assertEqual(response.status_code, 502)
        self.assertIn("500", response.json["error"])

    def test_unknown_provider_is_rejected(self):
        response = self.client.get("/api/tts?provider=espeak&text=你好")
        self.assertEqual(response.status_code, 400)

    def test_bad_url_is_rejected(self):
        server.CFG["tts_local_url"] = "ftp://nope"
        response = self.client.post("/api/tts/providers", json={"probe": True})
        self.assertEqual(response.status_code, 400)


class LangGuardTests(unittest.TestCase):
    """开了局域网之后，本机之外来的请求必须带访问码。"""

    def setUp(self):
        self.saved = {k: server.CFG.get(k) for k in ("host", "lan_token")}
        server.CFG["host"] = "0.0.0.0"
        server.CFG["lan_token"] = "sekret"

    def tearDown(self):
        for key, value in self.saved.items():
            server.CFG[key] = value

    def guard(self, path="/api/books", **kw):
        environ = {"REMOTE_ADDR": "192.168.1.50"}
        environ.update(kw.pop("environ", {}))
        with server.app.test_request_context(path, environ_overrides=environ, **kw):
            return server._guard_lan()

    def test_nonlocal_requests_need_token(self):
        response = self.guard()
        self.assertEqual(response[1], 401)
        self.assertTrue(response[0].json["need_key"])

    def test_token_in_query_header_or_body(self):
        self.assertIsNone(self.guard("/api/books?k=sekret"))
        self.assertIsNone(self.guard(headers={"X-ReadAloud-Key": "sekret"}))
        self.assertIsNone(self.guard(method="POST", json={"k": "sekret"}))

    def test_wrong_token_is_rejected(self):
        self.assertEqual(self.guard("/api/books?k=nope")[1], 401)

    def test_page_and_preflight_stay_open(self):
        self.assertIsNone(self.guard("/"))
        self.assertIsNone(self.guard("/manifest.json"))
        self.assertIsNone(self.guard(method="OPTIONS"))

    def test_loopback_never_needs_token(self):
        with server.app.test_request_context(
                "/api/books", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}):
            self.assertIsNone(server._guard_lan())


if __name__ == "__main__":
    unittest.main()
