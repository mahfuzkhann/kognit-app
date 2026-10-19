"""
Tests for the individual-class profile (Class 6 ... Class 12) in
backend/main.py's PUT /api/profile validation, plus the frontend/backend
contract that keeps the profile form and the validator in step.

Run with: GEMINI_API_KEY=dummy python3 -m pytest test_profile_individual_classes.py -v

Behaviour under test:
  * every exact class 6-12 can be saved, and the EXACT value reaches storage
    (it is never mapped to a grouped band);
  * Classes 6-8 have no stream (a stream is rejected), Classes 9-12 require
    one of the three valid streams - enforced by the BACKEND, not the form;
  * invalid class / stream combinations are rejected with 422 and nothing is
    stored;
  * the three legacy grouped values remain valid for rows/clients that still
    use them, with their old stream rules unchanged;
  * the form markup in templates/index.html, the constants in static/js/app.js
    and the validator constants cannot drift apart silently;
  * downstream consumers (chat prompt) just receive the exact class string.

get_current_user_and_token is stubbed and upsert_student_profile is mocked at
the backend.main import site, exactly like test_student_profile_endpoint.py.
"""
import os
import re
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

from backend import ai_engine
from backend import main as main_module

ROOT = os.path.abspath(os.path.dirname(__file__))

EXACT_CLASSES = [f"Class {n}" for n in range(6, 13)]
NO_STREAM_EXACT = ["Class 6", "Class 7", "Class 8"]
STREAM_EXACT = ["Class 9", "Class 10", "Class 11", "Class 12"]
STREAMS = ["Science (বিজ্ঞান)", "Commerce (ব্যবসায় শিক্ষা)", "Arts (মানবিক)"]
LEGACY = ["Class 6-8", "Class 9-10 (SSC)", "Class 11-12 (HSC)"]


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def clean_state():
    yield
    main_module.app.dependency_overrides.clear()


def _auth_as(user_id, token="test-token"):
    async def _fake():
        return (user_id, token)
    main_module.app.dependency_overrides[main_module.get_current_user_and_token] = _fake


def _profile_row(user_class, stream, user_id="user-1", name="Mahfuz Khan"):
    return {
        "user_id": user_id, "name": name, "user_class": user_class, "stream": stream,
        "created_at": "2026-10-01T10:00:00+00:00", "updated_at": "2026-10-01T10:00:00+00:00",
    }


def _put(client, user_class, stream="", name="Mahfuz Khan", **extra):
    """PUT the form; returns (response, upsert_mock)."""
    _auth_as("user-1")
    stored_stream = stream or None
    mock = AsyncMock(return_value=_profile_row(user_class, stored_stream))
    with patch.object(main_module, "upsert_student_profile", new=mock):
        data = {"name": name, "user_class": user_class, "stream": stream}
        data.update(extra)
        resp = client.put("/api/profile", data=data)
    return resp, mock


# ---------------------------------------------------------------------------
# 1. Every exact class can be saved, with the exact value stored
# ---------------------------------------------------------------------------

class TestExactClassesCanBeSaved:
    def test_there_are_exactly_seven_exact_classes(self):
        assert sorted(main_module.EXACT_PROFILE_CLASSES, key=lambda c: int(c.split()[1])) == EXACT_CLASSES

    @pytest.mark.parametrize("user_class", NO_STREAM_EXACT)
    def test_class_6_to_8_save_with_no_stream(self, client, user_class):
        resp, mock = _put(client, user_class, stream="")
        assert resp.status_code == 200, resp.text
        kwargs = mock.call_args.kwargs
        assert kwargs["user_class"] == user_class          # exact, not "Class 6-8"
        assert kwargs["stream"] is None
        assert resp.json()["profile"]["user_class"] == user_class

    @pytest.mark.parametrize("stream", STREAMS)
    @pytest.mark.parametrize("user_class", STREAM_EXACT)
    def test_class_9_to_12_save_with_each_valid_stream(self, client, user_class, stream):
        resp, mock = _put(client, user_class, stream=stream)
        assert resp.status_code == 200, resp.text
        kwargs = mock.call_args.kwargs
        assert kwargs["user_class"] == user_class           # exact, not "Class 9-10 (SSC)"
        assert kwargs["stream"] == stream
        assert resp.json()["profile"]["user_class"] == user_class
        assert resp.json()["profile"]["stream"] == stream

    @pytest.mark.parametrize("user_class", EXACT_CLASSES)
    def test_an_exact_class_is_never_rewritten_into_a_grouped_band(self, client, user_class):
        stream = "" if user_class in NO_STREAM_EXACT else STREAMS[0]
        _, mock = _put(client, user_class, stream=stream)
        assert mock.call_args.kwargs["user_class"] not in LEGACY

    def test_name_and_authenticated_identity_are_unchanged_by_the_feature(self, client):
        resp, mock = _put(client, "Class 9", STREAMS[0], user_id="user-ATTACKER", student_id="user-ATTACKER")
        assert resp.status_code == 200
        kwargs = mock.call_args.kwargs
        assert kwargs["user_id"] == "user-1" and kwargs["user_token"] == "test-token"
        assert kwargs["name"] == "Mahfuz Khan"


# ---------------------------------------------------------------------------
# 2. Invalid class / stream combinations are rejected (backend is authoritative)
# ---------------------------------------------------------------------------

class TestInvalidCombinationsRejected:
    @pytest.mark.parametrize("stream", STREAMS)
    @pytest.mark.parametrize("user_class", NO_STREAM_EXACT)
    def test_class_6_to_8_with_any_stream_is_rejected_and_not_stored(self, client, user_class, stream):
        resp, mock = _put(client, user_class, stream=stream)
        assert resp.status_code == 422
        assert resp.json()["detail"] == "This class does not have a stream."
        mock.assert_not_called()

    @pytest.mark.parametrize("user_class", STREAM_EXACT)
    def test_class_9_to_12_without_a_stream_is_rejected_and_not_stored(self, client, user_class):
        resp, mock = _put(client, user_class, stream="")
        assert resp.status_code == 422
        mock.assert_not_called()

    @pytest.mark.parametrize("user_class", STREAM_EXACT)
    def test_class_9_to_12_with_the_stream_field_omitted_is_rejected(self, client, user_class):
        _auth_as("user-1")
        with patch.object(main_module, "upsert_student_profile", new=AsyncMock()) as mock:
            resp = client.put("/api/profile", data={"name": "Mahfuz Khan", "user_class": user_class})
        assert resp.status_code == 422
        mock.assert_not_called()

    @pytest.mark.parametrize("bad_stream", ["Science", "science (বিজ্ঞান)", "Vocational", "None", "null", "  "])
    def test_class_9_to_12_with_an_unknown_stream_is_rejected(self, client, bad_stream):
        resp, mock = _put(client, "Class 10", stream=bad_stream)
        assert resp.status_code == 422
        mock.assert_not_called()

    @pytest.mark.parametrize("bad_class", [
        "Class 5", "Class 13", "Class 0", "Class 06", "class 9", "CLASS 9", " Class 9", "Class 9 ",
        "Class 9-10", "Class 11-12", "Class  9", "9", "Nine", "", "Class 9; DROP TABLE student_profiles",
    ])
    def test_unknown_or_malformed_class_values_are_rejected(self, client, bad_class):
        resp, mock = _put(client, bad_class, stream=STREAMS[0])
        assert resp.status_code == 422
        if bad_class:
            assert resp.json()["detail"] == "Unsupported class value."
        # (an empty value is rejected earlier by FastAPI as a missing required field)
        mock.assert_not_called()

    def test_a_blank_name_is_still_rejected(self, client):
        resp, mock = _put(client, "Class 9", STREAMS[0], name="   ")
        assert resp.status_code == 422
        mock.assert_not_called()

    def test_an_over_long_name_is_still_rejected(self, client):
        resp, mock = _put(client, "Class 9", STREAMS[0], name="ম" * (main_module.MAX_PROFILE_NAME_LENGTH + 1))
        assert resp.status_code == 422
        mock.assert_not_called()

    def test_unauthenticated_save_is_still_401(self, client):
        resp = client.put("/api/profile", data={"name": "X", "user_class": "Class 9", "stream": STREAMS[0]})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 3. Legacy grouped values: still valid, same rules, never converted
# ---------------------------------------------------------------------------

class TestLegacyGroupedValues:
    def test_legacy_set_is_exactly_the_three_old_bands(self):
        assert main_module.LEGACY_PROFILE_CLASSES == set(LEGACY)
        assert main_module.VALID_PROFILE_CLASSES == set(EXACT_CLASSES) | set(LEGACY)

    def test_legacy_class_6_8_still_has_no_stream(self, client):
        ok, mock = _put(client, "Class 6-8", "")
        assert ok.status_code == 200 and mock.call_args.kwargs["user_class"] == "Class 6-8"
        bad, _ = _put(client, "Class 6-8", STREAMS[0])
        assert bad.status_code == 422

    @pytest.mark.parametrize("legacy", ["Class 9-10 (SSC)", "Class 11-12 (HSC)"])
    def test_legacy_bands_still_require_a_stream(self, client, legacy):
        ok, mock = _put(client, legacy, STREAMS[1])
        assert ok.status_code == 200 and mock.call_args.kwargs["user_class"] == legacy
        bad, _ = _put(client, legacy, "")
        assert bad.status_code == 422

    @pytest.mark.parametrize("legacy,stream", [("Class 6-8", None), ("Class 9-10 (SSC)", STREAMS[0]), ("Class 11-12 (HSC)", STREAMS[2])])
    def test_an_existing_legacy_profile_loads_unchanged_and_is_not_rewritten(self, client, legacy, stream):
        _auth_as("user-1")
        row = _profile_row(legacy, stream)
        upsert = AsyncMock()
        with patch.object(main_module, "get_student_profile", new=AsyncMock(return_value=row)), \
             patch.object(main_module, "upsert_student_profile", new=upsert):
            resp = client.get("/api/profile")
        assert resp.status_code == 200
        assert resp.json() == {"profile": row}
        upsert.assert_not_called()      # reading never silently migrates a row


# ---------------------------------------------------------------------------
# 4. Frontend <-> backend contract (the "single source of truth" warning)
# ---------------------------------------------------------------------------

def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _radio_values(html, group_id):
    m = re.search(r'id="%s"[^>]*>(.*?)\n            </div>' % re.escape(group_id), html, re.S)
    assert m, f"{group_id} not found in templates/index.html"
    return re.findall(r'<input type="radio"[^>]*value="([^"]+)"', m.group(1))


class TestFormAndValidatorStayInStep:
    def test_the_class_form_offers_exactly_the_seven_exact_classes_in_order(self):
        assert _radio_values(_read("templates/index.html"), "profile-class-group") == EXACT_CLASSES

    def test_the_form_offers_no_grouped_class_option(self):
        html = _read("templates/index.html")
        block = html[html.index('id="profile-class-group"'): html.index('id="profile-stream-group"')]
        for legacy in LEGACY:
            assert legacy not in block
        assert not re.search(r"Class \d+\s*[-\u2013]\s*\d+", block)

    def test_the_stream_form_values_equal_the_backend_valid_streams(self):
        values = _radio_values(_read("templates/index.html"), "profile-stream-pills")
        assert values == STREAMS
        assert set(values) == main_module.VALID_PROFILE_STREAMS

    def test_every_form_class_is_accepted_by_the_backend_validator(self):
        for value in _radio_values(_read("templates/index.html"), "profile-class-group"):
            assert value in main_module.VALID_PROFILE_CLASSES

    def test_js_exact_class_set_equals_the_form_and_the_backend(self):
        js = _read("static/js/app.js")
        m = re.search(r"const PROFILE_EXACT_CLASSES = new Set\(\[(.*?)\]\);", js, re.S)
        assert m
        assert re.findall(r'"([^"]+)"', m.group(1)) == EXACT_CLASSES
        assert set(EXACT_CLASSES) == main_module.EXACT_PROFILE_CLASSES

    def test_js_stream_classes_are_exactly_the_backend_classes_that_need_a_stream(self):
        js = _read("static/js/app.js")
        m = re.search(r"const PROFILE_STREAM_CLASSES = new Set\(\[(.*?)\]\);", js, re.S)
        assert m
        js_stream_classes = set(re.findall(r'"([^"]+)"', m.group(1)))
        backend_stream_classes = main_module.EXACT_PROFILE_CLASSES - main_module.NO_STREAM_CLASSES
        assert js_stream_classes == backend_stream_classes == set(STREAM_EXACT)

    def test_the_backend_no_stream_set_is_classes_6_to_8_and_the_legacy_band(self):
        assert main_module.NO_STREAM_CLASSES == {"Class 6", "Class 7", "Class 8", "Class 6-8"}


# ---------------------------------------------------------------------------
# 5. Downstream: the exact class is just an opaque string to the AI prompt
# ---------------------------------------------------------------------------

def _instruction(user_class, stream):
    return ai_engine._build_chat_request(
        prompt="hi", mode="direct", board="BD NCTB (Bangla)", user_class=user_class, stream=stream,
        image_bytes=None, pdf_context="", history=None,
    ).system_instruction


class TestChatPromptReceivesTheExactClass:
    @pytest.mark.parametrize("user_class", STREAM_EXACT)
    def test_class_9_to_12_with_stream(self, user_class):
        assert f"studying {user_class} ({STREAMS[0]} stream)" in _instruction(user_class, STREAMS[0])

    @pytest.mark.parametrize("user_class", NO_STREAM_EXACT)
    def test_class_6_to_8_without_stream(self, user_class):
        si = _instruction(user_class, None)
        assert f"studying {user_class}" in si and "stream)" not in si.split("STRICT ACADEMIC")[0]

    def test_a_legacy_row_still_produces_its_old_clause(self):
        assert "studying Class 9-10 (SSC) (Science (বিজ্ঞান) stream)" in _instruction("Class 9-10 (SSC)", STREAMS[0])