"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dialogues (
    dialogue_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    quorum INTEGER NOT NULL CHECK(quorum >= 1),
    support_threshold REAL NOT NULL CHECK(support_threshold > 0 AND support_threshold <= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegations (
    delegation_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    can_sign INTEGER NOT NULL CHECK(can_sign IN (0, 1)),
    preconditions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(dialogue_id, organization_id)
);
CREATE TABLE IF NOT EXISTS speaking_grants (
    grant_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    title TEXT NOT NULL,
    proposer_delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clauses (
    clause_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    clause_key TEXT NOT NULL,
    head_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(proposal_id, clause_key)
);
CREATE TABLE IF NOT EXISTS clause_versions (
    version_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    language TEXT NOT NULL,
    text TEXT NOT NULL,
    amendment_id TEXT,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(clause_id, version_no)
);
CREATE TABLE IF NOT EXISTS translations (
    translation_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES clause_versions(version_id),
    language TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'verified', 'rejected')),
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_by TEXT,
    verified_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS translations_verified_unique
    ON translations(version_id, language) WHERE status = 'verified';
CREATE TABLE IF NOT EXISTS amendments (
    amendment_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    base_version_id TEXT NOT NULL REFERENCES clause_versions(version_id),
    proposer_delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    language TEXT NOT NULL,
    proposed_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'seconded', 'withdrawn', 'merged', 'conflicted')),
    merged_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS amendment_seconds (
    amendment_id TEXT NOT NULL REFERENCES amendments(amendment_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(amendment_id, delegation_id)
);
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    version_id TEXT NOT NULL REFERENCES clause_versions(version_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    stance TEXT NOT NULL CHECK(stance IN ('accept', 'conditional_accept', 'reserve', 'reject')),
    note TEXT,
    session_key TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, delegation_id)
);
CREATE TABLE IF NOT EXISTS statements (
    statement_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    session_key TEXT NOT NULL,
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    actor_id TEXT NOT NULL,
    clause_id TEXT REFERENCES clauses(clause_id),
    grant_id TEXT NOT NULL REFERENCES speaking_grants(grant_id),
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seals (
    seal_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    session_key TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    statement_count INTEGER NOT NULL,
    position_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dialogue_id, session_key)
);
CREATE TABLE IF NOT EXISTS consensus_records (
    consensus_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    version_id TEXT NOT NULL REFERENCES clause_versions(version_id),
    status TEXT NOT NULL CHECK(status IN ('pending_ratification', 'superseded')),
    tally_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(clause_id, version_id)
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    consensus_id TEXT NOT NULL REFERENCES consensus_records(consensus_id),
    dialogue_id TEXT NOT NULL,
    clause_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    stance TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'in_force')),
    effective_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(consensus_id, delegation_id)
);
CREATE TABLE IF NOT EXISTS commitment_conditions (
    condition_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    seq INTEGER NOT NULL CHECK(seq >= 0),
    kind TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'satisfied')),
    satisfied_at TEXT,
    satisfied_by TEXT,
    UNIQUE(commitment_id, seq)
);
CREATE TABLE IF NOT EXISTS followup_actions (
    action_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    clause_id TEXT REFERENCES clauses(clause_id),
    assignee_delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    title TEXT NOT NULL,
    detail TEXT,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'done', 'cancelled')),
    completed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coi_declarations (
    declaration_id TEXT PRIMARY KEY,
    dialogue_id TEXT NOT NULL REFERENCES dialogues(dialogue_id),
    clause_id TEXT REFERENCES clauses(clause_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    delegation_id TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'cleared')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cleared_at TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
