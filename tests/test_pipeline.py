"""Comprehensive integration tests for Memory Bridge pipeline.

Tests cover:
- All scanners with synthetic mock data
- Edge cases: empty files, truncated JSON, binary garbage, unicode
- Content quality: low-value session filtering, extraction prompt quality
- Future-proofing: format version detection, schema migration, export/import
- Performance: batch processing, streaming discovery
- Deduplication via hash
"""
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

# Add src to path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scanner import Session, Message, discover_all, get_available_scanners
from src.scanners.base import discover_sessions, _validate_table_name
from src.scanners.claude_code import _parse_claude_jsonl, detect_claude_format_version
from src.scanners.aider import _parse_aider_markdown
from src.scanners.codex import scan_codex
from src.scanners.gemini import scan_gemini
from src.indexer import MemoryIndex, MemoryEntry, SCHEMA_VERSION, SCHEMA_MIGRATIONS
from src.extractor import (
    FastExtractor, SmartExtractor, is_low_value_session,
    get_extraction_metrics, filter_sessions_for_llm, NOISE_PATTERNS,
    extract_structured_decisions,
)


# ══════════════════════════════════════════════════════════════════════════
#  FIXTURES
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def index():
    """Create a temporary MemoryIndex for testing."""
    db_path = Path(tempfile.gettempdir()) / f"mb-test-{__name__}.db"
    if db_path.exists():
        db_path.unlink()
    for ext in [".db-wal", ".db-shm"]:
        p = db_path.with_suffix(ext)
        if p.exists():
            p.unlink()
    idx = MemoryIndex(db_path)
    yield idx
    idx.close()
    if db_path.exists():
        db_path.unlink()
    for ext in [".db-wal", ".db-shm"]:
        p = db_path.with_suffix(ext)
        if p.exists():
            p.unlink()


@pytest.fixture
def sample_session():
    """Create a fake agent session with decisions and preferences."""
    return Session(
        source="claude-code",
        session_id="test-session-001",
        project="/home/user/project",
        messages=[
            Message(role="user", content="Let's deploy to Cloudflare Workers for the API layer."),
            Message(role="assistant", content="I've decided we will use Cloudflare Workers for the API layer. It's the right call for edge deployment."),
            Message(role="user", content="I prefer explicit CORS origins — never use wildcard * in production."),
            Message(role="assistant", content="Noted. Your preference for explicit CORS origins will be followed. I've learned from the Razorpay OAuth failure: always use trailing slash in redirect_uri."),
        ],
    )


@pytest.fixture
def temp_home(tmp_path):
    """Create a temporary home directory with mock agent files."""
    base = tmp_path / "home"
    base.mkdir()
    return base


# ══════════════════════════════════════════════════════════════════════════
#  TEST: CORE PIPELINE
# ══════════════════════════════════════════════════════════════════════════

class TestPipeline:
    """Test the full scan → extract → index → search pipeline."""

    def test_scan_extract_index_search(self, index, sample_session):
        """End-to-end: session → FastExtractor → MemoryIndex → FTS search."""
        # Extract
        entries = FastExtractor.extract(sample_session)
        assert len(entries) > 0, "FastExtractor should find at least one fact"

        # Verify entry categories
        categories = {e.category for e in entries}
        assert "decision" in categories, "Should find decision about Cloudflare Workers"

        # Index
        for entry in entries:
            entry_id = index.upsert(entry)
            assert entry_id > 0, f"upsert should return valid ID, got {entry_id}"

        # Mark as processed
        index.mark_source_processed(
            sample_session.source,
            sample_session.session_id,
            len(sample_session.messages),
        )

        # Verify source tracking
        assert index.is_source_processed("claude-code", "test-session-001"), \
            "Session should be marked as processed"

        # Search
        results = index.search_fts("cloudflare")
        assert len(results) > 0, "Should find Cloudflare-related entries"

        # Verify result structure
        result = results[0]
        assert result.content, "Result should have content"
        assert result.category in {"fact", "decision", "preference", "lesson", "project", "person"}, \
            f"Invalid category: {result.category}"

        # Stats
        stats = index.stats()
        assert stats["total_entries"] > 0, "Should have entries"
        assert stats["processed_sessions"] == 1, "Should have 1 processed session"

    def test_generator_discovery(self, temp_home):
        """Generator-based discovery yields sessions without accumulating all in memory."""
        # Create mock session files
        cc_dir = temp_home / ".claude" / "projects" / "testproj"
        cc_dir.mkdir(parents=True)
        session_file = cc_dir / "session1.jsonl"
        session_file.write_text(
            json.dumps({"type": "user", "message": {"content": "Hello"}, "timestamp": "2024-01-01"}) + "\n"
        )

        # Use generator
        sessions = list(discover_sessions(temp_home))
        assert len(sessions) >= 1


# ══════════════════════════════════════════════════════════════════════════
#  TEST: DEDUPLICATION
# ══════════════════════════════════════════════════════════════════════════

class TestDedup:
    """Test that duplicate content is deduplicated."""

    def test_dedup(self, index):
        """Same content twice → one entry, reference_count incremented."""
        entry = MemoryEntry(
            content="Use Cloudflare R2 for photo storage",
            category="decision",
            source_agent="claude-code",
            source_session="test-dedup",
            importance=0.8,
        )

        id1 = index.upsert(entry)
        id2 = index.upsert(entry)

        assert id1 == id2, f"Same content should return same ID: {id1} != {id2}"

        # Verify only one row
        stats = index.stats()
        assert stats["total_entries"] == 1, f"Should have 1 entry, got {stats['total_entries']}"

    def test_hash_consistency(self):
        """Hash algorithm should be deterministic."""
        h1 = MemoryIndex.hash_content("Hello world")
        h2 = MemoryIndex.hash_content("Hello world")
        assert h1 == h2
        assert len(h1) == 16  # sha256[:16]

        h3 = MemoryIndex.hash_content("Different content")
        assert h1 != h3


# ══════════════════════════════════════════════════════════════════════════
#  TEST: FTS5 TRIGRAM SEARCH
# ══════════════════════════════════════════════════════════════════════════

class TestFTSTrigram:
    """Test FTS5 trigram tokenizer search behavior."""

    def test_exact_match(self, index):
        """Exact word match should return results."""
        index.upsert(MemoryEntry(
            content="Deploy the PhotoSelect backend to Cloud Run",
            category="decision",
            source_agent="claude-code",
            source_session="test-fts",
            importance=0.9,
        ))

        results = index.search_fts("PhotoSelect")
        assert len(results) > 0, "Exact match should find PhotoSelect"

    def test_substring_match(self, index):
        """Partial word match via trigrams should work."""
        index.upsert(MemoryEntry(
            content="Configure Cloudflare DNS for antharmaya.com",
            category="fact",
            source_agent="codex",
            source_session="test-fts-2",
            importance=0.5,
        ))

        # "cloud" should match "Cloudflare" via trigrams
        results = index.search_fts("cloud")
        assert len(results) > 0, "Substring 'cloud' should match 'Cloudflare'"

    def test_special_characters(self, index):
        """Special FTS5 operators should be safely escaped."""
        index.upsert(MemoryEntry(
            content="Use AND operator in search queries carefully",
            category="lesson",
            source_agent="claude-code",
            source_session="test-fts-3",
            importance=0.7,
        ))

        # Words that are FTS5 operators should still be searchable
        results = index.search_fts("AND operator")
        # Should not crash; may or may not find results depending on escaping
        assert isinstance(results, list), "Should return list, not crash"

    def test_empty_query(self, index):
        """Empty query should return empty results."""
        results = index.search_fts("")
        assert results == []

    def test_typo_tolerant_fallback(self, index):
        """A misspelled query should still surface the entry via trigram fuzzy fallback."""
        index.upsert(MemoryEntry(
            content="Decided to use Cloudflare R2 for object storage",
            category="decision",
            source_agent="claude-code",
            source_session="test-fts-typo",
            importance=0.8,
        ))

        # Exact spelling matches.
        assert len(index.search_fts("cloudflare")) > 0
        # Transposition/deletion typos still match via trigram OR fallback.
        assert len(index.search_fts("clouflare")) > 0, "typo 'clouflare' should match 'Cloudflare'"
        assert len(index.search_fts("cloudflre")) > 0, "typo 'cloudflre' should match 'Cloudflare'"


# ══════════════════════════════════════════════════════════════════════════
#  TEST: CLAUDE CODE SCANNER
# ══════════════════════════════════════════════════════════════════════════

class TestClaudeCodeScanner:
    """Test Claude Code scanner with synthetic data."""

    def test_v2_format_parsing(self, tmp_path):
        """Parse v2 Claude Code JSONL format."""
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(
            json.dumps({"type": "user", "message": {"content": "Hello"}, "timestamp": "2024-01-01T00:00:00"}) + "\n"
            + json.dumps({"type": "assistant", "message": {"content": "Hi there!", "tool_calls": []}, "timestamp": "2024-01-01T00:00:01"}) + "\n"
            + json.dumps({"type": "tool_result", "content": "Build succeeded!", "timestamp": "2024-01-01T00:00:02"}) + "\n"
        )

        messages = _parse_claude_jsonl(session_file)
        assert len(messages) == 3, f"Expected 3 messages, got {len(messages)}"
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert messages[2].role == "tool", "Tool results should be captured"

    def test_v1_format_parsing(self, tmp_path):
        """Parse v1 (older) Claude Code format."""
        session_file = tmp_path / "session-v1.jsonl"
        session_file.write_text(
            json.dumps({"display": "Hello from v1", "timestamp": "2024-01-01", "project": "test", "sessionId": "abc123"}) + "\n"
            + json.dumps({"display": "Response from v1", "pastedContents": [], "timestamp": "2024-01-01"}) + "\n"
        )

        messages = _parse_claude_jsonl(session_file)
        assert len(messages) >= 1, "Should parse v1 format messages"

    def test_format_version_detection_v2(self, tmp_path):
        """Detect v2 format version."""
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(
            json.dumps({"type": "user", "message": {"content": "Hello"}, "sessionId": "abc123", "parentUuid": "xyz"}) + "\n"
        )

        version = detect_claude_format_version(session_file)
        assert version == "v2", f"Expected v2, got {version}"

    def test_format_version_detection_v1(self, tmp_path):
        """Detect v1 format version."""
        session_file = tmp_path / "session-v1.jsonl"
        session_file.write_text(
            json.dumps({"display": "Hello", "pastedContents": [], "project": "test", "sessionId": "abc123"}) + "\n"
        )

        version = detect_claude_format_version(session_file)
        assert version == "v1", f"Expected v1, got {version}"

    def test_format_version_detection_empty(self, tmp_path):
        """Empty file should return 'empty'."""
        session_file = tmp_path / "empty.jsonl"
        session_file.write_text("")

        version = detect_claude_format_version(session_file)
        assert version == "empty", f"Expected 'empty', got {version}"

    def test_format_version_detection_unknown(self, tmp_path):
        """Unknown format should return 'unknown' without crashing."""
        session_file = tmp_path / "unknown.jsonl"
        session_file.write_text("not json at all\nstill not json\n")

        version = detect_claude_format_version(session_file)
        assert version == "unknown", f"Expected 'unknown', got {version}"

    def test_multi_part_content(self, tmp_path):
        """Multi-part content (text + images) should extract text parts."""
        session_file = tmp_path / "multipart.jsonl"
        session_file.write_text(json.dumps({
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "Hello here is a diagram"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo..."}},
                ]
            }
        }) + "\n")

        messages = _parse_claude_jsonl(session_file)
        assert len(messages) == 1
        assert "iVBOR" not in messages[0].content, "Base64 image data should be filtered"
        assert "Hello here is a diagram" in messages[0].content

    def test_truncated_json(self, tmp_path):
        """Truncated JSON lines should be skipped without crashing."""
        session_file = tmp_path / "truncated.jsonl"
        session_file.write_text(
            json.dumps({"type": "user", "message": {"content": "Hello"}}) + "\n"
            + "{\"type\": \"assistant\", \"message\": {\"content\": \"Partial\"...\n"
        )

        messages = _parse_claude_jsonl(session_file)
        assert len(messages) == 1, "Should parse valid lines and skip truncated ones"

    def test_binary_garbage(self, tmp_path):
        """Binary garbage should not crash parser."""
        session_file = tmp_path / "garbage.jsonl"
        session_file.write_bytes(b"\x00\x01\x02\x03\xff\xfe\xfd\xfc")

        messages = _parse_claude_jsonl(session_file)
        assert messages == [], "Should return empty list without crashing"

    def test_unicode_content(self, tmp_path):
        """Unicode characters should be preserved."""
        session_file = tmp_path / "unicode.jsonl"
        session_file.write_text(
            json.dumps({"type": "user", "message": {"content": "Hello 世界 🌍"}}) + "\n"
            + json.dumps({"type": "assistant", "message": {"content": "नमस्ते दुनिया"}}) + "\n"
        )

        messages = _parse_claude_jsonl(session_file)
        assert len(messages) == 2
        assert "世界" in messages[0].content
        assert "नमस्ते" in messages[1].content


# ══════════════════════════════════════════════════════════════════════════
#  TEST: AIDER SCANNER
# ══════════════════════════════════════════════════════════════════════════

class TestAiderScanner:
    """Test Aider scanner with synthetic data."""

    def test_parse_markdown_chat(self):
        """Parse Aider markdown chat format."""
        content = """# Chat History

#### user
Can you help me refactor this code?

#### assistant
Sure! Let me look at the code and suggest improvements.

#### user
What about the database schema?

#### assistant
I'd recommend using PostgreSQL with UUID primary keys.
"""
        messages = _parse_aider_markdown(content)
        assert len(messages) == 4, f"Expected 4 messages, got {len(messages)}"
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert "refactor" in messages[0].content

    def test_empty_content(self):
        """Empty content should return empty list."""
        messages = _parse_aider_markdown("")
        assert messages == []

    def test_no_role_headers(self):
        """Content without role headers should return empty list."""
        messages = _parse_aider_markdown("Just some random text\nWithout any headers\n")
        assert messages == []


# ══════════════════════════════════════════════════════════════════════════
#  TEST: CODEX SCANNER
# ══════════════════════════════════════════════════════════════════════════

class TestCodexScanner:
    """Test Codex scanner with synthetic data."""

    def test_codex_synthetic_data(self, temp_home):
        """Scan synthetic Codex history."""
        # Create mock Codex history
        codex_dir = temp_home / ".codex"
        codex_dir.mkdir(parents=True)
        history_file = codex_dir / "history.jsonl"
        history_file.write_text(
            json.dumps({"session_id": "sess-001", "text": "Deploy to Cloudflare", "ts": "1700000000"}) + "\n"
            + json.dumps({"session_id": "sess-001", "text": "Use PostgreSQL 16", "ts": "1700000001"}) + "\n"
            + json.dumps({"session_id": "sess-002", "text": "Set up Redis cache", "ts": "1700000002"}) + "\n"
        )

        sessions = list(scan_codex(temp_home))
        assert len(sessions) == 2, f"Expected 2 sessions, got {len(sessions)}"
        assert sessions[0].source == "codex"

    def test_codex_noise_filtering(self, temp_home):
        """Codex scanner should filter noise (slash commands, clear, etc.)."""
        codex_dir = temp_home / ".codex"
        codex_dir.mkdir(parents=True)
        history_file = codex_dir / "history.jsonl"
        history_file.write_text(
            json.dumps({"session_id": "sess-001", "text": "/help", "ts": "1700000000"}) + "\n"
            + json.dumps({"session_id": "sess-001", "text": "continue", "ts": "1700000001"}) + "\n"
            + json.dumps({"session_id": "sess-001", "text": "clear", "ts": "1700000002"}) + "\n"
            + json.dumps({"session_id": "sess-001", "text": "[Image #1]", "ts": "1700000003"}) + "\n"
            + json.dumps({"session_id": "sess-001", "text": "Real message", "ts": "1700000004"}) + "\n"
        )

        sessions = list(scan_codex(temp_home))
        # Should have 1 session with 1 real message
        assert len(sessions) == 1, f"Expected 1 session, got {len(sessions)}"
        assert len(sessions[0].messages) == 1, "Should have filtered noise"
        assert sessions[0].messages[0].content == "Real message"


# ══════════════════════════════════════════════════════════════════════════
#  TEST: GEMINI SCANNER
# ══════════════════════════════════════════════════════════════════════════

class TestGeminiScanner:
    """Test Gemini scanner with synthetic data."""

    def test_gemini_synthetic_data(self, temp_home):
        """Scan synthetic Gemini CLI history with conversation grouping."""
        gemini_dir = temp_home / ".gemini" / "antigravity-cli"
        gemini_dir.mkdir(parents=True)
        history_file = gemini_dir / "history.jsonl"
        history_file.write_text(
            json.dumps({"conversationId": "conv-001", "display": "Build a web app", "timestamp": "2024-01-01"}) + "\n"
            + json.dumps({"conversationId": "conv-001", "display": "Use React for frontend", "timestamp": "2024-01-01"}) + "\n"
            + json.dumps({"conversationId": "conv-002", "display": "Deploy to Vercel", "timestamp": "2024-01-01"}) + "\n"
        )

        sessions = list(scan_gemini(temp_home))
        assert len(sessions) >= 1
        # Find the gemini-cli session
        cli_sessions = [s for s in sessions if s.source == "gemini-cli"]
        assert len(cli_sessions) == 2, f"Expected 2 grouped conversations, got {len(cli_sessions)}"

        conv1 = [s for s in cli_sessions if s.session_id == "conv-001"]
        assert len(conv1) == 1
        assert len(conv1[0].messages) == 2

        conv2 = [s for s in cli_sessions if s.session_id == "conv-002"]
        assert len(conv2) == 1
        assert len(conv2[0].messages) == 1


# ══════════════════════════════════════════════════════════════════════════
#  TEST: FAST EXTRACTOR
# ══════════════════════════════════════════════════════════════════════════

class TestFastExtractor:
    """Test rules-based fast extraction."""

    def test_extract_decisions(self, sample_session):
        """Extract decisions from session with decision markers."""
        entries = FastExtractor.extract(sample_session)
        categories = {e.category for e in entries}
        assert "decision" in categories
        assert any("Cloudflare" in e.content for e in entries)

    def test_extract_preferences(self, sample_session):
        """Extract preferences from session."""
        entries = FastExtractor.extract(sample_session)
        assert any("CORS" in e.content for e in entries), "Should catch CORS preference"

    def test_extract_lessons(self, sample_session):
        """Extract lessons from session."""
        entries = FastExtractor.extract(sample_session)
        assert any("Razorpay" in e.content for e in entries), "Should catch Razorpay lesson"

    def test_technical_decision_patterns(self):
        """Test new technical decision patterns."""
        session = Session(
            source="claude-code",
            session_id="test-tech",
            messages=[
                Message(role="assistant", content="I think we should use PostgreSQL for the database layer."),
                Message(role="assistant", content="Let's migrate from MySQL to PostgreSQL."),
                Message(role="assistant", content="We should deploy to Cloudflare Workers."),
                Message(role="assistant", content="The database is hosted on Hetzner CX32."),
            ],
        )
        entries = FastExtractor.extract(session)
        assert len(entries) >= 3, f"Expected at least 3 entries, got {len(entries)}"
        assert any("PostgreSQL" in e.content for e in entries)
        assert any("migrate" in e.content.lower() for e in entries)
        assert any("Hetzner" in e.content for e in entries)

    def test_tool_patterns(self):
        """Test new tool installation patterns."""
        session = Session(
            source="claude-code",
            session_id="test-tools",
            messages=[
                Message(role="assistant", content="I ran: npm install express and it worked."),
                Message(role="assistant", content="Then pip install fastapi for the API layer."),
                Message(role="assistant", content="Finally docker compose up -d to start everything."),
            ],
        )
        entries = FastExtractor.extract(session)
        assert len(entries) >= 3, f"Expected at least 3 entries, got {len(entries)}"

    def test_empty_session(self):
        """Empty session should return 0 entries."""
        session = Session(source="test", session_id="empty")
        entries = FastExtractor.extract(session)
        assert entries == []

    def test_noise_session(self):
        """Session with only noise should return 0 entries."""
        session = Session(
            source="test",
            session_id="noise",
            messages=[
                Message(role="user", content="hello"),
                Message(role="assistant", content="hi there"),
            ],
        )
        entries = FastExtractor.extract(session)
        assert entries == []


# ══════════════════════════════════════════════════════════════════════════
#  TEST: LOW-VALUE SESSION FILTERING
# ══════════════════════════════════════════════════════════════════════════

class TestSessionFiltering:
    """Test low-value session detection and filtering."""

    def test_short_session_is_low_value(self):
        """Session with <3 messages should be low-value."""
        session = Session(
            source="claude-code",
            session_id="short",
            messages=[
                Message(role="user", content="Hello"),
                Message(role="assistant", content="Hi"),
            ],
        )
        is_low, reason = is_low_value_session(session)
        assert is_low
        assert reason == "too_short"

    def test_noise_session_is_low_value(self):
        """Session with all slash commands should be low-value."""
        session = Session(
            source="codex",
            session_id="noise",
            messages=[
                Message(role="user", content="/help"),
                Message(role="user", content="/usage"),
                Message(role="user", content="/model"),
            ],
        )
        is_low, reason = is_low_value_session(session)
        assert is_low
        assert reason == "all_noise"

    def test_meaningful_session_not_low_value(self, sample_session):
        """Session with real content should NOT be low-value."""
        is_low, _ = is_low_value_session(sample_session)
        assert not is_low

    def test_filter_sessions_for_llm(self, sample_session):
        """filter_sessions_for_llm should correctly split sessions."""
        short = Session(source="test", session_id="s1", messages=[
            Message(role="user", content="Hi"),
            Message(role="assistant", content="Hello"),
        ])
        good = sample_session

        good_list, low_list = filter_sessions_for_llm([short, good])
        assert len(good_list) == 1
        assert len(low_list) == 1
        assert low_list[0].session_id == "s1"

    def test_mixed_session_partial_noise(self):
        """Session with some noise but some real content should not be skipped."""
        session = Session(
            source="claude-code",
            session_id="mixed",
            messages=[
                Message(role="user", content="/help"),
                Message(role="user", content="I want to deploy to Cloudflare"),
                Message(role="assistant", content="Sure, let's set that up"),
            ],
        )
        is_low, _ = is_low_value_session(session)
        assert not is_low, "Mixed session should not be low-value"


# ══════════════════════════════════════════════════════════════════════════
#  TEST: SCHEMA VERSIONING & MIGRATION
# ══════════════════════════════════════════════════════════════════════════

class TestSchemaVersioning:
    """Test SQLite schema versioning and migration."""

    def test_new_index_has_schema_version(self, index):
        """New index should have the current schema version."""
        version = index._get_user_version()
        assert version == SCHEMA_VERSION, f"Expected {SCHEMA_VERSION}, got {version}"

    def test_stats_include_schema_info(self, index):
        """Stats should include schema version and hash algorithm."""
        index.upsert(MemoryEntry(content="Test entry", category="fact", source_agent="test", source_session="s1"))
        stats = index.stats()
        assert "schema_version" in stats
        assert "hash_algorithm" in stats
        assert stats["hash_algorithm"] == "sha256_16"

    def test_meta_table_exists(self, index):
        """index_meta table should exist with hash_algorithm key."""
        row = index.conn.execute(
            "SELECT value FROM index_meta WHERE key = 'hash_algorithm'"
        ).fetchone()
        assert row is not None
        assert row[0] == "sha256_16"

    def test_integrity_check_pass(self, index):
        """Integrity check should pass on clean index."""
        issues = index.integrity_check()
        assert issues == [], f"Integrity issues: {issues}"

    def test_integrity_check_content(self, index):
        """Integrity check with entries should pass."""
        index.upsert(MemoryEntry(content="Test entry", category="fact", source_agent="test", source_session="s1"))
        issues = index.integrity_check()
        assert issues == []

    def test_vacuum(self, index):
        """VACUUM should not crash on populated index."""
        for i in range(10):
            index.upsert(MemoryEntry(
                content=f"Test entry {i}",
                category="fact",
                source_agent="test",
                source_session="s1",
            ))
        # Should not raise
        index.vacuum()


# ══════════════════════════════════════════════════════════════════════════
#  TEST: EXPORT / IMPORT
# ══════════════════════════════════════════════════════════════════════════

class TestExportImport:
    """Test index export and import."""

    def test_export_creates_tar_gz(self, index, tmp_path):
        """Export should create a valid tar.gz file."""
        # Add some data
        index.upsert(MemoryEntry(
            content="Test export entry",
            category="fact",
            source_agent="test",
            source_session="export-test",
        ))

        export_path = tmp_path / "export.tar.gz"
        size = index.export_to(str(export_path))
        assert export_path.exists()
        assert size > 0

        # Verify it's a valid tar.gz
        import tarfile
        with tarfile.open(str(export_path), "r:gz") as tar:
            names = tar.getnames()
            assert "index.db" in names

    def test_import_roundtrip(self, index, tmp_path):
        """Import should produce a working index."""
        # Create source data
        index.upsert(MemoryEntry(
            content="Roundtrip test entry",
            category="fact",
            source_agent="test",
            source_session="rt-test",
        ))

        # Export
        export_path = tmp_path / "roundtrip.tar.gz"
        index.export_to(str(export_path))

        # Import to new location
        import_path = tmp_path / "imported" / "index.db"
        imported = MemoryIndex.import_from(str(export_path), str(import_path))

        # Verify data
        stats = imported.stats()
        assert stats["total_entries"] >= 1
        imported.close()


# ══════════════════════════════════════════════════════════════════════════
#  TEST: VALIDATION & EDGE CASES
# ══════════════════════════════════════════════════════════════════════════

class TestValidation:
    """Test input validation and edge cases."""

    def test_invalid_category(self, index):
        """Invalid category should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid category"):
            index.upsert(MemoryEntry(
                content="Test",
                category="invalid-category",
                source_agent="test",
                source_session="s1",
            ))

    def test_empty_content(self, index):
        """Empty content should raise ValueError."""
        with pytest.raises(ValueError, match="at least 3 characters"):
            index.upsert(MemoryEntry(
                content="",
                category="fact",
                source_agent="test",
                source_session="s1",
            ))

    def test_short_content(self, index):
        """Content shorter than 3 chars should raise ValueError."""
        with pytest.raises(ValueError, match="at least 3 characters"):
            index.upsert(MemoryEntry(
                content="ab",
                category="fact",
                source_agent="test",
                source_session="s1",
            ))

    def test_importance_out_of_range(self, index):
        """Importance outside 0.0-1.0 should raise ValueError."""
        with pytest.raises(ValueError, match="Importance must be 0.0-1.0"):
            index.upsert(MemoryEntry(
                content="Test entry",
                category="fact",
                source_agent="test",
                source_session="s1",
                importance=1.5,
            ))

    def test_safe_table_name(self):
        """Table name validation should accept safe names."""
        assert _validate_table_name("entries")
        assert _validate_table_name("my_table_123")
        assert not _validate_table_name("entries; DROP TABLE")
        assert not _validate_table_name("")

    def test_llm_output_sanitized(self, index):
        """Malformed LLM facts must be coerced, never crash the scan."""
        from src.extractor import _facts_to_entries

        session = Session(source="codex", session_id="s-llm", messages=[])
        facts = [
            {"content": "Use Postgres 16 on port 5433", "category": "architecture", "importance": 1.7},
            {"content": "Prefer explicit CORS origins", "category": "preference", "importance": "high"},
            {"content": "short", "category": "fact"},          # too short -> dropped
            "not-a-dict",                                       # junk -> skipped
        ]
        entries = _facts_to_entries(facts, session)

        assert len(entries) == 2, "valid facts kept, junk/short dropped"
        # Invalid category coerced to 'fact'; bad/over-range importance clamped.
        for e in entries:
            assert e.category in index.VALID_CATEGORIES
            assert 0.0 <= e.importance <= 1.0
            index.upsert(e)  # must not raise

    def test_get_recent_empty(self, index):
        """get_recent on empty index should return empty list."""
        assert index.get_recent(10) == []

    def test_get_by_category_nonexistent(self, index):
        """get_by_category for non-existent category should return empty list."""
        assert index.get_by_category("decision") == []


# ══════════════════════════════════════════════════════════════════════════
#  TEST: SCANNER REGISTRY
# ══════════════════════════════════════════════════════════════════════════

class TestScannerRegistry:
    """Test scanner registration and discovery."""

    def test_all_scanners_available(self):
        """All expected scanners should be registered."""
        scanners = get_available_scanners()
        expected = {
            "aider", "agent-linux-control", "claude-code", "codex",
            "continue-dev", "cursor", "gemini", "goose", "opencode",
        }
        for s in expected:
            assert s in scanners, f"Scanner {s} not registered"

    def test_scanner_count(self):
        """Should have at least 9 scanners."""
        scanners = get_available_scanners()
        assert len(scanners) >= 9, f"Expected >=9 scanners, got {len(scanners)}"


# ══════════════════════════════════════════════════════════════════════════
#  TEST: MEMORY INDEX HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════

class TestIndexHelpers:
    """Test MemoryIndex helper functions."""

    def test_vector_encoding(self):
        """Vector encoding/decoding roundtrip."""
        from src.indexer import encode_vector, _decode_vector
        vec = [0.1, 0.2, 0.3, 0.4, 0.5]
        encoded = encode_vector(vec)
        decoded = _decode_vector(encoded)
        assert decoded == pytest.approx(vec, abs=1e-6), "Float32 roundtrip should preserve values"

    def test_cosine_similarity_identical(self):
        """Cosine similarity of identical vectors should be 1.0."""
        from src.indexer import _cosine_similarity
        vec = [1.0, 2.0, 3.0]
        assert _cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal(self):
        """Cosine similarity of orthogonal vectors should be 0.0."""
        from src.indexer import _cosine_similarity
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_cosine_similarity_zero_vector(self):
        """Cosine similarity with zero vector should be 0.0."""
        from src.indexer import _cosine_similarity
        assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════
#  TEST: EXTRACTION QUALITY METRICS
# ══════════════════════════════════════════════════════════════════════════

class TestExtractionMetrics:
    """Test extraction quality tracking."""

    def test_metrics_start_empty(self):
        """Metrics should start with zeros."""
        from src.extractor import _metrics
        _metrics.reset()
        q = _metrics.summary()
        assert q["sessions_processed"] == 0
        assert q["total_facts"] == 0

    def test_metrics_after_extraction(self, sample_session):
        """Metrics should be populated after extraction."""
        from src.extractor import _metrics
        _metrics.reset()

        extractor = SmartExtractor()
        extractor.extract_from_session(sample_session)
        q = _metrics.summary()
        assert q["sessions_processed"] >= 1

    def test_get_extraction_metrics_returns_dict(self):
        """get_extraction_metrics should return a dict."""
        q = get_extraction_metrics()
        assert isinstance(q, dict)
        assert "sessions_processed" in q
        assert "total_facts" in q


# ══════════════════════════════════════════════════════════════════════════
#  TEST: PERFORMANCE — BATCH PROCESSING
# ══════════════════════════════════════════════════════════════════════════

class TestPerformance:
    """Test performance characteristics with synthetic load."""

    def test_batch_insert_many(self, index):
        """Insert 50 entries should be fast."""
        entries = []
        for i in range(50):
            entries.append(MemoryEntry(
                content=f"Performance test entry number {i} with some unique content to test",
                category="fact" if i % 2 == 0 else "decision",
                source_agent="perf-test",
                source_session=f"perf-session-{i % 5}",
                importance=0.5 + (i % 5) * 0.1,
            ))

        for e in entries:
            index.upsert(e)

        stats = index.stats()
        assert stats["total_entries"] == 50

    def test_fts_search_after_many_entries(self, index):
        """FTS search should still work after many entries."""
        for i in range(50):
            index.upsert(MemoryEntry(
                content=f"Entry about Cloudflare Workers deployment {i}",
                category="decision",
                source_agent="test",
                source_session="perf-search",
            ))

        results = index.search_fts("Cloudflare", limit=5)
        assert len(results) > 0
        assert len(results) <= 5, "Should respect limit"


# ══════════════════════════════════════════════════════════════════════════
#  TEST: FORMAT VERSION METADATA
# ══════════════════════════════════════════════════════════════════════════

class TestFormatMetadata:
    """Test format version stored in session metadata."""

    def test_format_version_in_metadata(self, tmp_path):
        """Claude Code sessions should have format_version in metadata."""
        from src.scanners.claude_code import scan_claude_code

        # Create a mock project with a v2 session
        projects_dir = tmp_path / ".claude" / "projects" / "testproj"
        projects_dir.mkdir(parents=True)
        session_file = projects_dir / "sess001.jsonl"
        session_file.write_text(
            json.dumps({"type": "user", "message": {"content": "Hello"}}) + "\n"
            + json.dumps({"type": "assistant", "message": {"content": "Hi"}}) + "\n"
        )

        sessions = list(scan_claude_code(tmp_path))
        if sessions:
            assert "format_version" in sessions[0].metadata
            assert sessions[0].metadata["format_version"] in ("v1", "v2", "unknown")


# Run via: python3 -m pytest tests/test_pipeline.py -v


# ══════════════════════════════════════════════════════════════════════════
#  TEST: STRUCTURED DECISIONS (Decide → Remember → Verify)
# ══════════════════════════════════════════════════════════════════════════

class TestStructuredDecisions:
    """The wired-in decision subsystem: schema v3, extraction, and verify loop."""

    def test_schema_v3_tables_present_and_generic(self, index):
        """v3 migration creates the decision tables; framework seeds carry no book refs."""
        tables = {r[0] for r in index.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "structured_decisions" in tables
        assert "frameworks" in tables

        frameworks = index.list_frameworks()
        names = {f["name"] for f in frameworks}
        assert {"tradeoff_matrix", "failure_modes", "end_to_end"}.issubset(names)
        # No book-specific provenance leaked into the seed data.
        blob = " ".join(f"{f['name']} {f['discipline']} {f['description']}" for f in frameworks).lower()
        assert "ddia" not in blob and "kleppmann" not in blob

    def test_upsert_and_get_decision(self, index):
        did = index.upsert_decision(
            "Use Cloudflare R2 over S3 for object storage",
            agent_source="claude-code",
            framework_used="tradeoff_matrix",
            session_id="s-dec",
            confidence=0.8,
        )
        assert did >= 1
        decisions = index.get_decisions()
        assert len(decisions) == 1
        assert decisions[0]["framework_used"] == "tradeoff_matrix"

    def test_extract_structured_decisions_infers_framework(self):
        """Decision-category entries get promoted with an inferred framework."""
        session = Session(source="codex", session_id="s1", messages=[])
        entries = [
            MemoryEntry(content="We will use Postgres over MySQL — a clear trade-off matrix decision",
                        category="decision", source_agent="codex", source_session="s1", importance=0.7),
            MemoryEntry(content="Prefers tabs over spaces", category="preference",
                        source_agent="codex", source_session="s1", importance=0.6),
        ]
        decisions = extract_structured_decisions(session, entries)
        assert len(decisions) == 1, "only decision-category entries are promoted"
        assert decisions[0]["framework_used"] == "tradeoff_matrix"

    def test_verify_outcome_loop(self, index):
        did = index.upsert_decision("Adopt event sourcing", agent_source="hermes")
        assert index.mark_decision_outcome(did, 1, "Worked out well after 3 months")
        verified = index.get_decisions()[0]
        assert verified["outcome_verified"] == 1
        # stats reflects the verified decision
        s = index.stats()
        assert s["total_decisions"] == 1 and s["verified_decisions"] == 1


# ══════════════════════════════════════════════════════════════════════════
#  TEST: SCOPED RECALL (v0.2 — agent + timeframe)
# ══════════════════════════════════════════════════════════════════════════

class TestRecall:
    """Temporal / per-agent recall — the retrieval shape plain FTS could not serve."""

    def test_query_parsing(self):
        from datetime import datetime, timezone
        from src.recall_query import parse_recall_query
        now = datetime(2026, 6, 22, tzinfo=timezone.utc)
        p = parse_recall_query("what did i do with claude code last month 15th to 16th?", now=now)
        assert p["agent"] == "claude-code"
        assert p["since"].startswith("2026-05-15")
        assert p["until"].startswith("2026-05-16")
        assert p["is_recall"] is True
        # A plain topical query is NOT a recall query.
        assert parse_recall_query("show me the postgres decision")["is_recall"] is False

    def test_session_timestamp_uses_conversation_date(self):
        from src.extractor import session_timestamp
        s = Session(source="claude-code", session_id="s", messages=[
            Message(role="user", content="hi", timestamp="2026-05-15T10:00:00Z"),
            Message(role="assistant", content="yo", timestamp="2026-05-16T11:00:00Z"),
        ])
        assert session_timestamp(s).startswith("2026-05-16")

    def test_recall_filters_by_agent_and_date(self, index):
        index.upsert(MemoryEntry(content="Chose Cloudflare R2 for storage", category="decision",
                                 source_agent="claude-code", source_session="a",
                                 importance=0.8, created_at="2026-05-15T12:00:00+00:00"))
        index.upsert(MemoryEntry(content="Set up the codex pipeline", category="project",
                                 source_agent="codex", source_session="b",
                                 importance=0.5, created_at="2026-05-15T12:00:00+00:00"))
        index.upsert(MemoryEntry(content="Old claude note", category="fact",
                                 source_agent="claude-code", source_session="c",
                                 importance=0.5, created_at="2026-01-01T12:00:00+00:00"))
        # Agent + window scopes correctly.
        hits = index.recall(agent="claude-code", since="2026-05-14", until="2026-05-16T23:59:59")
        assert len(hits) == 1 and hits[0].content.startswith("Chose Cloudflare")
        # Agent alone returns both claude entries, newest first.
        assert len(index.recall(agent="claude-code")) == 2
        # Date alone, no agent, spans agents.
        assert len(index.recall(since="2026-05-14", until="2026-05-16")) == 2


# ══════════════════════════════════════════════════════════════════════════
#  TEST: COVERAGE + TRACEABILITY (v0.2.1 — every entry searchable & traceable)
# ══════════════════════════════════════════════════════════════════════════

class TestCoverageAndTrace:
    """No session is invisible; every entry traces back to its origin."""

    def test_document_source_is_ingested(self):
        # A memory-file style session (no user/assistant turns) must still yield entries.
        s = Session(source="claude-code-memory", session_id="m1",
                    project="/proj", metadata={"file_path": "/home/u/.claude/MEMORY.md"},
                    messages=[Message(role="memory", content=(
                        "Postgres 16 runs on port 5433 for the photoselect project.\n"
                        "- Razorpay OAuth breaks on trailing slash in redirect_uri.\n"))])
        entries = FastExtractor.extract(s)
        assert len(entries) >= 1, "document/memory sources must be searchable"
        assert all(e.metadata.get("source_file") for e in entries), "must be traceable"

    def test_conversation_without_patterns_still_searchable(self):
        s = Session(source="goose", session_id="g1", metadata={"file_path": "/x/goose.jsonl"},
                    messages=[Message(role="user", content="The user asked to navigate into the Downloads directory and list files."),
                              Message(role="assistant", content="Summarized the conversation after a context-length error occurred mid-task.")])
        entries = FastExtractor.extract(s)
        assert len(entries) >= 1, "a no-pattern conversation should not vanish"

    def test_traceability_from_varied_metadata_keys(self):
        for key in ("file_path", "brain_dir", "workspace", "db_path"):
            s = Session(source="x", session_id=f"s-{key}", metadata={key: f"/path/{key}"},
                        messages=[Message(role="user", content="We will use Cloudflare R2 for object storage in prod.")])
            entries = FastExtractor.extract(s)
            assert entries and entries[0].metadata.get("source_file") == f"/path/{key}"

    def test_questions_are_not_stored(self):
        s = Session(source="claude-code", session_id="q1",
                    messages=[Message(role="assistant", content="Did you use the invite URL to add the bot to your server?")])
        # That sentence matches no decision pattern anyway; ensure a question that
        # WOULD match a pattern is dropped by the readability guard.
        s2 = Session(source="claude-code", session_id="q2",
                     messages=[Message(role="user", content="Should we use Postgres for this?")])
        contents = [e.content for e in FastExtractor.extract(s2)]
        assert not any(c.rstrip().endswith("?") for c in contents)
