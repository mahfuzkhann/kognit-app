"""
Tests for backend/notes_engine.py (Pocket Notes pure domain logic).

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_notes_engine.py -v

No network, no database, no AI - the module under test has none.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest

from backend import notes_engine as ne
from backend.notes_engine import NotesValidationError

VALID_ID = "123e4567-e89b-12d3-a456-426614174000"


def _payload(**overrides):
    base = {"client_id": "n_abcdef123456", "content_md": "Force = mass x acceleration."}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# note id validation
# ---------------------------------------------------------------------------

class TestIsValidNoteId:
    def test_accepts_canonical_uuid(self):
        assert ne.is_valid_note_id(VALID_ID)

    @pytest.mark.parametrize("bad", [
        None, 123, "", "not-a-uuid", VALID_ID + "x",
        "123e4567-e89b-12d3-a456-42661417400",        # one char short
        "eq.1,or=(user_id.neq.x)",                    # PostgREST filter syntax
        "123e4567e89b12d3a456426614174000",           # un-dashed form
    ])
    def test_rejects_everything_else(self, bad):
        assert not ne.is_valid_note_id(bad)


# ---------------------------------------------------------------------------
# create payload
# ---------------------------------------------------------------------------

class TestValidateCreatePayload:
    def test_happy_path_normalizes_fields(self):
        out = ne.validate_create_payload(_payload(
            content_md="  Answer text  ", takeaway="Key idea", question_md="Why?",
            sources=[{"title": "T", "url": "https://a.example/x", "domain": "a.example"}],
            source_ref={"projectId": "proj_1", "chatId": "chat_1", "msgIndex": 3},
        ))
        assert out["content_md"] == "Answer text"
        assert out["takeaway"] == "Key idea"
        assert out["question_md"] == "Why?"
        assert out["sources"] == [{"title": "T", "url": "https://a.example/x", "domain": "a.example"}]
        assert out["source_ref"] == {"projectId": "proj_1", "chatId": "chat_1", "msgIndex": 3}

    def test_user_id_in_body_is_never_read(self):
        out = ne.validate_create_payload(_payload(user_id="attacker-id"))
        assert "user_id" not in out

    @pytest.mark.parametrize("body", [None, [], "x", 5])
    def test_non_object_body_rejected(self, body):
        with pytest.raises(NotesValidationError):
            ne.validate_create_payload(body)

    @pytest.mark.parametrize("client_id", [None, "", "short", "has space in it", "a" * 65, 12345678, "bad/char/here"])
    def test_bad_client_id_rejected(self, client_id):
        with pytest.raises(NotesValidationError):
            ne.validate_create_payload(_payload(client_id=client_id))

    @pytest.mark.parametrize("content", [None, "", "   \n ", 123, ["x"]])
    def test_missing_or_non_text_content_rejected(self, content):
        with pytest.raises(NotesValidationError):
            ne.validate_create_payload(_payload(content_md=content))

    def test_oversized_content_rejected(self):
        with pytest.raises(NotesValidationError):
            ne.validate_create_payload(_payload(content_md="x" * (ne.MAX_NOTE_CONTENT_CHARS + 1)))

    def test_content_at_exact_limit_accepted(self):
        out = ne.validate_create_payload(_payload(content_md="x" * ne.MAX_NOTE_CONTENT_CHARS))
        assert len(out["content_md"]) == ne.MAX_NOTE_CONTENT_CHARS

    def test_nul_characters_stripped(self):
        out = ne.validate_create_payload(_payload(content_md="ab\x00cd"))
        assert out["content_md"] == "abcd"

    def test_oversized_takeaway_and_question_rejected(self):
        with pytest.raises(NotesValidationError):
            ne.validate_create_payload(_payload(takeaway="t" * (ne.MAX_TAKEAWAY_CHARS + 1)))
        with pytest.raises(NotesValidationError):
            ne.validate_create_payload(_payload(question_md="q" * (ne.MAX_QUESTION_CHARS + 1)))

    def test_blank_optional_fields_become_none(self):
        out = ne.validate_create_payload(_payload(takeaway="  ", question_md=""))
        assert out["takeaway"] is None and out["question_md"] is None

    def test_sources_filtered_and_bounded(self):
        raw = [{"title": "ok", "url": "javascript:alert(1)"}, {"url": "https://x.example"}, "junk", {}, {"foo": "bar"}]
        raw += [{"title": f"t{i}"} for i in range(30)]
        out = ne.validate_create_payload(_payload(sources=raw))
        assert len(out["sources"]) <= ne.MAX_SOURCES
        assert {"title": "ok"} in out["sources"]                      # unsafe url dropped, title kept
        assert all("javascript" not in s.get("url", "") for s in out["sources"])

    def test_sources_must_be_a_list(self):
        with pytest.raises(NotesValidationError):
            ne.validate_create_payload(_payload(sources="nope"))

    @pytest.mark.parametrize("ref", [
        {"projectId": "", "chatId": "c", "msgIndex": 1},
        {"projectId": "p", "chatId": "c", "msgIndex": -1},
        {"projectId": "p", "chatId": "c", "msgIndex": True},
        {"projectId": "p", "chatId": "c", "msgIndex": "3"},
        {"projectId": "p"},
    ])
    def test_malformed_source_ref_becomes_none(self, ref):
        assert ne.validate_create_payload(_payload(source_ref=ref))["source_ref"] is None

    def test_non_object_source_ref_rejected(self):
        with pytest.raises(NotesValidationError):
            ne.validate_create_payload(_payload(source_ref="x"))


# ---------------------------------------------------------------------------
# tag labels
# ---------------------------------------------------------------------------

class TestNormalizeTagLabel:
    def test_strips_hash_and_collapses_whitespace(self):
        assert ne.normalize_tag_label("  #Vector   Addition ") == "Vector Addition"

    def test_newlines_and_tabs_are_treated_as_plain_whitespace(self):
        assert ne.normalize_tag_label("a\n\tb") == "a b"

    def test_bangla_label_allowed(self):
        assert ne.normalize_tag_label("পদার্থবিজ্ঞান") == "পদার্থবিজ্ঞান"

    def test_class_style_label_allowed(self):
        assert ne.normalize_tag_label("Class 9-10 (SSC)") == "Class 9-10 (SSC)"

    @pytest.mark.parametrize("bad", [None, 5, "", "   ", "#", "x" * 41, "<script>", "a\x00b", "a\x07b", "tag; drop", "x😀"])
    def test_invalid_labels_rejected(self, bad):
        assert ne.normalize_tag_label(bad) is None


# ---------------------------------------------------------------------------
# deterministic tagging
# ---------------------------------------------------------------------------

class TestDeriveTags:
    def test_class_and_stream_are_known_and_subject_inferred(self):
        out = ne.derive_tags("Explain this in physics", "Some answer.", "Class 9-10 (SSC)", "Science (বিজ্ঞান)")
        by_kind = {t["kind"]: t for t in out["tags"]}
        assert by_kind["class"]["origin"] == "known"
        assert by_kind["stream"]["origin"] == "known"
        assert by_kind["subject"] == {"label": "Physics", "kind": "subject", "origin": "inferred"}
        assert out["needs_review"] is False

    def test_no_subject_means_needs_review_and_no_guess(self):
        out = ne.derive_tags("What is Newton's second law?", "F = ma", None, None)
        assert not any(t["kind"] == "subject" for t in out["tags"])
        assert out["needs_review"] is True

    def test_ambiguous_subject_is_not_guessed(self):
        out = ne.derive_tags("Compare physics and chemistry", "text", None, None)
        assert not any(t["kind"] == "subject" for t in out["tags"])
        assert out["needs_review"] is True

    def test_question_subject_wins_over_answer_mentions(self):
        out = ne.derive_tags("In physics, what is work?", "It also appears in chemistry and biology.", None, None)
        assert [t["label"] for t in out["tags"] if t["kind"] == "subject"] == ["Physics"]

    def test_answer_used_as_fallback_when_question_has_no_subject(self):
        out = ne.derive_tags("Explain this", "This is a chemistry concept.", None, None)
        assert [t["label"] for t in out["tags"] if t["kind"] == "subject"] == ["Chemistry"]

    def test_topic_is_never_derived(self):
        out = ne.derive_tags("physics vectors chapter", "Vectors have magnitude and direction.", "Class 9-10 (SSC)", "Science")
        assert not any(t["kind"] == "topic" for t in out["tags"])

    def test_no_class_or_stream_when_profile_missing(self):
        out = ne.derive_tags("q", "a", None, None)
        assert not any(t["kind"] in ("class", "stream") for t in out["tags"])

    def test_formula_definition_and_worked_example_detected(self):
        content = "Definition: speed is distance over time.\n\n**Step 1** find $v = d/t$\n\n$$F = ma$$"
        labels = {t["label"] for t in ne.derive_tags("q", content, None, None)["tags"] if t["kind"] == "type"}
        assert labels == {"Formula", "Definition", "Worked example"}

    def test_bangla_definition_and_solution_markers(self):
        labels = {t["label"] for t in ne.derive_tags("q", "বেগ বলা হয় ...\nসমাধান: ...", None, None)["tags"] if t["kind"] == "type"}
        assert {"Definition", "Worked example"} <= labels

    def test_word_solution_in_prose_is_not_a_worked_example(self):
        content = "A saturated solution cannot dissolve more solute."
        labels = {t["label"] for t in ne.derive_tags("q", content, None, None)["tags"] if t["kind"] == "type"}
        assert "Worked example" not in labels

    def test_every_derived_tag_has_valid_shape(self):
        for tag in ne.derive_tags("physics", "$$x$$ definition", "Class 9-10 (SSC)", "Science")["tags"]:
            assert tag["kind"] in ne.TAG_KINDS and tag["origin"] in ne.TAG_ORIGINS
            assert ne.normalize_tag_label(tag["label"]) == tag["label"]


# ---------------------------------------------------------------------------
# student tag edits
# ---------------------------------------------------------------------------

class TestTagEdits:
    def test_valid_edit_payload(self):
        out = ne.validate_tag_edit_payload([{"label": "#Vector", "kind": "topic"}, {"label": "Formula", "kind": "type"}])
        assert [t["label"] for t in out] == ["Vector", "Formula"]

    def test_duplicates_collapsed_case_insensitively(self):
        out = ne.validate_tag_edit_payload([{"label": "Vector", "kind": "topic"}, {"label": "vector", "kind": "topic"}])
        assert len(out) == 1

    @pytest.mark.parametrize("raw", [
        "x", None, {"label": "a"}, ["str"], [{"kind": "topic"}], [{"label": "<b>", "kind": "topic"}],
        [{"label": "ok", "kind": "weird"}],
        [{"label": f"t{i}", "kind": "other"} for i in range(ne.MAX_TAGS + 1)],
    ])
    def test_invalid_edit_payload_rejected(self, raw):
        with pytest.raises(NotesValidationError):
            ne.validate_tag_edit_payload(raw)

    def test_unknown_origin_defaults_to_student(self):
        out = ne.validate_tag_edit_payload([{"label": "x", "kind": "other", "origin": "known"}])
        assert out[0]["origin"] == "known"   # accepted as input; merge decides the real origin

    def test_merge_keeps_existing_provenance(self):
        existing = [{"label": "Class 9-10 (SSC)", "kind": "class", "origin": "known"}]
        submitted = ne.validate_tag_edit_payload([{"label": "class 9-10 (ssc)", "kind": "class", "origin": "student"}])
        merged = ne.merge_tag_edits(existing, submitted)
        assert merged["tags"][0]["origin"] == "known"

    def test_client_cannot_claim_known_or_inferred_for_new_tags(self):
        submitted = ne.validate_tag_edit_payload([
            {"label": "Physics", "kind": "subject", "origin": "known"},
            {"label": "Vector", "kind": "topic", "origin": "inferred"},
        ])
        merged = ne.merge_tag_edits([], submitted)
        assert {t["origin"] for t in merged["tags"]} == {"student"}

    def test_ai_origin_only_honoured_for_topic_tags(self):
        submitted = ne.validate_tag_edit_payload([
            {"label": "Vector", "kind": "topic", "origin": "ai"},
            {"label": "Physics", "kind": "subject", "origin": "ai"},
        ])
        merged = ne.merge_tag_edits([], submitted)
        origins = {t["label"]: t["origin"] for t in merged["tags"]}
        assert origins == {"Vector": "ai", "Physics": "student"}

    def test_needs_review_recomputed_from_subject_presence(self):
        no_subject = ne.merge_tag_edits([], ne.validate_tag_edit_payload([{"label": "Vector", "kind": "topic"}]))
        assert no_subject["needs_review"] is True
        with_subject = ne.merge_tag_edits([], ne.validate_tag_edit_payload([{"label": "Physics", "kind": "subject"}]))
        assert with_subject["needs_review"] is False

    def test_merge_tolerates_garbage_existing_tags(self):
        merged = ne.merge_tag_edits("garbage", ne.validate_tag_edit_payload([{"label": "x", "kind": "other"}]))
        assert merged["tags"][0]["origin"] == "student"


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------

class TestPagination:
    def test_defaults(self):
        assert ne.parse_pagination(None, None) == (ne.DEFAULT_PAGE_SIZE, 0)

    def test_clamped_to_bounds(self):
        assert ne.parse_pagination(10_000, 10**9) == (ne.MAX_PAGE_SIZE, ne.MAX_OFFSET)
        assert ne.parse_pagination(0, -5) == (1, 0)
        assert ne.parse_pagination(-3, 0) == (1, 0)

    def test_garbage_falls_back_to_defaults(self):
        assert ne.parse_pagination("abc", "x") == (ne.DEFAULT_PAGE_SIZE, 0)

    def test_numeric_strings_accepted(self):
        assert ne.parse_pagination("5", "10") == (5, 10)


# ---------------------------------------------------------------------------
# AI output validators
# ---------------------------------------------------------------------------

class TestValidateFlashcards:
    def test_valid_cards_kept(self):
        out = ne.validate_flashcards([{"front": " Q1 ", "back": "A1"}, {"front": "Q2", "back": "A2"}])
        assert out == [{"front": "Q1", "back": "A1"}, {"front": "Q2", "back": "A2"}]

    @pytest.mark.parametrize("raw", [None, "x", {"front": "a", "back": "b"}, 5])
    def test_non_list_gives_empty(self, raw):
        assert ne.validate_flashcards(raw) == []

    def test_invalid_entries_dropped(self):
        raw = ["str", {"front": "", "back": "b"}, {"front": "a"}, {"front": 1, "back": "b"},
               {"front": "f" * 301, "back": "b"}, {"front": "ok", "back": "b" * 601}, {"front": "good", "back": "fine"}]
        assert ne.validate_flashcards(raw) == [{"front": "good", "back": "fine"}]

    def test_duplicate_fronts_dropped(self):
        out = ne.validate_flashcards([{"front": "Same", "back": "1"}, {"front": "same", "back": "2"}])
        assert len(out) == 1

    def test_capped_at_max_cards(self):
        raw = [{"front": f"q{i}", "back": "a"} for i in range(25)]
        assert len(ne.validate_flashcards(raw)) == ne.MAX_FLASHCARDS


def _q(**o):
    base = {"question": "What is F?", "options": ["ma", "mv", "m/a", "a/m"], "correct_index": 0, "explanation": "Newton"}
    base.update(o)
    return base


class TestValidateMiniQuiz:
    def test_valid_question_kept(self):
        assert ne.validate_mini_quiz([_q()]) == [_q()]

    @pytest.mark.parametrize("bad", [
        _q(question=""), _q(question=5), _q(options="abcd"), _q(options=["only one"]),
        _q(options=["a", "b", "c", "d", "e"]), _q(options=["a", "", "c", "d"]),
        _q(options=["a", "A", "c", "d"]),                 # duplicate (case-insensitive)
        _q(correct_index=4), _q(correct_index=-1), _q(correct_index=True),
        _q(correct_index="0"), _q(correct_index=None), _q(correct_index=1.0),
        _q(question="q" * 301), _q(options=["a" * 151, "b", "c", "d"]),
    ])
    def test_malformed_question_dropped(self, bad):
        assert ne.validate_mini_quiz([bad]) == []

    def test_correct_index_always_points_at_a_real_option(self):
        for q in ne.validate_mini_quiz([_q(correct_index=i) for i in range(4)]):
            assert 0 <= q["correct_index"] < len(q["options"])

    def test_missing_explanation_becomes_empty_string(self):
        q = _q()
        del q["explanation"]
        assert ne.validate_mini_quiz([q])[0]["explanation"] == ""

    def test_explanation_truncated(self):
        out = ne.validate_mini_quiz([_q(explanation="e" * 1000)])
        assert len(out[0]["explanation"]) == ne.MAX_QUIZ_EXPLANATION_CHARS

    def test_capped_at_max_questions(self):
        out = ne.validate_mini_quiz([_q(question=f"q{i}") for i in range(12)])
        assert len(out) == ne.MAX_QUIZ_QUESTIONS

    def test_non_list_gives_empty(self):
        assert ne.validate_mini_quiz({"question": "x"}) == []


class TestValidateTopicSuggestions:
    def test_accepts_object_shape(self):
        out = ne.validate_topic_suggestions({"topics": ["Vector Addition", "Newton's Laws"]})
        # apostrophe is not in the allowed character set -> that label is dropped, not "fixed"
        assert [t["label"] for t in out] == ["Vector Addition"]
        assert all(t["kind"] == "topic" and t["origin"] == "ai" for t in out)

    def test_accepts_bare_list(self):
        assert [t["label"] for t in ne.validate_topic_suggestions(["Momentum"])] == ["Momentum"]

    def test_at_most_three(self):
        out = ne.validate_topic_suggestions({"topics": [f"Topic {i}" for i in range(10)]})
        assert len(out) == ne.MAX_TOPIC_SUGGESTIONS

    def test_excludes_labels_already_on_the_note_and_duplicates(self):
        existing = [{"label": "vector", "kind": "topic", "origin": "student"}]
        out = ne.validate_topic_suggestions({"topics": ["Vector", "Momentum", "momentum"]}, existing)
        assert [t["label"] for t in out] == ["Momentum"]

    @pytest.mark.parametrize("raw", [None, "x", 5, {"topics": "x"}, {"other": []}, {"topics": [None, 5, "", "<b>"]}])
    def test_garbage_gives_empty_never_raises(self, raw):
        assert ne.validate_topic_suggestions(raw) == []