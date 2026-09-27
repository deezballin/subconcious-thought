"""Tests for lexical intent normalization."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from undermind.intents import (
    content_words,
    intent_id,
    is_repeat,
    normalize_text,
    signature,
    stem,
    tokenize,
)


class TestTokenize(unittest.TestCase):
    def test_lowercase_and_punctuation(self):
        tokens = tokenize("Hello, World! How's it going?")
        self.assertEqual(tokens, ["hello", "world", "how", "s", "it", "going"])

    def test_normalize_unicode(self):
        self.assertEqual(normalize_text("Café RÉSUMÉ"), "cafe resume")


class TestStem(unittest.TestCase):
    def test_plural(self):
        self.assertEqual(stem("bugs"), "bug")
        self.assertEqual(stem("buses"), "bus")

    def test_ing(self):
        self.assertEqual(stem("running"), "runn")

    def test_ed(self):
        self.assertEqual(stem("fixed"), "fix")

    def test_ly(self):
        self.assertEqual(stem("quickly"), "quick")

    def test_short_words_untouched(self):
        self.assertEqual(stem("is"), "is")
        self.assertEqual(stem("go"), "go")


class TestGrouping(unittest.TestCase):
    def test_different_phrasings_share_intent_id(self):
        id1 = intent_id("can you fix the login bug?")
        id2 = intent_id("please fix login bugs")
        self.assertEqual(id1, id2)

    def test_signature_is_sorted_content_words(self):
        self.assertEqual(signature("please fix the login bug"), "bug fix login")

    def test_stopwords_dropped(self):
        words = content_words("what is the status of the build")
        self.assertNotIn("what", words)
        self.assertNotIn("the", words)
        self.assertIn("status", words)
        self.assertIn("build", words)

    def test_intent_id_is_stable_hex(self):
        iid = intent_id("deploy the staging server")
        self.assertRegex(iid, re.compile(r"^[0-9a-f]{16}$"))
        self.assertEqual(iid, intent_id("Deploy the STAGING server!!"))

    def test_different_directions_differ(self):
        self.assertNotEqual(
            intent_id("fix the login bug"), intent_id("deploy the staging server")
        )

    def test_empty_and_stopword_only(self):
        self.assertEqual(signature(""), "")
        self.assertEqual(signature("the of and"), "")
        self.assertEqual(intent_id("the of and"), intent_id(""))

    def test_is_repeat(self):
        seen = {intent_id("write unit tests")}
        self.assertTrue(is_repeat("please write unit test", seen))
        self.assertFalse(is_repeat("delete the database", seen))


from undermind.intents import find_similar_intent, is_system_prompt, jaccard_similarity


class TestSystemPromptDetection(unittest.TestCase):
    def test_cron_envelope_detected(self):
        self.assertTrue(
            is_system_prompt(
                "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: ..."
            )
        )

    def test_human_text_not_flagged(self):
        self.assertFalse(is_system_prompt("remember the bridge dashboard drill"))
        self.assertFalse(is_system_prompt("fix the login bug please"))

    def test_empty_not_flagged(self):
        self.assertFalse(is_system_prompt(""))


class TestSimilarity(unittest.TestCase):
    def test_identical_signatures_score_one(self):
        self.assertAlmostEqual(
            jaccard_similarity("bridge smoke test", "bridge smoke test"), 1.0
        )

    def test_near_duplicate_scores_above_threshold(self):
        # "one" is the only differing word: 3 shared / 4 union = 0.75.
        score = jaccard_similarity("bridge one smoke test", "bridge smoke test")
        self.assertGreater(score, 0.6)
        self.assertLess(score, 1.0)

    def test_disjoint_scores_zero(self):
        self.assertEqual(jaccard_similarity("bug fix login", "bridge smoke test"), 0.0)

    def test_empty_signature_scores_zero(self):
        self.assertEqual(jaccard_similarity("", "bridge smoke test"), 0.0)

    def test_find_similar_intent_picks_best(self):
        existing = [("aaa", "bridge smoke test"), ("bbb", "bug fix login")]
        self.assertEqual(
            find_similar_intent("bridge one smoke test", existing, 0.6), "aaa"
        )

    def test_find_similar_intent_none_below_threshold(self):
        existing = [("aaa", "bug fix login")]
        self.assertIsNone(find_similar_intent("bridge smoke test", existing, 0.6))

    def test_find_similar_intent_disabled_at_zero(self):
        existing = [("aaa", "bridge smoke test")]
        self.assertIsNone(find_similar_intent("bridge smoke test", existing, 0.0))


if __name__ == "__main__":
    unittest.main()
