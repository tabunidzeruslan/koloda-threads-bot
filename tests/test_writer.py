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

    def generate(self, prompt, system="", json_mode=False, search=False, temperature=0.9, images=None):
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


class NoSearchGM(FakeGM):
    def generate(self, prompt, system="", json_mode=False, search=False, temperature=0.9, images=None):
        if search:
            raise RuntimeError("HTTP 429 quota")
        if system == writer.FACTCHECK_NOSEARCH:
            return json.dumps({"ok": True})
        return super().generate(prompt, system, json_mode, search, temperature)


class NoSearchTests(unittest.TestCase):
    def test_fallback_without_search(self):
        post = writer.write_post(NoSearchGM(), today=date(2026, 11, 22), sell=False, recent=[], last_rubrics=[],
                                 rnd=random.Random(0), log=lambda *a: None)
        self.assertEqual(post["fact"], "nosearch")


if __name__ == "__main__":
    unittest.main()


class IllustrationPromptTest(unittest.TestCase):
    def test_firewood_detail_only_when_scene_has_wood(self):
        from bot import writer
        il = writer.load("illustration.json")
        with_wood = writer.render_prompt("Ivanovych stacks firewood by the fence.")
        self.assertIn(il["firewood_render"], with_wood)
        self.assertIn(il["render"], with_wood)
        plain = writer.render_prompt("Ivanovych watches snow fall with a thermos.")
        self.assertNotIn(il["firewood_render"], plain)

    def test_render_has_no_negative_lists(self):
        from bot import writer
        il = writer.load("illustration.json")
        for key in ("render", "firewood_render"):
            self.assertNotRegex(il[key], r"\b(No|Never|not)\b")


class MemeTest(unittest.TestCase):
    def test_compose_all_layouts(self):
        from bot import meme
        if not meme.available():
            self.skipTest("no Pillow")
        from pathlib import Path
        img = (Path(__file__).resolve().parent.parent / "data" / "ref" / "ivanovych" / "full.png").read_bytes()
        for layout in ("caption_above", "classic", "two_panel_vertical", "two_panel_horizontal"):
            out = meme.compose(layout, [img, img], ["Очікування: затишний вечір", "Реальність: дим"])
            self.assertTrue(out.startswith(b"\x89PNG"), layout)

    def test_meme_formats_valid(self):
        from bot import writer
        fm = writer.meme_formats()
        self.assertGreaterEqual(len(fm), 20)
        for f in fm.values():
            self.assertIn(f["layout"], ("caption_above", "classic", "two_panel_vertical", "two_panel_horizontal"))


class CastTest(unittest.TestCase):
    def test_references_per_cast(self):
        from bot.images import Images
        self.assertEqual(len(Images.references(["ivanovych"])), 2)
        two = Images.references(["ivanovych", "angelina"])
        self.assertEqual(len(two), 4)
        self.assertEqual({p.parent.name for p in two}, {"ivanovych", "angelina"})
        self.assertEqual(len(Images.references(["nobody"])), 0)


class ThreadsApiShapeTest(unittest.TestCase):
    def test_methods_exist(self):
        from bot.threads import Threads, metric_value
        for m in ("publish_text", "publish_image", "publish_reply", "refresh", "replies", "my_posts",
                  "account_stats", "post_stats"):
            self.assertTrue(callable(getattr(Threads, m, None)), m)
        self.assertEqual(metric_value({"total_value": {"value": 5}}), 5)
        self.assertEqual(metric_value({"values": [{"value": 2}, {"value": 3}]}), 5)


class MenuWordTest(unittest.TestCase):
    def test_plain_words(self):
        from bot.main import menu_word
        self.assertEqual(menu_word("хайп"), "menu_hype")
        self.assertEqual(menu_word("Хайп!"), "menu_hype")
        self.assertEqual(menu_word("🔥 Хайп"), "menu_hype")
        self.assertEqual(menu_word("Активувати"), "menu_on")
        self.assertIsNone(menu_word("Динамо програло"))
