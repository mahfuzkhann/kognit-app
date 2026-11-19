"""
AI Engine 2.0 finalization (2026-11-17) - regression tests from the finalization audit.
Mock provider only: no network, no real key.

  1. A missing / blank GEMINI_API_KEY fails with a clear error that names the setting
     (fixed in backend/providers/gemini.py).
  2. stream_ai_response can never end WITHOUT exactly one terminal event, even if the
     attempt loop is exhausted. The behaviour already existed; this pins it.
  3. The per-request stream telemetry line reports the model that was actually used
     (it used to print the legacy MODEL_NAME even when the router chose another model;
     fixed in backend/ai_engine.py).
"""
import logging

import pytest

from backend import ai_engine, usage_collector
from backend.providers import set_provider
from backend.providers.mock import MockProvider, text_attempt

TERMINALS = ("done", "interrupted", "error")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ai_engine, "_sleep", lambda s: None)
    yield
    set_provider(None)
    usage_collector._current.set(None)


class TestMissingKey:
    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_clear_error_names_the_setting_and_never_a_value(self, monkeypatch, value):
        from backend.providers.gemini import GeminiProvider
        if value is None:
            monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        else:
            monkeypatch.setenv("GEMINI_API_KEY", value)
        with pytest.raises(RuntimeError) as err:
            GeminiProvider()
        message = str(err.value)
        assert "GEMINI_API_KEY" in message
        assert ".env" in message

    def test_a_present_key_still_builds_a_client(self):
        from backend.providers.gemini import GeminiProvider
        provider = GeminiProvider(api_key="unit-test-placeholder-not-a-real-key")
        assert provider.client is not None


class TestAlwaysATerminalEvent:
    def test_exhausted_loop_yields_exactly_one_error_terminal(self, monkeypatch):
        # MAX_ATTEMPTS=0 makes the attempt loop run zero times, i.e. forces the
        # normally-unreachable fall-through path.
        monkeypatch.setattr(ai_engine, "MAX_ATTEMPTS", 0)
        set_provider(MockProvider([text_attempt("never used")]))
        chunks = list(ai_engine.stream_ai_response(
            "What is photosynthesis?", request_id="rid-final-1",
            user_class="Class 9-10 (SSC)", stream="Science"))
        terminals = [c for c in chunks if c.kind in TERMINALS]
        assert len(terminals) == 1
        assert terminals[0].kind == "error"
        assert terminals[0].error_code == ai_engine.ERROR_INTERNAL
        assert terminals[0].text == ai_engine.GENERIC_CHAT_ERROR   # safe student-facing text

    def test_normal_stream_is_unchanged(self):
        set_provider(MockProvider([text_attempt("Hello ", "world")]))
        chunks = list(ai_engine.stream_ai_response(
            "Say hello", request_id="rid-final-2", user_class="Class 9-10 (SSC)", stream="Science"))
        terminals = [c for c in chunks if c.kind in TERMINALS]
        assert [c.kind for c in terminals] == ["done"]
        assert terminals[0].text == "Hello world"


class TestTelemetryReportsTheModelUsed:
    def test_policy_mode_logs_the_routed_model(self, monkeypatch, caplog):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        set_provider(MockProvider([text_attempt("ok")]))
        with caplog.at_level(logging.INFO, logger="kognit.ai_engine"):
            list(ai_engine.stream_ai_response(
                "Who wrote Amar Shonar Bangla?", request_id="rid-final-3", plan="free",
                user_class="Class 9-10 (SSC)", stream="Science"))
        lines = [r.getMessage() for r in caplog.records if "stream_telemetry" in r.getMessage()]
        assert len(lines) == 1
        assert "model=gemini-3.1-flash-lite" in lines[0]
        assert f"model={ai_engine.MODEL_NAME} " not in lines[0] or ai_engine.MODEL_NAME == "gemini-3.1-flash-lite"

    def test_static_mode_still_logs_the_legacy_model(self, caplog):
        set_provider(MockProvider([text_attempt("ok")]))
        with caplog.at_level(logging.INFO, logger="kognit.ai_engine"):
            list(ai_engine.stream_ai_response(
                "Hi", request_id="rid-final-4", user_class="Class 9-10 (SSC)", stream="Science"))
        lines = [r.getMessage() for r in caplog.records if "stream_telemetry" in r.getMessage()]
        assert len(lines) == 1 and f"model={ai_engine.MODEL_NAME} " in lines[0]


# =============================================================================
# KLORIZ MODEL APPROVAL (2026-11-17): exactly two models, gemini-3.1-flash-lite and
# gemini-3.5-flash-lite. gemini-3.6-flash is NOT approved and must never be reachable.
# =============================================================================
import os  # noqa: E402

from backend.ai import config as ai_config, cost, gateway, health, registry, router  # noqa: E402
from backend.providers import ProviderErrorKind, ProviderUsage  # noqa: E402

APPROVED = {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite"}
UNAPPROVED = "gemini-3.6-flash"


class TestOnlyTheTwoApprovedModelsExist:
    def test_the_registry_contains_exactly_the_two_approved_models(self):
        assert {m.model_id for m in registry.all_models()} == APPROVED
        assert len(registry.all_models()) == 2

    @pytest.mark.parametrize("role_var", ["AI_MODEL_GENERAL", "AI_MODEL_ADVANCED"])
    def test_an_environment_override_cannot_introduce_an_unapproved_model(self, monkeypatch, role_var):
        monkeypatch.setenv(role_var, UNAPPROVED)
        assert {m.model_id for m in registry.all_models()} == APPROVED
        assert registry.role_for_model_id(UNAPPROVED) is None

    def test_the_static_default_is_an_approved_model(self):
        assert ai_engine.DEFAULT_MODEL_NAME in APPROVED
        assert ai_engine.MODEL_NAME in APPROVED

    def test_gemini_model_env_rejects_gemini_3_6_flash(self, caplog):
        caplog.set_level(logging.WARNING)
        assert ai_engine._resolve_model_name({"GEMINI_MODEL": UNAPPROVED}) == ai_engine.DEFAULT_MODEL_NAME
        assert any("not an approved Kloriz model" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("model", sorted(APPROVED))
    def test_gemini_model_env_accepts_each_approved_model(self, model):
        assert ai_engine._resolve_model_name({"GEMINI_MODEL": model}) == model

    def test_no_production_file_mentions_the_unapproved_model_as_a_configured_value(self):
        # The model is only ever named in comments / the rejection tests. No runtime constant carries it.
        root = os.path.dirname(os.path.abspath(__file__))
        for name in ("backend/ai/registry.py", "backend/ai/router.py", "backend/ai/cost.py", "backend/ai/gateway.py"):
            text = open(os.path.join(root, name), encoding="utf-8").read()
            assert UNAPPROVED not in text, name


class TestRoutingAndFallbackStayInsideTheApprovedPair:
    @pytest.fixture(autouse=True)
    def _reset_health(self):
        health.tracker.reset()
        yield
        health.tracker.reset()

    @pytest.mark.parametrize("plan", ["free", "pro"])
    @pytest.mark.parametrize("has_image,has_doc", [(False, False), (True, False), (False, True)])
    def test_every_routing_decision_and_alternative_is_approved(self, monkeypatch, plan, has_image, has_doc):
        monkeypatch.setenv("AI_MODEL_GENERAL", UNAPPROVED)
        monkeypatch.setenv("AI_MODEL_ADVANCED", UNAPPROVED)
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        decision = router.route(router.RouteInput(
            operation=router.OP_CHAT, plan=plan, user_class="Class 9-10 (SSC)", stream="Science",
            prompt="Solve 2x + 3 = 7 and explain each step", has_image=has_image, has_document=has_doc,
            request_id="rid-approved"))
        assert decision.model_id in APPROVED
        assert set(decision.eligible_alternatives) <= APPROVED

    @pytest.mark.parametrize("start_role", [registry.ROLE_GENERAL, registry.ROLE_ADVANCED])
    def test_in_provider_fallback_only_ever_switches_to_the_other_approved_model(self, monkeypatch, start_role):
        monkeypatch.setenv("AI_MODEL_GENERAL", UNAPPROVED)
        monkeypatch.setenv("AI_MODEL_ADVANCED", UNAPPROVED)
        monkeypatch.setenv("AI_MODEL_FALLBACK", "on")
        start = registry.get_model(start_role)
        governor = gateway.AttemptGovernor("rid-fb", model_id=start.model_id, role=start_role)
        switched = governor.maybe_switch_model(ProviderErrorKind.RATE_LIMITED.value)
        assert switched in APPROVED and switched != start.model_id

    def test_a_full_request_with_fallback_never_sends_an_unapproved_model_to_the_provider(self, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "policy")
        monkeypatch.setenv("AI_MODEL_FALLBACK", "on")
        monkeypatch.setenv("AI_MODEL_GENERAL", UNAPPROVED)
        mock = MockProvider([
            ai_failing(ProviderErrorKind.RATE_LIMITED), text_attempt("recovered")])
        set_provider(mock)
        chunks = list(ai_engine.stream_ai_response(
            "Who wrote Amar Shonar Bangla?", request_id="rid-fb2", plan="free",
            user_class="Class 9-10 (SSC)", stream="Science"))
        assert chunks[-1].kind == "done"
        assert mock.requests and all(r.model in APPROVED for r in mock.requests)


def ai_failing(kind):
    from backend.providers.mock import failing_attempt
    return failing_attempt(kind, http_status=429)


class TestStaticModeUsesAnApprovedPricedModel:
    def test_static_mode_sends_the_approved_default_to_the_provider(self):
        mock = MockProvider([text_attempt("ok")])
        set_provider(mock)
        list(ai_engine.stream_ai_response("Hi", request_id="rid-st1", user_class="Class 9-10 (SSC)", stream="Science"))
        assert [r.model for r in mock.requests] == [ai_engine.MODEL_NAME]
        assert mock.requests[0].model in APPROVED

    def test_static_mode_usage_row_has_a_real_estimated_cost(self):
        set_provider(MockProvider([text_attempt("ok", usage=ProviderUsage(
            prompt_tokens=1000, output_tokens=500, thoughts_tokens=100, total_tokens=1600))]))
        collector = usage_collector.start_collection("rid-st2", "")
        list(ai_engine.stream_ai_response("Hi", request_id="rid-st2", user_class="Class 9-10 (SSC)", stream="Science"))
        row = collector.snapshot()[0]
        assert row["model"] == ai_engine.MODEL_NAME
        assert row["estimated_cost_usd"] is not None and row["estimated_cost_usd"] > 0

    @pytest.mark.parametrize("model_id,expected_per_million", [
        ("gemini-3.1-flash-lite", 0.25 + 1.50),   # Google official Standard paid tier, checked 2026-11-17
        ("gemini-3.5-flash-lite", 0.30 + 2.50),
    ])
    def test_each_approved_model_is_priced_at_the_officially_confirmed_rate(self, model_id, expected_per_million):
        # 1M input tokens + 1M output tokens (output INCLUDES thinking tokens on Google's price list).
        est = cost.estimate(model_id, ProviderUsage(prompt_tokens=1_000_000, output_tokens=1_000_000,
                                                    thoughts_tokens=0, total_tokens=2_000_000))
        assert est.usd == pytest.approx(expected_per_million)

    def test_thinking_tokens_are_billed_at_the_output_rate(self):
        est = cost.estimate("gemini-3.5-flash-lite", ProviderUsage(
            prompt_tokens=0, output_tokens=1_000_000, thoughts_tokens=1_000_000, total_tokens=2_000_000))
        assert est.usd == pytest.approx(2 * 2.50)

    def test_the_unapproved_model_is_still_unpriced(self):
        est = cost.estimate(UNAPPROVED, ProviderUsage(prompt_tokens=10, output_tokens=10, total_tokens=20))
        assert est.usd is None and est.basis == "unavailable:unknown_model"


class TestLegacyReasoningLevelFollowsTheRegistry:
    def test_3_5_flash_lite_gets_the_documented_low_level(self, monkeypatch):
        monkeypatch.setattr(ai_engine, "MODEL_NAME", "gemini-3.5-flash-lite")
        assert ai_engine._legacy_reasoning("low") == "low"
        assert ai_engine._legacy_reasoning(None) is None

    def test_3_1_flash_lite_gets_no_undocumented_level(self, monkeypatch):
        monkeypatch.setattr(ai_engine, "MODEL_NAME", "gemini-3.1-flash-lite")
        assert ai_engine._legacy_reasoning("low") is None

    def test_static_chat_request_carries_the_level_only_when_documented(self, monkeypatch):
        for model, expected in (("gemini-3.5-flash-lite", "low"), ("gemini-3.1-flash-lite", None)):
            monkeypatch.setattr(ai_engine, "MODEL_NAME", model)
            mock = MockProvider([text_attempt("ok")])
            set_provider(mock)
            list(ai_engine.stream_ai_response("Hi", request_id="rid-lv", user_class="Class 9-10 (SSC)", stream="Science"))
            assert mock.requests[0].model == model
            assert mock.requests[0].reasoning_effort == expected


# =============================================================================
# FINAL LAUNCH CONFIGURATION (2026-11-17): a deployment with NO AI_* variables set uses
# policy routing and the one approved-model fallback. conftest.py pins the legacy values
# for the older single-model tests, so these tests delete them to see the real defaults.
# =============================================================================
import io  # noqa: E402

from PIL import Image  # noqa: E402

from backend.providers.mock import failing_attempt  # noqa: E402

GENERAL_ID, ADVANCED_ID = "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def bare_deployment(monkeypatch):
    for name in ("AI_ROUTER_MODE", "AI_MODEL_FALLBACK", "AI_CONTEXT_BUILDER", "AI_VERIFY_MODE", "AI_GATEWAY",
                 "AI_MODEL_GENERAL", "AI_MODEL_ADVANCED"):
        monkeypatch.delenv(name, raising=False)
    health.tracker.reset()
    yield
    health.tracker.reset()


class TestProductionDefaults:
    def test_a_bare_deployment_is_policy_with_fallback_and_nothing_experimental(self, bare_deployment):
        assert ai_config.router_mode() == "policy"
        assert ai_config.model_fallback_enabled() is True
        assert ai_config.verify_mode() == "off"            # no extra paid call per answer
        assert ai_config.context_builder_mode() == "legacy"  # v2 prompts are not validated on real answers
        assert ai_config.gateway_enabled() is True

    def test_a_plain_free_question_is_routed_to_gemini_3_1_flash_lite(self, bare_deployment):
        mock = MockProvider([text_attempt("ok", usage=ProviderUsage(prompt_tokens=100, output_tokens=50, total_tokens=150))])
        set_provider(mock)
        chunks = list(ai_engine.stream_ai_response(
            "Who wrote Amar Shonar Bangla?", request_id="rid-d1", plan="free",
            user_class="Class 9-10 (SSC)", stream="Science"))
        assert chunks[-1].kind == "done" and chunks[-1].model == GENERAL_ID
        assert [r.model for r in mock.requests] == [GENERAL_ID]

    def test_an_image_question_is_routed_to_gemini_3_5_flash_lite(self, bare_deployment):
        mock = MockProvider([text_attempt("ok")])
        set_provider(mock)
        chunks = list(ai_engine.stream_ai_response(
            "What does this diagram show?", request_id="rid-d2", plan="free", image_bytes=_png(),
            user_class="Class 9-10 (SSC)", stream="Science"))
        assert chunks[-1].kind == "done" and chunks[-1].model == ADVANCED_ID
        assert mock.requests[0].model == ADVANCED_ID

    def test_a_429_recovers_on_the_other_approved_model_and_the_done_event_names_it(self, bare_deployment):
        mock = MockProvider([failing_attempt(ProviderErrorKind.RATE_LIMITED, http_status=429), text_attempt("recovered")])
        set_provider(mock)
        collector = usage_collector.start_collection("rid-d3", "")
        chunks = list(ai_engine.stream_ai_response(
            "Who wrote Amar Shonar Bangla?", request_id="rid-d3", plan="free",
            user_class="Class 9-10 (SSC)", stream="Science"))
        assert [c.kind for c in chunks if c.kind in TERMINALS] == ["done"]
        assert [r.model for r in mock.requests] == [GENERAL_ID, ADVANCED_ID]
        assert chunks[-1].model == ADVANCED_ID and chunks[-1].text == "recovered"
        assert all(r.model in APPROVED for r in mock.requests)
        assert collector.snapshot()[-1]["fallback_from"] == GENERAL_ID

    def test_429_on_both_approved_models_stops_after_exactly_two_attempts(self, bare_deployment):
        mock = MockProvider([failing_attempt(ProviderErrorKind.RATE_LIMITED, http_status=429)])
        set_provider(mock)
        chunks = list(ai_engine.stream_ai_response(
            "Who wrote Amar Shonar Bangla?", request_id="rid-d4", plan="free",
            user_class="Class 9-10 (SSC)", stream="Science"))
        assert [c.kind for c in chunks if c.kind in TERMINALS] == ["error"]
        assert mock.call_count == 2

    def test_rollback_to_the_single_model_path_is_one_environment_variable(self, bare_deployment, monkeypatch):
        monkeypatch.setenv("AI_ROUTER_MODE", "static")
        monkeypatch.setenv("AI_MODEL_FALLBACK", "off")
        mock = MockProvider([text_attempt("ok")])
        set_provider(mock)
        chunks = list(ai_engine.stream_ai_response(
            "Who wrote Amar Shonar Bangla?", request_id="rid-d5", plan="free",
            user_class="Class 9-10 (SSC)", stream="Science"))
        assert chunks[-1].model == ai_engine.MODEL_NAME and mock.requests[0].model == ai_engine.MODEL_NAME

    def test_the_done_event_names_the_model_in_static_mode_too(self):
        set_provider(MockProvider([text_attempt("ok")]))
        chunks = list(ai_engine.stream_ai_response("Hi", request_id="rid-d6", user_class="Class 9-10 (SSC)", stream="Science"))
        assert chunks[-1].kind == "done" and chunks[-1].model == ai_engine.MODEL_NAME

    def test_an_interrupted_or_failed_stream_does_not_claim_a_model(self, bare_deployment):
        set_provider(MockProvider([failing_attempt(ProviderErrorKind.AUTH, http_status=401)]))
        chunks = list(ai_engine.stream_ai_response("Hi", request_id="rid-d7", plan="free", user_class="Class 9-10 (SSC)", stream="Science"))
        assert chunks[-1].kind == "error" and chunks[-1].model is None