from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.material_evidence import extract_candidates


class MaterialEvidenceTests(unittest.TestCase):
    def test_text_material_returns_literal_candidates_with_traceable_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            material = Path(directory) / "SK_hynix_Q2.txt"
            original = (
                "SK hynix announced that HBM revenue increased 50% year over year in the quarter.\n\n"
                "The company also discussed memory demand from AI servers and supply-chain capacity."
            )
            material.write_text(original, encoding="utf-8")
            candidates = extract_candidates(material, query="SK海力士业绩与AI服务器存储需求", limit=10)

        self.assertTrue(candidates)
        self.assertEqual(candidates[0].source, "SK_hynix_Q2.txt p1")
        self.assertEqual(candidates[0].reference, str(material.resolve()))
        self.assertIn(candidates[0].content.rstrip("…"), original)
        self.assertTrue(all(candidate.page == 1 for candidate in candidates))

    def test_unsupported_material_is_rejected_without_fallback_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            material = Path(directory) / "notice.html"
            material.write_text("<p>公告</p>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "仅支持"):
                extract_candidates(material)


if __name__ == "__main__":
    unittest.main()
