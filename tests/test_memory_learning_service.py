from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
import hashlib
import json

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.application.memory_learning import MemoryLearningService
from seo_control.infrastructure.database import initialize_database


class MemoryLearningServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = initialize_database(Path(self.temp.name) / "memory-learning.sqlite3")
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            first = self.connection.execute("INSERT INTO projects(name) VALUES('first')")
            second = self.connection.execute("INSERT INTO projects(name) VALUES('second')")
            self.project_id = int(first.lastrowid)
            self.other_project_id = int(second.lastrowid)
            memory = self.connection.execute(
                """INSERT INTO competitor_content_memory(
                       project_id,normalized_url,url,domain,page_title,content,content_hash
                   ) VALUES(?,?,?,?,?,?,?)""",
                (self.project_id, "https://source.test/guide", "https://source.test/guide",
                 "source.test", "Outdoor step lighting guide", "Readable body " * 200, "hash-v1"),
            )
            self.memory_id = int(memory.lastrowid)
            version = self.connection.execute(
                """INSERT INTO competitor_content_versions(project_id,memory_id,content_hash,content,extractor)
                   VALUES(?,?,?,?, 'fixture')""",
                (self.project_id, self.memory_id, "hash-v1", "Readable body " * 200),
            )
            self.version_id = int(version.lastrowid)
            other = self.connection.execute(
                """INSERT INTO competitor_content_memory(
                       project_id,normalized_url,url,domain,page_title,content,content_hash
                   ) VALUES(?,?,?,?,?,?,?)""",
                (self.other_project_id, "https://other.test/guide", "https://other.test/guide",
                 "other.test", "Other project", "Other body " * 200, "other-hash"),
            )
            self.other_memory_id = int(other.lastrowid)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _documents(self) -> list[dict[str, str]]:
        return [{
            "source_id": f"competitor-memory-{self.memory_id}",
            "url": "https://untrusted-model-value.test/will-be-ignored",
            "content_excerpt": "Untrusted model context",
        }]

    def test_saves_three_typed_cards_rejects_invalid_fact_and_binds_versions(self) -> None:
        cards = [
            {"card_type": "writing_style", "topic": "outdoor step lighting", "summary": "Lead with constraints.", "source_ids": [f"competitor-memory-{self.memory_id}"], "quality_score": 0.8, "confidence_score": 0.82},
            {"card_type": "content_structure", "topic": "outdoor step lighting", "summary": "Use selection, installation and verification sections.", "source_ids": [f"competitor-memory-{self.memory_id}"], "quality_score": 0.76, "confidence_score": 0.78},
            {"card_type": "topic_gap", "topic": "outdoor step lighting", "summary": "Add a maintenance decision checkpoint.", "source_ids": [f"competitor-memory-{self.memory_id}"], "quality_score": 0.7, "confidence_score": 0.68},
            {"card_type": "fact", "topic": "unsafe fact", "summary": "A competitor claim.", "source_ids": [f"competitor-memory-{self.memory_id}"]},
            {"card_type": "writing_style", "topic": "invented", "summary": "No source.", "source_ids": ["competitor-memory-999999"]},
        ]
        with self.connection:
            result = MemoryLearningService(self.connection, project_id=self.project_id).save_competitor_cards(
                cards, source_documents=self._documents(), learning_run_id=9,
            )

        self.assertEqual(3, result.created_count)
        self.assertEqual(2, result.rejected_count)
        rows = self.connection.execute(
            "SELECT memory_type,card_type,evidence_count,inference_level FROM content_learning_memories WHERE project_id=? ORDER BY id",
            (self.project_id,),
        ).fetchall()
        self.assertEqual(
            [("style", "writing_style"), ("editorial", "content_structure"), ("editorial", "topic_gap")],
            [(row["memory_type"], row["card_type"]) for row in rows],
        )
        self.assertTrue(all(row["evidence_count"] == 1 and row["inference_level"] == "synthesized" for row in rows))
        sources = self.connection.execute(
            "SELECT source_url,source_content_hash,source_version_id,evidence_excerpt FROM content_learning_memory_sources WHERE project_id=?",
            (self.project_id,),
        ).fetchall()
        self.assertEqual(3, len(sources))
        self.assertTrue(all(row["source_url"] == "https://source.test/guide" for row in sources))
        self.assertTrue(all(row["source_version_id"] == self.version_id for row in sources))
        self.assertTrue(all("Readable body" not in row["evidence_excerpt"] for row in sources))

    def test_repeated_learning_updates_cards_and_new_content_hash_appends_evidence(self) -> None:
        card = {"topic": "outdoor step lighting", "summary": "Lead with constraints.", "source_ids": [f"competitor-memory-{self.memory_id}"]}
        service = MemoryLearningService(self.connection, project_id=self.project_id)
        with self.connection:
            first = service.save_competitor_cards([card], source_documents=self._documents(), learning_run_id=1)
            repeated = service.save_competitor_cards([card], source_documents=self._documents(), learning_run_id=2)
        self.assertEqual(1, first.created_count)
        self.assertEqual(0, repeated.created_count)
        self.assertEqual(1, repeated.updated_count)

        with self.connection:
            self.connection.execute(
                "UPDATE competitor_content_memory SET content_hash='hash-v2',content=? WHERE id=? AND project_id=?",
                ("Changed readable body " * 200, self.memory_id, self.project_id),
            )
            self.connection.execute(
                "INSERT INTO competitor_content_versions(project_id,memory_id,content_hash,content,extractor) VALUES(?,?,?,?, 'fixture')",
                (self.project_id, self.memory_id, "hash-v2", "Changed readable body " * 200),
            )
            changed = service.save_competitor_cards([card], source_documents=self._documents(), learning_run_id=3)
        self.assertEqual(1, changed.updated_count)
        memory_id = changed.memory_ids[0]
        count = self.connection.execute(
            "SELECT COUNT(*) FROM content_learning_memory_sources WHERE project_id=? AND memory_id=?",
            (self.project_id, memory_id),
        ).fetchone()[0]
        self.assertEqual(2, count)

    def test_cross_project_source_is_rejected(self) -> None:
        service = MemoryLearningService(self.connection, project_id=self.project_id)
        card = {"card_type": "writing_style", "topic": "other", "summary": "Do not save.", "source_ids": [f"competitor-memory-{self.other_memory_id}"]}
        with self.connection:
            result = service.save_competitor_cards(
                [card], source_documents=[{"source_id": f"competitor-memory-{self.other_memory_id}"}], learning_run_id=4,
            )
        self.assertEqual(1, result.rejected_count)
        self.assertEqual(0, result.created_count)

    def test_first_v2_learning_upgrades_matching_legacy_card_in_place(self) -> None:
        topic = "outdoor step lighting"
        legacy_signature = hashlib.sha256(json.dumps({
            "topic": topic,
            "urls": ["https://source.test/guide"],
        }, ensure_ascii=False).encode("utf-8")).hexdigest()
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO content_learning_memories(
                       project_id,memory_type,topic,summary,source_url,source_content_hash,quality_score
                   ) VALUES(?, 'style', ?, 'Legacy summary', ?, ?, 0.7)""",
                (self.project_id, topic, "https://source.test/guide", f"collected-competitor:{legacy_signature}"),
            )
            legacy_id = int(cursor.lastrowid)
            result = MemoryLearningService(self.connection, project_id=self.project_id).save_competitor_cards(
                [{"card_type": "writing_style", "topic": topic, "summary": "Upgraded summary", "source_ids": [f"competitor-memory-{self.memory_id}"]}],
                source_documents=self._documents(), learning_run_id=5,
            )
        self.assertEqual(0, result.created_count)
        self.assertEqual(1, result.updated_count)
        self.assertEqual((legacy_id,), result.memory_ids)
        row = self.connection.execute("SELECT card_type,source_content_hash FROM content_learning_memories WHERE id=?", (legacy_id,)).fetchone()
        self.assertEqual("writing_style", row["card_type"])
        self.assertTrue(row["source_content_hash"].startswith("competitor-card-v2:"))


if __name__ == "__main__":
    unittest.main()
