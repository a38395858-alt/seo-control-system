"""Persistence adapters introduced during the staged PostgreSQL migration."""

from .collection import (
    CollectionMigrationService,
    CollectionRepository,
    PostgresCollectionRepository,
    SQLiteCollectionRepository,
)
from .evidence import (
    EvidenceMigrationService,
    EvidenceRepository,
    PostgresEvidenceRepository,
    SQLiteEvidenceRepository,
)
from .keywords import (
    KeywordMigrationService,
    KeywordRepository,
    PostgresKeywordRepository,
    SQLiteKeywordRepository,
)
from .legacy import (
    LegacyMigrationService,
    LegacyRepository,
    PostgresLegacyRepository,
    SQLiteLegacyRepository,
)
from .migration import SnapshotMigrationReport, SnapshotMigrationService
from .migration_suite import MigrationSuiteReport, PostgresMigrationSuite
from .projects import (
    PostgresProjectRepository,
    ProjectMigrationService,
    ProjectRepository,
    SQLiteProjectRepository,
)
from .workflow import (
    PostgresWorkflowRepository,
    SQLiteWorkflowRepository,
    WorkflowMigrationService,
    WorkflowRepository,
)
from .shadow import ShadowCollectionRepository, ShadowKeywordRepository, ShadowProjectRepository

__all__ = [
    "CollectionMigrationService",
    "CollectionRepository",
    "EvidenceMigrationService",
    "EvidenceRepository",
    "KeywordMigrationService",
    "KeywordRepository",
    "LegacyMigrationService",
    "LegacyRepository",
    "MigrationSuiteReport",
    "PostgresCollectionRepository",
    "PostgresEvidenceRepository",
    "PostgresKeywordRepository",
    "PostgresLegacyRepository",
    "PostgresProjectRepository",
    "PostgresMigrationSuite",
    "PostgresWorkflowRepository",
    "ProjectMigrationService",
    "ProjectRepository",
    "SQLiteCollectionRepository",
    "SQLiteEvidenceRepository",
    "SQLiteKeywordRepository",
    "SQLiteLegacyRepository",
    "SQLiteProjectRepository",
    "SQLiteWorkflowRepository",
    "SnapshotMigrationReport",
    "SnapshotMigrationService",
    "ShadowCollectionRepository",
    "ShadowKeywordRepository",
    "ShadowProjectRepository",
    "WorkflowMigrationService",
    "WorkflowRepository",
]
