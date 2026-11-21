import json
import random
import unittest
from datetime import date

from bot import writer

GOOD = "Дуб чи граб? Знаю родини, де це питання гостріше за футбол.\nА у вас вдома хто перемагає?"
GOOD2 = "Холодна підлога о п'ятій ранку.\nНайчесніший будильник приватного будинку в листопаді."


class FakeGM:
    model = "fake"

    def __init__(self, fact_ok=True):
        self.fact_ok = fact_ok
        self.calls = []

    def generate(self, prompt, system="", json_mode=False, search=False, temperature=0.9):
        self.calls.append((system[:20], search))
        if system == writer.SYSTEM:
            return json.dumps({"variants": [{"rubric": "Дуб чи граб", "text": GOOD},
                                            {"rubric": "Зимові будні", "text": GOOD2},
                                            {"rubric": "x", "text": "Купуйте дрова #дрова 🔥 і ще трохи слів для довжини посту"}]})
        if system == writer.EDITOR:
            return json.dumps({"scores": [{"i": 0, "score": 6}, {"i": 1, "score": 8, "why": "живо"}]})
        if system == writer.FACTCHECK:
            assert search
            return json.dumps({"ok": self.fact_ok, "problems": [] if self.fact_ok else ["помилка"], "fixed_text": ""})
        raise AssertionError(system)


class WriterTests(unittest.TestCase):
    def test_pipeline_picks_best_passing(self):
        gm = FakeGM()
        post = writer.write_post(gm, today=date(2026, 11, 22), sell=False, recent=[], last_rubrics=[],
                                 rnd=random.Random(0), log=lambda *a: None)
        self.assertEqual(post["text"], GOOD2)
        self.assertEqual(post["score"], 8)
        self.assertEqual(post["fact"], "ok")

    def test_pipeline_rejects_bad_facts(self):
        gm = FakeGM(fact_ok=False)
        post = writer.write_post(gm, today=date(2026, 11, 22), sell=False, recent=[], last_rubrics=[],
                                 rnd=random.Random(0), log=lambda *a: None)
        self.assertIsNone(post)

    def test_prompt_has_date_and_season(self):
        p = writer.build_prompt(today=date(2026, 11, 22), rubrics=["Дуб чи граб"], sell=False, recent=[])
        self.assertIn("22 листопада 2026", p)
        self.assertIn("осінь", p)
