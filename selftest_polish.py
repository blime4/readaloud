"""Offline regression checks for the polish and prepare routes."""

import hashlib
import json
import unittest
from unittest.mock import patch

import server


class PolishTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        for name, result in (
            ("pick_models", ["test-model"]),
            ("ai_cache_get", None),
            ("ai_cache_put", None),
            ("list_voices", []),
        ):
            mock = patch.object(server, name, return_value=result)
            mock.start()
            self.addCleanup(mock.stop)

    def test_polish_uses_new_cache_key(self):
        text, model, limit = "正文", "test-model", 300
        pref = json.dumps(server.CFG.get("prefs") or {}, ensure_ascii=False, sort_keys=True)
        for mode in ("polish", "summarize"):
            previous = f"{mode}|{model}|{limit}|{len(text)}|{pref}|{text}"
            old_key = hashlib.sha1(previous.encode("utf-8")).hexdigest()[:24]
            key = server._ai_key(text, mode, model, limit)
            if mode == "polish":
                self.assertNotEqual(key, old_key)
            else:
                self.assertEqual(key, old_key)

    def test_only_qwen35_polish_disables_thinking(self):
        for model, mode in (
            ("qwen3.5:9b", "polish"),
            ("qwen3.5:9b", "summarize"),
            ("qwen3:4b", "polish"),
        ):
            with self.subTest(model=model, mode=mode), patch.object(
                server, "pick_models", return_value=[model]
            ), patch.object(server.requests, "post") as post:
                content = json.dumps({"text": "这是一段朗读稿。"}) if mode == "polish" else "这是一段摘要。"
                post.return_value.json.return_value = {"message": {"content": content}}
                response = self.client.post("/api/ai", json={"text": "原文", "mode": mode, "model": model})
                self.assertEqual(response.status_code, 200)
                payload = post.call_args.kwargs["json"]
                if model == "qwen3.5:9b" and mode == "polish":
                    self.assertIs(payload.get("think"), False)
                else:
                    self.assertNotIn("think", payload)

    def test_prepare_rejects_truncated_polish_and_reads_clean_source(self):
        text = "Adam's apple\n\n**释义**　喉结\n\n" + "这里讲述这个英语词组的历史。" * 20
        short = "Adam's apple，意思是喉结。\n\n" + "这里讲述这个英语词组的历史。" * 7
        with patch.object(server, "ollama_chat", return_value=json.dumps({"text": short})):
            data = self.client.post("/api/prepare", json={"text": text, "ai": "polish"}).get_json()
        self.assertEqual(data["ai"], "none")
        self.assertIn("截断", data["ai_note"])
        self.assertIn("喉结", data["text"])
        self.assertEqual(data["text"].count("这里讲述"), 20)
        self.assertFalse(data["long_doc"])

    def test_dictionary_markers_are_cleaned_before_and_after_model(self):
        source = (
            "Adam's apple\n"
            "*/ˌædəmz 'æpəl/ · n*\n\n"
            '**释义**　喉结\n\n{% hint style="info" %} **词源**\n\n'
            "这个说法来自亚当和夏娃的故事。 {% endhint %}*&#x20;"
        )
        generated = source.replace("**释义**　喉结", "它的意思是喉结。")
        with patch.object(server, "ollama_chat", return_value=json.dumps({"text": generated})) as chat:
            polish = self.client.post("/api/ai", json={"text": source, "mode": "polish"})
            prepare = self.client.post("/api/prepare", json={"text": source, "ai": "polish"})
        self.assertEqual(polish.status_code, 200)
        self.assertEqual(prepare.get_json()["ai"], "polish")
        for text in (
            chat.call_args.args[1],
            polish.get_json()["text"],
            prepare.get_json()["text"],
        ):
            self.assertIn("Adam's apple", text)
            self.assertIn("喉结", text)
            self.assertIn("这个说法来自亚当和夏娃的故事", text)
            self.assertNotRegex(text, r"ˌædəmz|{%|&#x20;")

    def test_polish_accepts_many_valid_paragraphs(self):
        paragraphs = [f"词条 {index}。 " + "这是一段关于这个表达的朗读文字。" * 9 for index in range(20)]
        text = "\n\n".join(paragraphs)
        self.assertGreater(len(text), 2500)
        with patch.object(server, "ollama_chat", return_value=json.dumps({"text": text})):
            response = self.client.post("/api/ai", json={"text": text, "mode": "polish"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["text"], text)

    def test_long_polish_does_not_call_the_model(self):
        text = "# 词源\n\n" + "这个词源有一段历史。" * 20
        with patch.dict(server.CFG, {"long_doc_chars": 100}), patch.object(server, "ollama_chat") as chat:
            data = self.client.post("/api/prepare", json={"text": text, "ai": "polish"}).get_json()
            response = self.client.post("/api/ai", json={"text": text, "mode": "polish"})
        chat.assert_not_called()
        self.assertEqual(data["ai"], "none")
        self.assertFalse(data["long_doc"])
        self.assertIn("分段", data["ai_note"])
        self.assertEqual(data["items"][0]["kind"], "heading")
        self.assertEqual(response.status_code, 502)
        self.assertIn("分段", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
