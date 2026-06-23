#!/usr/bin/env python3
"""
Standalone CLI for Antharmaya Memory Bridge.

Usage:
  memory-bridge scan              Scan for new agent conversations
  memory-bridge search <query>    Search unified memory
  memory-bridge stats             Show memory bridge statistics
  memory-bridge quality           Show extraction quality metrics
  memory-bridge decisions         List structured decisions consolidated from agents
  memory-bridge recall <question> Recall what you did with an agent in a time window
  memory-bridge brain [entity]    Explore the entity graph (top entities or a map)
  memory-bridge brief <scope>     Always-fresh briefing for a project/entity
  memory-bridge verify <id> good|bad|unset   Record how a decision turned out
  memory-bridge reflect           Surface failed decisions as lessons (non-destructive)
  memory-bridge mcp               Run the MCP server (stdio) for any MCP client
  memory-bridge consolidate       Deep LLM consolidation of all unscanned sessions
  memory-bridge repair            Repair corrupted index
  memory-bridge export <file>     Export index to tar.gz
  memory-bridge import <file>     Import index from tar.gz
  memory-bridge vacuum            Vacuum index to reclaim space
"""

import argparse
import os
import sys
from pathlib import Path

# Add src to path if running from repo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def cmd_scan(args):
    """Scan for new agent conversations and extract facts."""
    from src.scanner import discover_all
    from src.indexer import MemoryIndex
    from src.extractor import FastExtractor, SmartExtractor, extract_structured_decisions
    from src.entities import extract_entities
    from src.config import get_default_db_path

    home = Path(args.home) if args.home else Path.home()
    db_path = get_default_db_path()
    index = MemoryIndex(db_path)

    sessions = discover_all(home)
    print(f"Discovered {len(sessions)} sessions across {len(set(s.source for s in sessions))} sources")
    print()

    # Set up SmartExtractor — always prefers ctx.llm if available
    use_llm = not args.no_llm
    if use_llm:
        extractor = SmartExtractor()
        print("Smart extraction enabled (ctx.llm → Direct → FastExtractor)")
    else:
        extractor = None
        print("Rules-based extraction only")

    if args.verbose:
        # Show per-scanner diagnostics
        from src.scanner import scan_stats
        stats = scan_stats(home)
        print()
        print("Scanner diagnostics:")
        for scanner, data in sorted(stats.items()):
            print(f"  {scanner}: {data['sessions']} sessions, {data['messages']} msgs, {data['projects']} projects")
        print()

    new = 0
    skipped = 0
    total_entries = 0
    batch_size = args.batch_size or 10

    total_sessions = len(sessions)
    num_batches = (total_sessions + batch_size - 1) // batch_size

    for batch_num in range(num_batches):
        start_idx = batch_num * batch_size
        end_idx = min(start_idx + batch_size, total_sessions)
        batch = sessions[start_idx:end_idx]

        print(f"Processing batch {batch_num + 1}/{num_batches} ({start_idx + 1}-{end_idx} of {total_sessions})...")

        for session in batch:
            if index.is_source_processed(session.source, session.session_id):
                skipped += 1
                continue

            # SmartExtractor handles LLM + fast extraction and merging
            if extractor and not args.no_llm:
                try:
                    entries = extractor.extract_from_session(session)
                except Exception as e:
                    print(f"  LLM extraction failed for {session.source}/{session.session_id[:8]}: {e}")
                    entries = FastExtractor.extract(session)
            else:
                entries = FastExtractor.extract(session)

            for entry in entries:
                entry_id = index.upsert(entry)
                try:
                    ents = extract_entities(entry.content, (entry.metadata or {}).get("project"))
                    index.index_entities_for_entry(entry_id, ents, entry.created_at)
                except Exception:
                    pass

            # Promote decisions into the structured_decisions table.
            for d in extract_structured_decisions(session, entries):
                try:
                    index.upsert_decision(
                        d["decision_text"],
                        agent_source=d["agent_source"],
                        framework_used=d["framework_used"],
                        session_id=d["session_id"],
                        confidence=d["confidence"],
                    )
                except Exception:
                    pass

            index.mark_source_processed(session.source, session.session_id, len(session.messages))
            new += 1
            total_entries += len(entries)

    # Optional semantic backfill (no-op if model2vec isn't installed).
    from src import embeddings as _emb
    if _emb.available():
        n = index.backfill_embeddings(_emb.embed)
        if n:
            print(f"Embedded {n} new entries for semantic search.")

    # Close the decision loop: pull Council decisions in, then surface failures as lessons.
    council_n = index.import_council_decisions()
    if council_n:
        print(f"Imported {council_n} Council decisions.")
    insights = index.detect_insights()
    if insights:
        print(f"Reflection surfaced {insights} lesson(s) from failed decisions.")

    stats = index.stats()
    print()
    print(f"Done. {new} new sessions processed, {skipped} skipped.")
    print(f"Total index: {stats['total_entries']} entries, {stats.get('total_decisions', 0)} structured decisions")
    for src, count in sorted(stats.get("by_source", {}).items()):
        print(f"  {src}: {count}")
    print(f"Schema version: {stats.get('schema_version', '?')}")
    print(f"Hash algorithm: {stats.get('hash_algorithm', 'sha256_16')}")

    # Show quality metrics if extraction was used
    if use_llm and extractor:
        try:
            from src.extractor import get_extraction_metrics
            q = get_extraction_metrics()
            print()
            print("Extraction quality this session:")
            print(f"  Facts per message: {q.get('facts_per_message', 0)}")
            print(f"  LLM failures: {q.get('llm_failures', 0)}")
            print(f"  Sessions skipped (too short): {q.get('sessions_skipped_short', 0)}")
            print(f"  Sessions skipped (noise): {q.get('sessions_skipped_noise', 0)}")
        except Exception:
            pass

    index.close()


def cmd_search(args):
    """Search the unified memory index."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()

    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return

    index = MemoryIndex(db_path)
    results = index.search_fts(args.query, limit=args.limit)

    if not results:
        print(f"No results for: {args.query}")
    else:
        print(f"Found {len(results)} results for: {args.query}")
        print()
        for i, r in enumerate(results, 1):
            source_tag = r.source_agent.replace("-", " ").title()
            print(f"{i}. [{r.category.upper()}] ({source_tag}) {r.content}")
            if r.tags:
                print(f"   Tags: {', '.join(r.tags)}")
            if r.metadata.get("confidence"):
                print(f"   Confidence: {r.metadata['confidence']}")
            print()

    index.close()


def cmd_stats(args):
    """Show memory bridge statistics."""
    from src.indexer import MemoryIndex
    from src.scanner import get_available_scanners
    from src.config import get_default_db_path

    db_path = get_default_db_path()

    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return

    index = MemoryIndex(db_path)
    stats = index.stats()

    print("🧠 Antharmaya Memory Bridge")
    print("=" * 40)
    print(f"Total entries: {stats['total_entries']}")
    print(f"Structured decisions: {stats.get('total_decisions', 0)} ({stats.get('verified_decisions', 0)} outcome-verified)")
    print(f"Processed sessions: {stats['processed_sessions']}")
    print(f"Schema version: {stats.get('schema_version', '?')}")
    print(f"Hash algorithm: {stats.get('hash_algorithm', 'sha256_16')}")
    print()

    integrity = index.integrity_check()
    if integrity:
        print(f"⚠️  Integrity issues: {'; '.join(integrity)}")
        print()
    else:
        print("✅ Index integrity: OK")
        print()

    print("By category:")
    for cat, count in sorted(stats.get("by_category", {}).items()):
        print(f"  {cat}: {count}")

    print()
    print("By source:")
    for src, count in sorted(stats.get("by_source", {}).items()):
        print(f"  {src}: {count}")

    print()
    print(f"Available scanners: {', '.join(get_available_scanners())}")

    index.close()


def cmd_decisions(args):
    """List structured decisions consolidated from agent conversations."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()
    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return

    index = MemoryIndex(db_path)
    decisions = index.get_decisions(
        limit=args.limit,
        framework=args.framework,
        unverified_only=args.unverified,
    )
    if not decisions:
        print("No structured decisions found yet. Run 'memory-bridge scan' to consolidate them.")
        index.close()
        return

    print(f"Found {len(decisions)} structured decisions:")
    print()
    verdict = {1: "✅ worked", -1: "❌ failed", 0: "… unverified"}
    for i, d in enumerate(decisions, 1):
        outcome = verdict.get(d.get("outcome_verified", 0), "… unverified")
        fw = d.get("framework_used") or "unknown"
        print(f"{i}. [{fw}] ({d.get('agent_source')}) {d.get('decision_text', '')[:160]}")
        print(f"   confidence: {d.get('confidence')}  |  outcome: {outcome}")
        print()

    index.close()


def cmd_recall(args):
    """Scoped recall: 'what did I do with <agent> on <date>'."""
    from src.indexer import MemoryIndex
    from src.recall_query import parse_recall_query
    from src.config import get_default_db_path

    db_path = get_default_db_path()
    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return

    question = " ".join(args.question)
    parsed = parse_recall_query(question)
    index = MemoryIndex(db_path)
    entries = index.recall(
        agent=parsed["agent"], since=parsed["since"], until=parsed["until"],
        query=None if (parsed["agent"] or parsed["since"]) else question, limit=args.limit,
    )
    decisions = index.recall_decisions(
        agent=parsed["agent"], since=parsed["since"], until=parsed["until"], limit=10,
    )

    scope = []
    if parsed["agent"]:
        scope.append(f"agent={parsed['agent']}")
    if parsed["since"]:
        scope.append(f"{parsed['since'][:10]} → {(parsed['until'] or '')[:10]}")
    print(f"Recall ({', '.join(scope) or 'full text'}): {len(entries)} memories, {len(decisions)} decisions")
    print()
    for e in entries:
        when = (e.created_at or "")[:10]
        src = e.source_agent.replace("-", " ").title()
        print(f"  [{e.category.upper()}] ({src} · {when}) {e.content[:160]}")
    if decisions:
        print()
        print("  Decisions in this window:")
        for d in decisions:
            print(f"    ({d.get('framework_used') or 'unknown'}) {d.get('decision_text', '')[:140]}")
    index.close()


def cmd_quality(args):
    """Show extraction quality metrics."""
    from src.extractor import get_extraction_metrics

    q = get_extraction_metrics()
    if q["sessions_processed"] == 0:
        print("No extraction quality data available. Run 'memory-bridge scan' first.")
        return

    print("📊 Extraction Quality Metrics")
    print("=" * 40)
    print(f"Sessions processed: {q['sessions_processed']}")
    print(f"Total facts: {q['total_facts']}")
    print(f"Total messages: {q['total_messages']}")
    print(f"Facts per message: {q['facts_per_message']}")
    print(f"LLM failures: {q['llm_failures']}")
    print(f"Sessions skipped (too short): {q['sessions_skipped_short']}")
    print(f"Sessions skipped (noise): {q['sessions_skipped_noise']}")
    print()

    if q["category_distribution"]:
        print("Category distribution:")
        for cat, count in sorted(q["category_distribution"].items()):
            print(f"  {cat}: {count}")
        print()

    if q["engine_usage"]:
        print("Engine usage:")
        for engine, count in sorted(q["engine_usage"].items()):
            print(f"  {engine}: {count} sessions")


def cmd_repair(args):
    """Repair a corrupted index."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()

    if not db_path.exists():
        print("No memory index found at", db_path)
        return

    print(f"Repairing index at {db_path}...")
    index = MemoryIndex(db_path, auto_migrate=False)
    if index.repair():
        print("✅ Index repaired successfully.")
        print(f"Backup saved at {db_path}.corrupted")
    else:
        print("❌ Failed to repair index.")
        print("Try deleting the index and re-scanning:")
        print(f"  rm {db_path}")
        print("  memory-bridge scan")


def cmd_export(args):
    """Export the index to a tar.gz file."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()

    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return

    output_path = args.file or "memory-bridge-export.tar.gz"
    index = MemoryIndex(db_path)
    size = index.export_to(output_path)
    index.close()

    print(f"✅ Exported index ({size / 1024:.1f} KB) to {output_path}")
    print(f"Import on another machine: memory-bridge import {output_path}")


def cmd_import(args):
    """Import an index from a tar.gz file."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    import_path = args.file
    if not Path(import_path).exists():
        print(f"File not found: {import_path}")
        return

    db_path = get_default_db_path()

    # Backup existing index if any
    if db_path.exists():
        backup = db_path.with_suffix(".db.pre-import")
        import shutil
        shutil.copy2(str(db_path), str(backup))
        print(f"Backed up current index to {backup}")

    print(f"Importing from {import_path}...")
    index = MemoryIndex.import_from(import_path, db_path)
    stats = index.stats()
    index.close()

    print(f"✅ Imported index with {stats['total_entries']} entries from {stats['processed_sessions']} sessions")


def cmd_vacuum(args):
    """Vacuum the index to reclaim space."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()

    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return

    print("Vacuuming index...")
    index = MemoryIndex(db_path)
    before = db_path.stat().st_size
    index.vacuum()
    after = db_path.stat().st_size
    index.close()

    saved = before - after
    print(f"✅ Vacuum complete. {before / 1024:.1f} KB → {after / 1024:.1f} KB (saved {saved / 1024:.1f} KB)")


def cmd_brain(args):
    """Explore the memory brain — top entities, or one entity's neighborhood."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()
    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return
    index = MemoryIndex(db_path)
    if args.entity:
        nb = index.entity_neighborhood(" ".join(args.entity))
        if not nb:
            print("Entity not found in the brain.")
        else:
            e = nb["entity"]
            print(f"🧠 {e['name']}  ({e['kind']} · {e['mentions']} mentions)")
            print("   connected to:")
            for n in nb["neighbors"]:
                print(f"     ~ {n['name']} ({n['kind']})  · weight {round(n['weight'])}")
    else:
        print("🧠 Top entities in your brain:")
        for e in index.top_entities(limit=25):
            print(f"   [{e['kind']:<8}] {e['name']} — {e['mentions']} mentions")
    index.close()


def cmd_brief(args):
    """Always-fresh briefing for a project/entity (deterministic, traceable)."""
    from src.indexer import MemoryIndex, format_brief
    from src.config import get_default_db_path

    db_path = get_default_db_path()
    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return
    index = MemoryIndex(db_path)
    print(format_brief(index.brief(" ".join(args.scope))))
    index.close()


def cmd_verify(args):
    """Mark how a past decision turned out — the loop's 'verify' step."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()
    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return
    outcome = {"good": 1, "bad": -1, "unset": 0}.get(args.outcome)
    if outcome is None:
        print("outcome must be: good | bad | unset")
        return
    index = MemoryIndex(db_path)
    ok = index.mark_decision_outcome(args.id, outcome, " ".join(args.note) or None)
    if not ok:
        print(f"No decision with id {args.id}. List them: memory-bridge decisions --unverified")
    else:
        verdict = {1: "✓ worked", -1: "✗ failed", 0: "unset"}[outcome]
        print(f"Decision {args.id} marked: {verdict}")
        if outcome == -1:
            n = index.detect_insights()
            print(f"🪞 Surfaced {n} lesson(s) so it won't be repeated.")
    index.close()


def cmd_reflect(args):
    """Surface failed/contradicted decisions as new lesson entries (non-destructive)."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path

    db_path = get_default_db_path()
    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return
    index = MemoryIndex(db_path)
    n = index.detect_insights()
    print(f"🪞 Reflection: surfaced {n} insight(s) from failed decisions as new lessons.")
    index.close()


def cmd_embed(args):
    """Backfill semantic embeddings for entries that lack them (optional feature)."""
    from src.indexer import MemoryIndex
    from src.config import get_default_db_path
    from src import embeddings as _emb

    if not _emb.available():
        print("Semantic embeddings are not installed. Enable with:")
        print('  pip install "memory-bridge[semantic]"   (or: pip install model2vec)')
        return
    db_path = get_default_db_path()
    if not db_path.exists():
        print("No memory index found. Run 'memory-bridge scan' first.")
        return
    index = MemoryIndex(db_path)
    print(f"Embedding model: {_emb.MODEL_NAME}")
    n = index.backfill_embeddings(_emb.embed)
    print(f"✅ Embedded {n} entries. Total vectors: {index.embedding_count()}")
    index.close()


def cmd_mcp(args):
    """Run the MCP server (stdio) so any MCP client can use the unified index."""
    from src.mcp_server import serve
    serve()


def main():
    parser = argparse.ArgumentParser(
        description="Antharmaya Memory Bridge — Unified Agent Memory",
    )
    sub = parser.add_subparsers(dest="command")

    # mcp (framework-agnostic frontend)
    mcp = sub.add_parser("mcp", help="Run the MCP server (stdio) for any MCP client")
    mcp.set_defaults(func=cmd_mcp)

    # scan
    scan = sub.add_parser("scan", help="Scan for new agent conversations")
    scan.add_argument("--no-llm", action="store_true", help="Skip LLM extraction")
    scan.add_argument("--home", type=str, help="Home directory to scan")
    scan.add_argument("--verbose", "-v", action="store_true", help="Show per-scanner diagnostics")
    scan.add_argument("--batch-size", type=int, default=10, help="Session batch size (default: 10)")
    scan.set_defaults(func=cmd_scan)

    # search
    search = sub.add_parser("search", help="Search unified memory")
    search.add_argument("query", type=str, help="Search query")
    search.add_argument("--limit", "-n", type=int, default=10, help="Max results")
    search.set_defaults(func=cmd_search)

    # stats
    stats = sub.add_parser("stats", help="Show memory bridge statistics")
    stats.set_defaults(func=cmd_stats)

    # quality
    quality = sub.add_parser("quality", help="Show extraction quality metrics")
    quality.set_defaults(func=cmd_quality)

    # decisions
    # embed (optional semantic)
    embed = sub.add_parser("embed", help="Backfill semantic embeddings (needs memory-bridge[semantic])")
    embed.set_defaults(func=cmd_embed)

    # brain
    brain = sub.add_parser("brain", help="Explore the entity graph (top entities, or an entity's map)")
    brain.add_argument("entity", nargs="*", help="Entity to map (omit for top entities)")
    brain.set_defaults(func=cmd_brain)

    # brief
    brief = sub.add_parser("brief", help="Always-fresh briefing for a project/entity")
    brief.add_argument("scope", nargs="+", help="Project or entity name (e.g. photoselect)")
    brief.set_defaults(func=cmd_brief)

    # verify
    verify = sub.add_parser("verify", help="Mark how a past decision turned out (good/bad/unset)")
    verify.add_argument("id", type=int, help="Decision id (from `decisions --unverified`)")
    verify.add_argument("outcome", choices=["good", "bad", "unset"], help="How it turned out")
    verify.add_argument("note", nargs="*", help="What happened (optional)")
    verify.set_defaults(func=cmd_verify)

    # reflect
    reflect = sub.add_parser("reflect", help="Surface failed decisions as lessons (non-destructive)")
    reflect.set_defaults(func=cmd_reflect)

    # recall
    recall = sub.add_parser("recall", help="Recall what happened with an agent in a time window")
    recall.add_argument("question", nargs="+", help="e.g. recall what did I do with claude code last month 15th to 16th")
    recall.add_argument("--limit", type=int, default=30, help="Max results (default 30)")
    recall.set_defaults(func=cmd_recall)

    decisions = sub.add_parser("decisions", help="List structured decisions")
    decisions.add_argument("--framework", type=str, default=None, help="Filter by framework name")
    decisions.add_argument("--unverified", action="store_true", help="Only outcome-unverified decisions")
    decisions.add_argument("--limit", type=int, default=20, help="Max results (default 20)")
    decisions.set_defaults(func=cmd_decisions)

    # repair
    repair = sub.add_parser("repair", help="Repair corrupted index")
    repair.set_defaults(func=cmd_repair)

    # export
    export = sub.add_parser("export", help="Export index to tar.gz")
    export.add_argument("file", type=str, nargs="?", help="Output file path")
    export.set_defaults(func=cmd_export)

    # import
    imp = sub.add_parser("import", help="Import index from tar.gz")
    imp.add_argument("file", type=str, help="Input tar.gz file path")
    imp.set_defaults(func=cmd_import)

    # vacuum
    vacuum = sub.add_parser("vacuum", help="Vacuum index to reclaim space")
    vacuum.set_defaults(func=cmd_vacuum)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
