"""
Kognit Phase 7C - "Kognit Research Benchmark - Core v1" seed script.

============================================================================
HONEST SCOPE DISCLOSURE (same discipline as Phase 7B's seed_core_v1.py)
============================================================================

This seeds 20 items, NOT the 40-60 target named in the approved
architecture. Same reasoning as Phase 7B: no independent human reviewer
was available in this session, and several of these items concern
genuinely live facts (current exchange rates, current office-holders,
current exam schedules) that I cannot verify against a real source
without live search access - which this environment does not have
(no usable GEMINI_API_KEY, and this sandbox's network has no route to
Google's API domains regardless).

Every item is created with author="claude (AI-authored, Phase 7C,
pending human review)", content_status='draft',
verification_status='unverified', partition='dev'. NONE are
holdout-eligible. For items with a non-'timeless' freshness_requirement,
`verified_as_of_date` is deliberately left unset (the schema/authoring
layer will refuse to mark them 'verified_correct' without it - see
evaluation/research_authoring.py's temporal verification rule) - a real
human must confirm the current fact and record the date, this script
does not and cannot do that.

Coverage achieved (real, not padded): all 10 Step-11 categories
(A through J), a mix of research-required (14) and research-not-required
(6) items specifically so the benchmark can measure BOTH missed research
and unnecessary research, per Step 10.1's explicit requirement.
"""

from __future__ import annotations

from evaluation import research_authoring as ra

AUTHOR = "claude (AI-authored, Phase 7C, pending human review)"
DATASET_NAME = "Kognit Research Benchmark - Core v1"


def _draft(**kwargs) -> ra.ResearchItemDraft:
    kwargs.setdefault("source_type", "original")
    kwargs.setdefault("author", AUTHOR)
    return ra.ResearchItemDraft(**kwargs)


def build_research_core_v1_drafts() -> list:
    drafts = []

    # A. Current factual questions
    drafts.append(_draft(
        category="current_factual", expected_research_decision="required", freshness_requirement="changes_yearly",
        question_text="What is the current population of Bangladesh?", language="en", difficulty="easy",
        citation_expectations={"requires_citation": True, "min_sources": 1},
    ))
    drafts.append(_draft(
        category="current_factual", expected_research_decision="required", freshness_requirement="changes_daily",
        question_text="What is the current price of gold per ounce in USD?", language="en", difficulty="easy",
        citation_expectations={"requires_citation": True, "min_sources": 1},
    ))

    # B. Recent developments
    drafts.append(_draft(
        category="recent_development", expected_research_decision="required", freshness_requirement="changes_frequently",
        question_text="What are the latest developments in AI reasoning models in 2026?", language="en", difficulty="medium",
        citation_expectations={"requires_citation": True, "min_sources": 2},
    ))
    drafts.append(_draft(
        category="recent_development", expected_research_decision="required", freshness_requirement="changes_yearly",
        question_text="What were the key announcements in Bangladesh's most recent national budget?",
        language="bn", difficulty="medium", citation_expectations={"requires_citation": True, "min_sources": 1},
    ))

    # C. Current Bangladesh information
    drafts.append(_draft(
        category="current_bangladesh_info", expected_research_decision="required", freshness_requirement="changes_frequently",
        question_text="Who is the current Finance Minister of Bangladesh?", language="en", difficulty="easy",
        citation_expectations={"requires_citation": True, "min_sources": 1},
    ))
    drafts.append(_draft(
        category="current_bangladesh_info", expected_research_decision="required", freshness_requirement="changes_daily",
        question_text="বর্তমানে ১ মার্কিন ডলার সমান কত বাংলাদেশি টাকা?", language="bn", difficulty="easy",
        citation_expectations={"requires_citation": True, "min_sources": 1},
    ))

    # D. Current education information
    drafts.append(_draft(
        category="current_education_info", expected_research_decision="required", freshness_requirement="changes_yearly",
        question_text="What is this year's HSC examination routine/schedule?", language="en", difficulty="medium",
        citation_expectations={"requires_citation": True, "min_sources": 1},
    ))
    drafts.append(_draft(
        category="current_education_info", expected_research_decision="not_required", freshness_requirement="stable_long_term",
        question_text="What topics are typically covered in the NCTB Class 10 Physics syllabus (Force & Motion, Optics, etc.)?",
        language="en", difficulty="easy",
    ))

    # E. Current technology
    drafts.append(_draft(
        category="current_technology", expected_research_decision="required", freshness_requirement="changes_frequently",
        question_text="What is the latest flagship smartphone released by Samsung?", language="en", difficulty="easy",
        citation_expectations={"requires_citation": True, "min_sources": 1},
    ))
    drafts.append(_draft(
        category="current_technology", expected_research_decision="not_required", freshness_requirement="timeless",
        question_text="What is Moore's Law?", language="en", difficulty="easy",
    ))

    # F. Current science
    drafts.append(_draft(
        category="current_science", expected_research_decision="required", freshness_requirement="changes_yearly",
        question_text="Who won the most recent Nobel Prize in Physics?", language="en", difficulty="medium",
        citation_expectations={"requires_citation": True, "min_sources": 1},
    ))
    drafts.append(_draft(
        category="current_science", expected_research_decision="not_required", freshness_requirement="timeless",
        question_text="State Newton's second law of motion.", language="en", difficulty="easy",
    ))

    # G. Current public information
    drafts.append(_draft(
        category="current_public_info", expected_research_decision="required", freshness_requirement="changes_frequently",
        question_text="What is the current speed limit for private cars on the Dhaka-Chittagong highway?",
        language="en", difficulty="medium", citation_expectations={"requires_citation": True, "min_sources": 1},
    ))
    drafts.append(_draft(
        category="current_public_info", expected_research_decision="not_required", freshness_requirement="timeless",
        question_text="At what temperature does water boil at sea level (in Celsius)?", language="en", difficulty="easy",
    ))

    # H. Stable factual questions where search is unnecessary
    drafts.append(_draft(
        category="stable_factual_no_search_needed", expected_research_decision="not_required", freshness_requirement="timeless",
        question_text="What is the capital of Bangladesh?", language="en", difficulty="easy",
    ))
    drafts.append(_draft(
        category="stable_factual_no_search_needed", expected_research_decision="not_required", freshness_requirement="timeless",
        question_text="State the Pythagorean theorem.", language="en", difficulty="easy",
    ))

    # I. Ambiguous freshness questions
    drafts.append(_draft(
        category="ambiguous_freshness", expected_research_decision="required", freshness_requirement="changes_yearly",
        question_text="How many public universities are there in Bangladesh?", language="en", difficulty="medium",
        citation_expectations={"requires_citation": True, "min_sources": 1},
        evaluation_rubric_text="Genuinely ambiguous: the count changes slowly (new universities are "
                                "established occasionally) but a training-knowledge answer may still be "
                                "approximately correct. Included specifically to observe how the "
                                "decisioning heuristic and the model itself handle a borderline case, "
                                "not to harshly penalize either choice.",
    ))
    drafts.append(_draft(
        category="ambiguous_freshness", expected_research_decision="required", freshness_requirement="changes_yearly",
        question_text="What is Bangladesh's current adult literacy rate?", language="en", difficulty="medium",
        citation_expectations={"requires_citation": True, "min_sources": 1},
        evaluation_rubric_text="Changes slowly year over year - a genuinely borderline case for the "
                                "research-decision heuristic, included deliberately (see Step 11).",
    ))

    # J. Questions where search results can be misleading
    drafts.append(_draft(
        category="potentially_misleading_search", expected_research_decision="not_required", freshness_requirement="timeless",
        question_text="Is moderate coffee consumption bad for health?", language="en", difficulty="medium",
        evaluation_rubric_text="A web search can surface sensationalized/contradictory health articles. "
                                "The academically sound answer rests on established, stable scientific "
                                "consensus (moderate caffeine intake is generally considered safe for most "
                                "healthy adults), which does not require live search and could be actively "
                                "harmed by it if search results skew toward clickbait framing.",
    ))
    drafts.append(_draft(
        category="potentially_misleading_search", expected_research_decision="not_required", freshness_requirement="timeless",
        question_text="Do vaccines cause autism?", language="en", difficulty="medium",
        evaluation_rubric_text="A well-established scientific consensus question (no causal link, per "
                                "extensive research) where a live web search risks surfacing misinformation "
                                "rather than improving the answer. The correct behavior is a confident, "
                                "stable, non-searched answer citing scientific consensus, not a search.",
    ))

    return drafts


def seed_research_database(conn) -> str:
    item_version_ids = []
    for draft in build_research_core_v1_drafts():
        _, item_version_id = ra.create_research_item_with_first_version(conn, draft)
        item_version_ids.append(item_version_id)

    dataset_id = ra.create_research_dataset(conn, DATASET_NAME)
    dataset_version_id = ra.cut_research_dataset_version(conn, dataset_id, item_version_ids)
    return dataset_version_id


def main() -> None:
    from pathlib import Path
    from evaluation import db as evaldb

    db_path = Path(__file__).parent / "data" / "research_v1.sqlite"
    if db_path.exists():
        db_path.unlink()

    conn = evaldb.get_connection(db_path)
    evaldb.initialize_schema(conn, schema_path=str(Path(__file__).parent / "schema" / "research_schema.sql"), schema_version=evaldb.RESEARCH_SCHEMA_VERSION)
    dataset_version_id = seed_research_database(conn)

    item_count = conn.execute(
        "SELECT COUNT(*) AS c FROM research_dataset_version_items WHERE dataset_version_id = ?",
        (dataset_version_id,),
    ).fetchone()["c"]
    conn.close()

    print(f"Seeded {item_count} research items into {db_path}")
    print(f"dataset_version_id = {dataset_version_id}")
    print("All items: content_status='draft', verification_status='unverified', partition='dev'.")
    print("None are holdout-eligible. Human review (including verified_as_of_date for non-timeless items) required.")


if __name__ == "__main__":
    main()
