import unittest

from bot.channel_docs import INITIAL_SETUP_DOC, WORKFLOWS_DOC, chunk_markdown


class ChunkTests(unittest.TestCase):
    def test_short_docs_are_one_message(self):
        self.assertEqual(chunk_markdown("# Title\n\nBody"), ["# Title\n\nBody"])

    def test_long_docs_split_on_blank_lines_under_the_limit(self):
        paragraphs = [f"Paragraph {i} " + "x" * 300 for i in range(10)]
        chunks = chunk_markdown("\n\n".join(paragraphs), limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertEqual("\n\n".join(chunks), "\n\n".join(paragraphs))


class DocTests(unittest.TestCase):
    def test_both_docs_fit_discord_when_chunked(self):
        for path in (INITIAL_SETUP_DOC, WORKFLOWS_DOC):
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# "), path)
            self.assertTrue(all(len(chunk) <= 1900 for chunk in chunk_markdown(text)), path)


if __name__ == "__main__":
    unittest.main()
