"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
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
-- 规则文本协商与承诺跟踪：代表团与发言授权
CREATE TABLE IF NOT EXISTS delegations (
    delegation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL UNIQUE REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global','proposal','clause')),
    scope_id TEXT,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
-- 提案、条款与不可变文本快照
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    required_supports INTEGER CHECK(required_supports IS NULL OR required_supports >= 1),
    amendment_supports INTEGER NOT NULL DEFAULT 2 CHECK(amendment_supports >= 1),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clauses (
    clause_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','accepted','in_force')),
    current_version_id TEXT REFERENCES text_versions(version_id),
    created_at TEXT NOT NULL,
    UNIQUE(proposal_id, code)
);
CREATE TABLE IF NOT EXISTS text_versions (
    version_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    language TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('initial','amendment','editorial','translation')),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    supersedes_version_id TEXT REFERENCES text_versions(version_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS amendments (
    amendment_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    base_version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    proposed_version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    proposer_delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    status TEXT NOT NULL CHECK(status IN ('proposed','merged','withdrawn','conflicted')),
    merged_at TEXT,
    created_at TEXT NOT NULL
);
-- 同一基础快照至多有一个修订被合并：互相冲突的修订不能同时合并
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_merged_amendment
    ON amendments(clause_id, base_version_id) WHERE status='merged';
-- 立场：当前状态按（对象，代表团）唯一，重复表态/签署不增加支持数
CREATE TABLE IF NOT EXISTS stances (
    stance_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('amendment','clause')),
    subject_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    position TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_kind, subject_id, delegation_id)
);
-- 立场事实只追加：封存后可据此复核，不受后续文字整理影响
CREATE TABLE IF NOT EXISTS stance_events (
    fact_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    delegation_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    position TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    fact_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS statements (
    statement_id TEXT PRIMARY KEY,
    amendment_id TEXT,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    actor_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    made_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conflict_declarations (
    conflict_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','cleared')),
    declared_at TEXT NOT NULL,
    cleared_at TEXT,
    cleared_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_open_conflict
    ON conflict_declarations(actor_id, clause_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','withdrawn')),
    created_at TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_reservation
    ON reservations(clause_id, delegation_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS consensuses (
    consensus_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    support_count INTEGER NOT NULL,
    required_supports INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_ratification' CHECK(status IN ('pending_ratification')),
    formed_by TEXT NOT NULL,
    formed_at TEXT NOT NULL,
    UNIQUE(clause_id, version_id)
);
CREATE TABLE IF NOT EXISTS signatures (
    signature_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
    signed_at TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_signature
    ON signatures(clause_id, version_id, delegation_id) WHERE status='active';
-- 生效前置条件：按 sequence 顺序满足
CREATE TABLE IF NOT EXISTS ratification_conditions (
    condition_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    due_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','satisfied','lapsed')),
    evidence TEXT,
    satisfied_at TEXT,
    lapsed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(clause_id, delegation_id, sequence)
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    effective_at TEXT NOT NULL,
    UNIQUE(clause_id, delegation_id)
);
CREATE TABLE IF NOT EXISTS follow_up_actions (
    action_id TEXT PRIMARY KEY,
    clause_id TEXT NOT NULL REFERENCES clauses(clause_id),
    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','completed','overdue')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
-- 翻译对应关系
CREATE TABLE IF NOT EXISTS translation_links (
    link_id TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    target_version_id TEXT NOT NULL REFERENCES text_versions(version_id),
    translator_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL CHECK(status IN ('proposed','certified')),
    note TEXT NOT NULL DEFAULT '',
    certified_by TEXT REFERENCES actors(actor_id),
    certified_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_version_id, target_version_id)
);
-- 封存：同一范围并发封存只能有一个结果
CREATE TABLE IF NOT EXISTS seals (
    seal_id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    aggregate_hash TEXT NOT NULL,
    fact_count INTEGER NOT NULL,
    sealed_by TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    UNIQUE(scope_kind, scope_id)
);
CREATE TABLE IF NOT EXISTS sealed_facts (
    seal_id TEXT NOT NULL REFERENCES seals(seal_id),
    fact_kind TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    fact_hash TEXT NOT NULL,
    PRIMARY KEY(seal_id, fact_kind, fact_key)
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
        # 共享连接在多线程 HTTP 服务下需要进程内串行化写事务；
        # BEGIN IMMEDIATE 与唯一索引共同保证跨进程/跨线程只有一个写入结果。
        self.write_lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        with self.write_lock:
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
