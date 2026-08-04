"""Local credential isolation contracts for the PostgreSQL runtime."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from seo_control.infrastructure.credential_vault import CredentialVault  # noqa: E402
from seo_control.infrastructure.database import initialize_database  # noqa: E402
from seo_control.infrastructure.runtime_postgres import PostgresRuntimeMigration  # noqa: E402
from seo_control.web import create_server  # noqa: E402


class CredentialVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "workspace.sqlite3"
        self.vault_path = self.root / "runtime-credentials.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_empty_or_uninitialized_source_does_not_block_vault_startup(self) -> None:
        sqlite3.connect(self.source).close()
        vault = CredentialVault(self.vault_path, import_database_path=self.source)
        self.assertEqual(0, vault.readiness()["wordpress_credentials"])
        self.assertEqual(0, vault.readiness()["gsc_oauth_credentials"])

    def test_existing_ciphertext_is_imported_only_into_local_vault(self) -> None:
        connection = initialize_database(self.source)
        with connection:
            project_id = int(connection.execute(
                "INSERT INTO projects(name,site_url) VALUES(?,?)", ("Vault", "https://vault.test")
            ).lastrowid)
            connection.execute(
                "INSERT INTO project_wordpress_configs(project_id,site_url,username,application_password) VALUES(?,?,?,?)",
                (project_id, "https://vault.test", "editor", "dpapi:wordpress-ciphertext"),
            )
            connection.execute(
                "INSERT INTO gsc_oauth_connection(id,refresh_token) VALUES(1,?)",
                ("dpapi:gsc-ciphertext",),
            )
        connection.close()

        vault = CredentialVault(self.vault_path, import_database_path=self.source)
        self.assertEqual(1, vault.readiness()["wordpress_credentials"])
        self.assertEqual(1, vault.readiness()["gsc_oauth_credentials"])
        target = vault.connect()
        try:
            self.assertEqual(
                "dpapi:wordpress-ciphertext",
                target.execute("SELECT application_password FROM wordpress_credentials").fetchone()[0],
            )
        finally:
            target.close()

        report = PostgresRuntimeMigration(
            self.source, "postgresql://user:password@localhost/isolated"
        ).preview().as_dict()
        serialized = json.dumps(report)
        self.assertNotIn("wordpress-ciphertext", serialized)
        self.assertNotIn("gsc-ciphertext", serialized)

    def test_vault_accepts_credentials_for_a_postgres_only_project_id(self) -> None:
        vault = CredentialVault(self.vault_path)
        connection = vault.connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO wordpress_credentials(project_id,site_url,username,application_password) VALUES(?,?,?,?)",
                    (987654, "https://postgres-only.test", "editor", "dpapi:ciphertext"),
                )
        finally:
            connection.close()
        self.assertEqual(1, vault.readiness()["wordpress_credentials"])

    def test_project_delete_removes_its_local_wordpress_credential(self) -> None:
        server = create_server(
            "127.0.0.1", 0, database_path=self.source,
            credential_vault_path=self.vault_path,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            create = Request(
                root + "/api/projects",
                data=json.dumps({"name": "Credential cleanup"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urlopen(create, timeout=10) as response:
                project_id = int(json.load(response)["id"])
            vault = server.credential_vault
            connection = vault.connect()
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO wordpress_credentials(project_id,site_url,username,application_password) VALUES(?,?,?,?)",
                        (project_id, "https://cleanup.test", "editor", "dpapi:ciphertext"),
                    )
            finally:
                connection.close()
            delete = Request(root + f"/api/projects/{project_id}", method="DELETE")
            with urlopen(delete, timeout=10):
                pass
            self.assertEqual(0, vault.readiness()["wordpress_credentials"])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
