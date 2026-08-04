"""Project isolation and audit contracts for modular Agent tools."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from seo_control.application.agent_tools import AgentToolScopeError, AgentToolService
from seo_control.infrastructure.database import initialize_database


class AgentToolServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = initialize_database(Path(self.temp.name) / "agent-tools.sqlite3")
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            first = self.connection.execute("INSERT INTO projects(name,site_url) VALUES('first','https://first.example')")
            second = self.connection.execute("INSERT INTO projects(name,site_url) VALUES('second','https://second.example')")
            self.first_project = int(first.lastrowid)
            self.second_project = int(second.lastrowid)
            job = self.connection.execute(
                "INSERT INTO agent_jobs(project_id,requested_action,status,current_node) VALUES(?, 'generate_content', 'planning', 'created')",
                (self.first_project,),
            )
            self.job_id = int(job.lastrowid)
            self.connection.execute(
                """INSERT INTO content_learning_memories(
                       project_id,memory_type,topic,summary,evidence_json,source_url,quality_score,pinned
                   ) VALUES(?, 'style', 'outdoor step lighting', 'Explain safety before fixture selection.',
                            '{"source":"competitor","url":"https://source.example/article"}',
                            'https://source.example/article', 0.9, 0)""",
                (self.first_project,),
            )
            self.connection.execute(
                """INSERT INTO content_learning_memories(
                       project_id,memory_type,topic,summary,evidence_json,quality_score,pinned
                   ) VALUES(?, 'style', 'outdoor step lighting', 'Other project memory.', '{}', 1, 1)""",
                (self.second_project,),
            )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_tools_validate_scope_retrieve_only_project_memories_and_write_audits(self) -> None:
        service = AgentToolService(self.connection, project_id=self.first_project, job_id=self.job_id)
        context = service.invoke("load_project_context", {"project_id": self.first_project}).output
        memories = service.invoke(
            "retrieve_project_memories",
            {"project_id": self.first_project, "query": "outdoor step lighting safety", "limit": 7},
        ).output

        self.assertEqual(self.first_project, context["project"]["id"])
        self.assertEqual(1, memories["selected_count"])
        self.assertIn("Matched topic terms", memories["memories"][0]["selection_reason"])
        self.assertIn("Confidence", memories["memories"][0]["selection_reason"])
        self.assertEqual("general", memories["memories"][0]["card_type"])
        audits = self.connection.execute(
            "SELECT tool_name,status FROM agent_tool_audits WHERE project_id=? AND job_id=? ORDER BY id",
            (self.first_project, self.job_id),
        ).fetchall()
        self.assertEqual(
            [("load_project_context", "completed"), ("retrieve_project_memories", "completed")],
            [(row["tool_name"], row["status"]) for row in audits],
        )

    def test_tool_rejects_cross_project_payload_before_reading_data(self) -> None:
        service = AgentToolService(self.connection, project_id=self.first_project, job_id=self.job_id)
        with self.assertRaisesRegex(AgentToolScopeError, "does not match"):
            service.invoke("load_project_context", {"project_id": self.second_project})
        audit = self.connection.execute(
            "SELECT status,error_summary FROM agent_tool_audits WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (self.job_id,),
        ).fetchone()
        self.assertEqual("failed", audit["status"])
        self.assertIn("does not match", audit["error_summary"])

    def test_unimplemented_and_non_allowlisted_tools_fail_closed(self) -> None:
        service = AgentToolService(self.connection, project_id=self.first_project, job_id=self.job_id)
        with self.assertRaisesRegex(ValueError, "planned but not implemented"):
            service.invoke("generate_article", {"project_id": self.first_project})
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            service.invoke("read_database", {"project_id": self.first_project})
        audits = self.connection.execute(
            "SELECT tool_name,status FROM agent_tool_audits WHERE job_id=? ORDER BY id",
            (self.job_id,),
        ).fetchall()
        self.assertEqual(
            [("generate_article", "failed"), ("read_database", "failed")],
            [(row["tool_name"], row["status"]) for row in audits],
        )

    def test_failed_audit_redacts_secret_values(self) -> None:
        service = AgentToolService(self.connection, project_id=self.first_project, job_id=self.job_id)
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            service.invoke(
                "read_database",
                {"project_id": self.first_project, "authorization": "Bearer-private-value"},
            )
        audit = self.connection.execute(
            "SELECT input_json,error_summary FROM agent_tool_audits WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (self.job_id,),
        ).fetchone()
        self.assertEqual("[redacted]", json.loads(audit["input_json"])["authorization"])
        self.assertNotIn("private-value", audit["error_summary"] or "")

    def test_pinned_memory_cannot_cross_the_topic_boundary(self) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE content_learning_memories SET pinned=1 WHERE project_id=?",
                (self.first_project,),
            )
        service = AgentToolService(self.connection, project_id=self.first_project, job_id=self.job_id)
        result = service.invoke(
            "retrieve_project_memories",
            {"project_id": self.first_project, "query": "unrelated payroll taxation", "limit": 7},
        ).output
        self.assertEqual(0, result["selected_count"])


if __name__ == "__main__":
    unittest.main()
