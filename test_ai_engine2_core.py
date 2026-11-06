"""
AI Engine 2.0 - unit tests for the policy modules in backend/ai/:
model registry, cost accounting, deterministic router, health tracker /
circuit breaker, retry budget, attempt governor, bounded admission control,
and configuration parsing.

These are pure-Python tests: no network, no provider, no database.
"""
import asyncio

import pytest

from backend.ai import config, cost, gateway, health, registry, router
from backend.providers.base import ProviderUsage


# =============================== registry ====================================
class TestRegistry:
    def test_the_two_approved_models_use_the_ids_from_the_directive(self):
        assert registry.get_model(registry.ROLE_GENERAL).model_id == "gemini-3.1-flash-lite"
        assert registry.get_model(registry.ROLE_ADVANCED).model_id == "gemini-3.5-flash-lite"

    def test_verified_capabilities_are_recorded_with_their_sources(self):
        for spec in registry.all_models():
            assert spec.context_tokens == 1_048_576 and spec.max_output_tokens == 65_536
            assert {"text", "image", "pdf"} <= spec.inputs
            assert spec.streaming and spec.structured_output and spec.search_grounding
            assert spec.verified_on == "2026-11-03"
            assert any("ai.google.dev/gemini-api/docs" in url for url in spec.sources)

    def test_only_documented_thinking_levels_are_ever_sent(self):
        general = registry.get_model(registry.ROLE_GENERAL)
        advanced = registry.get_model(registry.ROLE_ADVANCED)
        # the official thinking table does not list 3.1 Flash-Lite -> send nothing
        assert registry.thinking_level_for(general, "low") is None
        assert registry.thinking_level_for(advanced, "medium") == "medium"
        assert registry.thinking_level_for(advanced, "extreme") is None
        assert registry.thinking_level_for(advanced, None) is None

    def test_an_overridden_model_id_never_inherits_prices_or_levels(self, monkeypatch):
        monkeypatch.setenv("AI_MODEL_ADVANCED", "gemini-9-future-lite")
        spec = registry.get_model(registry.ROLE_ADVANCED)
        assert spec.model_id == "gemini-9-future-lite"
        assert spec.price is None and spec.thinking_levels == () and spec.verified_on == "unverified"
        assert cost.estimate("gemini-9-future-lite", ProviderUsage(prompt_tokens=10, output_tokens=10)).usd is None

    def test_an_invalid_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("AI_MODEL_GENERAL", "bad id; rm -rf /")
        assert registry.get_model(registry.ROLE_GENERAL).model_id == "gemini-3.1-flash-lite"

    def test_unknown_role_is_an_error_not_a_default(self):
        with pytest.raises(KeyError):
            registry.get_model("made_up")

    def test_capability_check_reports_each_requirement(self):
        spec = registry.get_model(registry.ROLE_GENERAL)
        checks = registry.supports(spec, inputs=("image", "pdf", "hologram"), structured=True)
        assert checks["input:image"] and checks["input:pdf"] and not checks["input:hologram"]
        assert checks["structured_output"] and checks["streaming"]


# ================================= cost ======================================
class TestCost:
    def _u(self, p=1000, o=500, t=100, c=0):
        return ProviderUsage(prompt_tokens=p, output_tokens=o, thoughts_tokens=t, cached_tokens=c)

    def test_general_model_cost_matches_hand_calculation(self):
        # (1000*0.25 + (500+100)*1.50) / 1e6
        est = cost.estimate("gemini-3.1-flash-lite", self._u())
        assert est.usd == pytest.approx(0.00115) and est.pricing_version == registry.PRICE_VERSION
        assert est.basis == "estimated_from_provider_usage"

    def test_advanced_model_cost_matches_hand_calculation(self):
        # (1000*0.30 + 600*2.50) / 1e6
        assert cost.estimate("gemini-3.5-flash-lite", self._u()).usd == pytest.approx(0.0018)

    def test_cached_tokens_are_priced_at_the_cached_rate(self):
        # (400*0.25 + 600*0.025 + 600*1.5)/1e6
        est = cost.estimate("gemini-3.1-flash-lite", self._u(c=600))
        assert est.usd == pytest.approx((400 * 0.25 + 600 * 0.025 + 600 * 1.5) / 1e6)

    def test_cached_tokens_can_never_exceed_the_prompt(self):
        est = cost.estimate("gemini-3.1-flash-lite", self._u(p=100, c=999))
        assert est.usd == pytest.approx((0 * 0.25 + 100 * 0.025 + 600 * 1.5) / 1e6)

    @pytest.mark.parametrize("usage,reason", [
        (None, "no_usage_reported"),
        (ProviderUsage(prompt_tokens=10), "incomplete_usage"),
        (ProviderUsage(output_tokens=10), "incomplete_usage"),
    ])
    def test_missing_usage_is_null_never_zero(self, usage, reason):
        est = cost.estimate("gemini-3.1-flash-lite", usage)
        assert est.usd is None and est.basis == f"unavailable:{reason}"

    def test_unknown_model_has_no_cost(self):
        est = cost.estimate("gemini-3.6-flash", self._u())
        assert est.usd is None and est.basis == "unavailable:unknown_model"


# ================================= router ====================================
def R(**kw):
    kw.setdefault("operation", router.OP_CHAT)
    return router.route(router.RouteInput(**kw))


class TestClassifier:
    @pytest.mark.parametrize("prompt,klass,expected", [
        ("What is the capital of Bangladesh?", "Class 9-10 (SSC)", router.CAT_GENERAL),
        ("Solve the quadratic equation x^2 - 5x + 6 = 0", "Class 9-10 (SSC)", router.CAT_MATH),
        ("সমীকরণ সমাধান কর: 2x + 3 = 11", "Class 9-10 (SSC)", router.CAT_MATH),
        ("A car has velocity 20 m/s and acceleration 2 m/s2, find the distance", "Class 11-12 (HSC)", router.CAT_PHYSICS),
        ("বেগ ও ত্বরণ কী?", "Class 11-12 (HSC)", router.CAT_PHYSICS),
        ("Balance the equation and find the mole ratio", "Class 9-10 (SSC)", router.CAT_CHEMISTRY),
        ("Prepare a trial balance from these 5000 and 3000 figures", "Class 11-12 (HSC)", router.CAT_COMMERCE_NUMERIC),
        ("Explain binary to hexadecimal conversion with a truth table", "Class 11-12 (HSC)", router.CAT_ICT_REASONING),
    ])
    def test_classification_of_typical_requests(self, prompt, klass, expected):
        assert router.classify_task(prompt, klass, None) == expected

    def test_classes_6_to_8_are_not_sent_to_the_advanced_math_route(self):
        assert router.classify_task("Solve the equation 2x + 3 = 11", "Class 6-8", None) == router.CAT_GENERAL

    def test_commerce_words_without_numbers_are_general(self):
        assert router.classify_task("What is a ledger?", "Class 11-12 (HSC)", "Commerce") == router.CAT_GENERAL

    def test_class_bands(self):
        assert router.class_band("Class 6-8") == "6-8"
        assert router.class_band("Class 9-10 (SSC)") == "9-10"
        assert router.class_band("Class 11-12 (HSC)") == "11-12"
        assert router.class_band(None) is None and router.class_band("Cambridge O Level") is None


class TestRouting:
    def test_free_general_text_uses_the_general_model(self):
        d = R(plan="free", prompt="Who wrote Amar Shonar Bangla?", user_class="Class 9-10 (SSC)")
        assert d.model_id == "gemini-3.1-flash-lite" and d.reason == "free_general_text"

    def test_images_and_documents_always_use_the_advanced_model(self):
        for kw in ({"has_image": True}, {"has_document": True}):
            for plan in ("free", "pro"):
                d = R(plan=plan, prompt="explain this", **kw)
                assert d.model_id == "gemini-3.5-flash-lite" and d.reason == "multimodal_or_document"
        assert R(has_image=True, prompt="x").task_category == router.CAT_MULTIMODAL
        assert R(has_document=True, prompt="x").task_category == router.CAT_DOCUMENT

    @pytest.mark.parametrize("plan", ["free", "pro"])
    def test_demanding_subjects_use_the_advanced_model_on_every_plan(self, plan):
        d = R(plan=plan, prompt="Solve the integral of x^2 dx", user_class="Class 11-12 (HSC)")
        assert d.model_id == "gemini-3.5-flash-lite" and d.reason == "advanced_mathematics"

    def test_pro_general_text_follows_the_70_30_distribution_deterministically(self):
        picks = [R(plan="pro", prompt="Tell me about Lalon Fakir", request_id=f"req-{i}").role for i in range(2000)]
        advanced = picks.count(registry.ROLE_ADVANCED) / len(picks)
        assert 0.66 <= advanced <= 0.74, advanced          # target 70%
        # stable: the same request id always routes the same way
        assert [R(plan="pro", prompt="x", request_id="req-7").role for _ in range(5)] == [R(plan="pro", prompt="x", request_id="req-7").role] * 5

    def test_distribution_share_is_configurable_and_bounded(self, monkeypatch):
        monkeypatch.setenv("AI_PRO_ADVANCED_SHARE_PERCENT", "0")
        assert all(R(plan="pro", prompt="hello", request_id=str(i)).role == registry.ROLE_GENERAL for i in range(50))
        monkeypatch.setenv("AI_PRO_ADVANCED_SHARE_PERCENT", "100")
        assert all(R(plan="pro", prompt="hello", request_id=str(i)).role == registry.ROLE_ADVANCED for i in range(50))
        monkeypatch.setenv("AI_PRO_ADVANCED_SHARE_PERCENT", "250")      # out of range -> default 70
        assert config.pro_advanced_share_percent() == 70

    def test_task_suitability_beats_the_distribution(self):
        # a 100%-general distribution must never pull a math or image request off the advanced model
        import os
        os.environ["AI_PRO_ADVANCED_SHARE_PERCENT"] = "0"
        try:
            assert R(plan="pro", prompt="Solve x^2 = 4", user_class="Class 9-10 (SSC)").role == registry.ROLE_ADVANCED
            assert R(plan="pro", prompt="hi", has_image=True).role == registry.ROLE_ADVANCED
        finally:
            del os.environ["AI_PRO_ADVANCED_SHARE_PERCENT"]

    def test_unknown_plan_strings_are_treated_as_free(self):
        assert R(plan="enterprise-gold", prompt="hello").reason == "free_general_text"

    def test_quiz_on_a_demanding_subject_uses_the_advanced_model(self):
        d = R(operation=router.OP_QUIZ, prompt="Quadratic equations and their solution", user_class="Class 9-10 (SSC)")
        assert d.role == registry.ROLE_ADVANCED
        assert R(operation=router.OP_QUIZ, prompt="Bengali literature of the 19th century").role == registry.ROLE_GENERAL

    @pytest.mark.parametrize("op", [router.OP_TITLE, router.OP_NOTES])
    def test_titles_and_notes_ai_use_the_general_model(self, op):
        assert R(operation=op, prompt="Solve the equation x = 1").role == registry.ROLE_GENERAL

    def test_decisions_are_reproducible_and_explained(self):
        a = R(plan="pro", prompt="Solve 3x = 9", user_class="Class 9-10 (SSC)", request_id="abc")
        b = R(plan="pro", prompt="Solve 3x = 9", user_class="Class 9-10 (SSC)", request_id="abc")
        assert a == b
        assert a.policy_version == router.POLICY_VERSION and a.provider == "gemini"
        assert "route_reason=" in a.log_fields() and "route_policy=" in a.log_fields()
        assert a.capability_checks["streaming"] is True

    def test_thinking_level_is_only_set_for_the_model_that_documents_levels(self):
        assert R(plan="free", prompt="hello").thinking_level is None                       # 3.1: undocumented
        assert R(plan="free", prompt="Solve x^2 = 4", user_class="Class 9-10 (SSC)").thinking_level == "medium"
        assert R(has_image=True, prompt="x").thinking_level == "low"

    def test_an_open_breaker_diverts_to_the_other_capable_model(self):
        h = health.HealthTracker()
        for _ in range(config.breaker_failure_threshold()):
            h.record_failure("gemini-3.1-flash-lite", "unavailable")
        d = router.route(router.RouteInput(plan="free", prompt="hello"), health=h)
        assert d.model_id == "gemini-3.5-flash-lite" and d.reason.endswith("+health_alternative")

    def test_when_both_breakers_are_open_the_original_choice_is_kept(self):
        h = health.HealthTracker()
        for m in ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite"):
            for _ in range(config.breaker_failure_threshold()):
                h.record_failure(m, "unavailable")
        d = router.route(router.RouteInput(plan="free", prompt="hello"), health=h)
        assert d.model_id == "gemini-3.1-flash-lite" and "health_alternative" not in d.reason


# ================================ health =====================================
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class TestHealthAndBreaker:
    def test_breaker_opens_after_n_consecutive_transient_failures(self):
        clock, h = FakeClock(), None
        h = health.HealthTracker(clock)
        for _ in range(config.breaker_failure_threshold() - 1):
            h.record_failure("m", "unavailable")
        assert h.state("m") == health.STATE_CLOSED and h.allow("m")
        h.record_failure("m", "timeout")
        assert h.state("m") == health.STATE_OPEN and not h.allow("m")

    def test_a_success_resets_the_consecutive_count(self):
        h = health.HealthTracker(FakeClock())
        for _ in range(config.breaker_failure_threshold() - 1):
            h.record_failure("m", "unavailable")
        h.record_success("m", 1.0)
        h.record_failure("m", "unavailable")
        assert h.state("m") == health.STATE_CLOSED

    def test_half_open_lets_exactly_one_probe_through(self):
        clock = FakeClock()
        h = health.HealthTracker(clock)
        for _ in range(config.breaker_failure_threshold()):
            h.record_failure("m", "unavailable")
        assert not h.allow("m")
        clock.advance(config.breaker_cooldown_seconds() + 1)
        assert h.allow("m") is True            # the probe
        assert h.allow("m") is False           # everyone else still blocked
        h.record_success("m", 0.5)
        assert h.state("m") == health.STATE_CLOSED and h.allow("m")

    def test_a_failed_probe_reopens_the_breaker(self):
        clock = FakeClock()
        h = health.HealthTracker(clock)
        for _ in range(config.breaker_failure_threshold()):
            h.record_failure("m", "unavailable")
        clock.advance(config.breaker_cooldown_seconds() + 1)
        assert h.allow("m")
        h.record_failure("m", "unavailable")
        assert h.state("m") == health.STATE_OPEN and not h.allow("m")

    def test_models_are_tracked_independently(self):
        h = health.HealthTracker(FakeClock())
        for _ in range(config.breaker_failure_threshold()):
            h.record_failure("a", "unavailable")
        assert not h.allow("a") and h.allow("b")

    def test_snapshot_reports_rates_latency_and_failure_classes(self):
        h = health.HealthTracker(FakeClock())
        for lat in (0.1, 0.2, 0.3, 0.4):
            h.record_success("m", lat)
        h.record_failure("m", "rate_limited")
        h.record_failure("m", "timeout")
        snap = h.snapshot("m")
        assert snap.window_requests == 6 and snap.window_failures == 2
        assert snap.window_rate_limits == 1 and snap.window_timeouts == 1
        assert snap.error_rate == pytest.approx(2 / 6) and snap.p50_latency_ms in (200, 300)

    def test_old_events_age_out_of_the_window(self):
        clock = FakeClock()
        h = health.HealthTracker(clock)
        h.record_failure("m", "unavailable")
        clock.advance(health.WINDOW_SECONDS + 1)
        assert h.snapshot("m").window_requests == 0


# ============================ failure policy table ===========================
class TestFailurePolicy:
    def test_every_provider_error_kind_has_an_explicit_rule(self):
        from backend.providers.base import ProviderErrorKind
        for kind in ProviderErrorKind:
            assert kind.value.lower() in gateway.FAILURE_POLICY, kind
            # the real enum values are UPPER-case; the lookup must not silently fall back to "unknown"
            assert gateway.rule_for(kind.value).kind == kind.value.lower(), kind

    def test_rows_of_the_table(self):
        P = gateway.FAILURE_POLICY
        assert P["invalid_request"].pre_token == gateway.ACTION_FAIL and not P["invalid_request"].counts_for_health
        assert P["auth"].pre_token == gateway.ACTION_FAIL and not P["auth"].counts_for_health
        assert P["rate_limited"].pre_token == gateway.ACTION_ALT_MODEL        # never a same-model retry
        assert P["unavailable"].pre_token == gateway.ACTION_RETRY and P["unavailable"].mid_stream == gateway.ACTION_RECOVER
        assert P["timeout"].mid_stream == gateway.ACTION_INTERRUPT

    def test_bad_requests_and_auth_never_trip_the_breaker(self):
        assert gateway.health_kind("invalid_request") is None and gateway.health_kind("auth") is None
        assert gateway.health_kind("unavailable") == "unavailable" and gateway.health_kind("unknown") == "transport"


# ============================== retry budget =================================
class TestRetryBudget:
    def test_it_is_a_token_bucket(self):
        clock = FakeClock()
        b = gateway.RetryBudget(burst=3, refill_per_second=1.0, clock=clock)
        assert [b.try_spend() for _ in range(4)] == [True, True, True, False]
        clock.advance(2.0)
        assert [b.try_spend() for _ in range(3)] == [True, True, False]

    def test_refill_never_exceeds_the_burst(self):
        clock = FakeClock()
        b = gateway.RetryBudget(burst=2, refill_per_second=100.0, clock=clock)
        clock.advance(1000)
        assert [b.try_spend() for _ in range(3)] == [True, True, False]

    def test_zero_burst_refuses_every_retry(self):
        assert gateway.RetryBudget(burst=0, refill_per_second=0).try_spend() is False

    def test_an_outage_cannot_multiply_load_beyond_the_budget(self):
        b = gateway.RetryBudget(burst=10, refill_per_second=0.0)
        granted = sum(1 for _ in range(1000) if b.try_spend())       # 1000 failing requests each want a retry
        assert granted == 10


# =============================== governor ====================================
def G(clock=None, **kw):
    kw.setdefault("budget", gateway.RetryBudget(burst=100, refill_per_second=0))
    kw.setdefault("health", health.HealthTracker())
    kw.setdefault("fallback_allowed", False)
    kw.setdefault("first_token_deadline", 60.0)
    kw.setdefault("stream_deadline", 200.0)
    if clock is not None:
        kw["clock"] = clock
    return gateway.AttemptGovernor("req-1", model_id="gemini-3.1-flash-lite", role=registry.ROLE_GENERAL, **kw)


class TestGovernor:
    def test_attempt_timeout_is_capped_by_the_remaining_deadline(self):
        clock = FakeClock()
        g = G(clock)
        assert g.attempt_timeout(30) == 30
        clock.advance(50)
        assert g.attempt_timeout(30) == pytest.approx(10)
        clock.advance(9.8)
        assert g.attempt_timeout(30) is None          # no time left: do not call the provider

    def test_deadline_switches_from_first_token_to_whole_stream_once_text_arrives(self):
        clock = FakeClock()
        g = G(clock)
        clock.advance(61)
        assert g.expired()
        g2 = G(clock)
        g2.mark_first_token()
        clock.advance(61)
        assert not g2.expired()
        clock.advance(200)
        assert g2.expired()

    def test_retries_are_refused_near_the_deadline_and_when_the_budget_is_empty(self):
        clock = FakeClock()
        g = G(clock)
        assert g.allow_retry("x")
        clock.advance(59)
        assert not g.allow_retry("x")
        g2 = G(budget=gateway.RetryBudget(burst=1, refill_per_second=0))
        assert g2.allow_retry("a") and not g2.allow_retry("b") and g2.budget_refusals == 1

    def test_legacy_gateway_flag_disables_deadlines_and_budget(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY", "legacy")
        clock = FakeClock()
        g = G(clock, budget=gateway.RetryBudget(burst=0, refill_per_second=0))
        clock.advance(10_000)
        assert g.attempt_timeout(30) == 30 and g.allow_retry("x") and not g.expired()

    def test_fallback_is_off_by_default_even_when_the_other_model_is_healthy(self):
        g = G(fallback_allowed=False)
        assert g.maybe_switch_model("unavailable") is None

    def test_fallback_switches_once_to_the_other_approved_model_only(self):
        g = G(fallback_allowed=True)
        assert g.maybe_switch_model("unavailable") == "gemini-3.5-flash-lite"
        assert g.role == registry.ROLE_ADVANCED and g.fell_back_from == "gemini-3.1-flash-lite"
        assert g.maybe_switch_model("unavailable") is None                    # never twice in one request

    def test_bad_requests_never_trigger_a_model_switch(self):
        assert G(fallback_allowed=True).maybe_switch_model("invalid_request") is None
        assert G(fallback_allowed=True).maybe_switch_model("auth") is None

    def test_fallback_is_refused_when_the_other_model_is_unhealthy(self):
        h = health.HealthTracker()
        for _ in range(config.breaker_failure_threshold()):
            h.record_failure("gemini-3.5-flash-lite", "unavailable")
        assert G(health=h, fallback_allowed=True).maybe_switch_model("unavailable") is None

    def test_failures_and_successes_feed_the_health_tracker(self):
        h = health.HealthTracker()
        g = G(health=h)
        g.record_failure("unavailable", 0.5)
        g.record_failure("invalid_request", 0.1)      # not model health
        g.record_success(0.3)
        snap = h.snapshot("gemini-3.1-flash-lite")
        assert snap.window_requests == 2 and snap.window_failures == 1


# ============================ admission control ==============================
class TestAdmission:
    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_it_bounds_inflight_and_queue_then_sheds(self, monkeypatch):
        monkeypatch.setenv("AI_MAX_INFLIGHT_GENERATIONS", "2")
        monkeypatch.setenv("AI_MAX_QUEUED_GENERATIONS", "1")
        monkeypatch.setenv("AI_QUEUE_WAIT_SECONDS", "0.3")

        async def scenario():
            a = gateway.AdmissionController()
            await a.acquire(); await a.acquire()
            assert a.inflight == 2
            waiter = asyncio.ensure_future(a.acquire())          # takes the single queue slot
            await asyncio.sleep(0.05)
            assert a.queued == 1
            with pytest.raises(gateway.Overloaded):
                await a.acquire()                                  # queue full -> shed immediately
            a.release()                                            # frees a slot for the waiter
            await waiter
            assert a.inflight == 2 and a.queued == 0 and a.shed == 1
            a.release(); a.release()
            assert a.inflight == 0

        self._run(scenario())

    def test_a_queued_request_is_shed_after_the_wait_limit(self, monkeypatch):
        monkeypatch.setenv("AI_MAX_INFLIGHT_GENERATIONS", "1")
        monkeypatch.setenv("AI_MAX_QUEUED_GENERATIONS", "5")
        monkeypatch.setenv("AI_QUEUE_WAIT_SECONDS", "0.1")

        async def scenario():
            a = gateway.AdmissionController()
            await a.acquire()
            with pytest.raises(gateway.Overloaded):
                await a.acquire()
            assert a.queued == 0 and a.shed == 1

        self._run(scenario())


# ================================= config ====================================
class TestConfig:
    def test_defaults_reproduce_pre_2_0_behaviour(self):
        assert config.router_mode() == "static" and config.context_builder_mode() == "legacy"
        assert config.verify_mode() == "off" and not config.model_fallback_enabled()
        assert config.gateway_enabled() and config.plan_gating_mode() == "enforce"

    @pytest.mark.parametrize("env,value,getter,expected", [
        ("AI_ROUTER_MODE", "policy", config.router_mode, "policy"),
        ("AI_ROUTER_MODE", "chaos", config.router_mode, "static"),
        ("AI_VERIFY_MODE", "on", config.verify_mode, "off"),          # there is no student-facing mode
        ("AI_VERIFY_MODE", "shadow", config.verify_mode, "shadow"),
        ("AI_CONTEXT_BUILDER", "v2", config.context_builder_mode, "v2"),
        ("PLAN_GATING", "off", config.plan_gating_mode, "off"),
        ("AI_FIRST_TOKEN_DEADLINE_SECONDS", "abc", config.first_token_deadline_seconds, 75.0),
        ("AI_FIRST_TOKEN_DEADLINE_SECONDS", "0.1", config.first_token_deadline_seconds, 75.0),
        ("AI_MAX_INFLIGHT_GENERATIONS", "-3", config.max_inflight_generations, 32),
    ])
    def test_values_are_validated_with_safe_fallbacks(self, monkeypatch, env, value, getter, expected):
        monkeypatch.setenv(env, value)
        assert getter() == expected