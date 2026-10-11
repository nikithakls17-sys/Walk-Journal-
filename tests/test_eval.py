import asyncio
import sys

from basecamp.app import gemma
from eval import run_eval
from tests.test_core import photo


def test_classify():
    assert run_eval.classify("accept", "pass") == "correct_accept"
    assert run_eval.classify("accept", "fail") == "false_reject"
    assert run_eval.classify("reject", "pass") == "false_accept"
    assert run_eval.classify("reject", "retake") == "correct_reject"
    assert run_eval.classify("reject", "unsure") == "couldnt_tell"


def test_eval_writes_report(tmp_path, monkeypatch):
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "leaf.jpg").write_bytes(photo())
    (tmp_path / "photos" / "blur.jpg").write_bytes(photo(sharp=False))
    labels = tmp_path / "labels.csv"
    labels.write_text("file,prompt,verify,category,expected\n"
                      "leaf.jpg,Find a red leaf,a red leaf,leaf,accept\n"
                      "blur.jpg,Find a stone,a stone,stone,reject\n"
                      "missing.jpg,Find x,x,other,accept\n", encoding="utf-8")

    async def available():
        return True

    async def chat(*a, **k):
        return {"match": True, "confidence": 0.8, "what_i_see": "a red leaf", "reason": "ok"}
    monkeypatch.setattr(gemma, "available", available)
    monkeypatch.setattr(gemma, "chat", chat)
    out = tmp_path / "EVAL.md"
    monkeypatch.setattr(sys, "argv", ["run_eval", "--labels", str(labels), "--photos", str(tmp_path / "photos"), "--out", str(out)])
    assert asyncio.run(run_eval.main()) == 0
    text = out.read_text(encoding="utf-8")
    assert "| Correct accept | 1 |" in text and "| Correct reject | 1 |" in text and "100%" in text
    assert "Skipped 1" in text
