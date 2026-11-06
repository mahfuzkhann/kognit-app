"""
Deterministic, explainable model router (policy version below).

No machine learning, no model call, no randomness: the same input always
produces the same RouteDecision, so routing is reproducible in tests and every
decision can be explained from the stored reason code.

CEO rules (D-2, D-4) encoded here, in priority order:
  1. A request that carries an IMAGE or an uploaded DOCUMENT goes to the
     advanced model ("multimodal_or_document").
  2. Demanding academic tasks - Classes 9-12 Mathematics / Higher Mathematics,
     Physics, Chemistry, numerical Commerce, reasoning-heavy ICT - go to the
     advanced model ("advanced_<category>").
  3. Everything else is "general text": Free -> general model.
     Pro -> 70% advanced / 30% general (configurable), chosen by a stable hash
     of the request id so a given request always routes the same way.
  4. Task suitability beats the distribution: rules 1-2 are never diluted.
  5. Health: if the chosen model's circuit breaker is open and the other
     approved model is capable of the task, use it (reason suffix
     "+health_alternative"). If neither is available the originally chosen
     model is still returned (failing fast would be worse than trying).

The task classifier is a TRANSPARENT KEYWORD HEURISTIC, not an academic
authority. Its accuracy is unmeasured (a hypothesis) - routing runs in
"shadow" mode first so its decisions can be compared with outcomes before
anyone relies on them. Class/stream come from the verified server-side
profile; the prompt is only read, never executed.
"""
import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from backend.ai import config, registry
from backend.ai.health import tracker as default_tracker, HealthTracker

POLICY_VERSION = "2026-11-03.r1"

OP_CHAT = "chat"
OP_QUIZ = "quiz"
OP_TITLE = "title"
OP_NOTES = "notes"

CAT_GENERAL = "general"
CAT_MULTIMODAL = "multimodal"
CAT_DOCUMENT = "document"
CAT_MATH = "mathematics"
CAT_PHYSICS = "physics"
CAT_CHEMISTRY = "chemistry"
CAT_COMMERCE_NUMERIC = "commerce_numeric"
CAT_ICT_REASONING = "ict_reasoning"

REASONING_CATEGORIES = (CAT_MATH, CAT_PHYSICS, CAT_CHEMISTRY, CAT_COMMERCE_NUMERIC, CAT_ICT_REASONING)

# --- keyword lists (English + Bangla). Short, reviewable, versioned. ---------
_MATH = (
    "equation", "integral", "integrate", "derivative", "differentiate", "calculus",
    "logarithm", "trigonometr", "quadratic", "polynomial", "matrix", "determinant",
    "sin ", "cos ", "tan ", "limit", "vector", "sequence", "series", "probability",
    "permutation", "combination", "geometry", "theorem", "prove that", "simplify",
    "factorise", "factorize", "solve", "inequality", "complex number", "conic",
    "parabola", "ellipse", "hyperbola", "binomial",
    "সমীকরণ", "যোগজ", "অন্তরীকরণ", "অন্তরজ", "ত্রিকোণমিতি", "বহুপদী", "ম্যাট্রিক্স",
    "নির্ণায়ক", "সমাধান", "প্রমাণ কর", "সরল কর", "উৎপাদক", "জ্যামিতি", "ভেক্টর",
    "সম্ভাবনা", "ধারা", "লগারিদম", "বৃত্ত", "অনুপাত",
)
_PHYSICS = (
    "velocity", "acceleration", "newton", "momentum", "kinetic energy", "potential energy",
    "projectile", "circuit", "resistance", "capacitor", "magnetic", "electric field",
    "wavelength", "refraction", "lens", "thermodynamic", "gravitation", "torque", "work done",
    "বেগ", "ত্বরণ", "নিউটন", "ভরবেগ", "গতিশক্তি", "স্থিতিশক্তি", "প্রাসের", "বর্তনী",
    "রোধ", "তড়িৎ", "চৌম্বক", "তরঙ্গ", "প্রতিসরণ", "লেন্স", "মহাকর্ষ", "কার্য",
)
_CHEMISTRY = (
    "mole", "molarity", "stoichiometr", "oxidation", "reduction", "redox", "titration",
    "equilibrium", "electrolysis", "enthalpy", "ph of", "balance the equation", "periodic table",
    "organic", "alkane", "alkene", "mass of", "molecular formula", "valency",
    "মোল", "মোলারিটি", "জারণ", "বিজারণ", "টাইট্রেশন", "সাম্যাবস্থা", "তড়িৎ বিশ্লেষণ",
    "রাসায়নিক বিক্রিয়া", "সমতা কর", "পর্যায় সারণি", "জৈব", "আণবিক সংকেত",
)
_COMMERCE = (
    "balance sheet", "trial balance", "ledger", "journal entry", "depreciation", "profit and loss",
    "break-even", "cost of goods", "ratio analysis", "partnership", "interest on", "net profit",
    "ক্রয়", "জাবেদা", "খতিয়ান", "রেওয়ামিল", "অবচয়", "লাভ-ক্ষতি", "স্থিতিপত্র", "আর্থিক বিবরণী",
    "মুনাফা", "সুদ",
)
_ICT = (
    "algorithm", "flowchart", "binary", "hexadecimal", "boolean", "logic gate", "truth table",
    "number system", "program", "c language", "pseudocode", "database", "sql", "network",
    "অ্যালগরিদম", "ফ্লোচার্ট", "বাইনারি", "হেক্সাডেসিমাল", "বুলিয়ান", "লজিক গেট", "সত্যক সারণি",
    "সংখ্যা পদ্ধতি", "প্রোগ্রাম", "ডেটাবেজ",
)
_ARITHMETIC_RE = re.compile(r"(\d+\s*[-+*/^=×÷]\s*\d+)|(\d+\s*(?:kg|m/s|cm|mol|g|n|j|v|a)\b)", re.IGNORECASE)

_NUMBER_RE = re.compile(r"[0-9\u09E6-\u09EF]+(?:[.,][0-9\u09E6-\u09EF]+)?")
_CLASS_6_8_RE = re.compile(r"\b6\s*[-–]\s*8\b")


def class_band(user_class: Optional[str]) -> Optional[str]:
    """'6-8' | '9-10' | '11-12' | None, from the profile's class label."""
    text = (user_class or "").lower()
    if _CLASS_6_8_RE.search(text) or "class 6" in text or "class 7" in text or "class 8" in text:
        return "6-8"
    if "9-10" in text.replace("–", "-") or "ssc" in text or "class 9" in text or "class 10" in text:
        return "9-10"
    if "11-12" in text.replace("–", "-") or "hsc" in text or "class 11" in text or "class 12" in text:
        return "11-12"
    return None


def _hits(text: str, words: tuple) -> int:
    return sum(1 for w in words if w in text)


def classify_task(prompt: str, user_class: Optional[str], stream: Optional[str]) -> str:
    """Returns one academic CAT_* constant (never multimodal/document - those
    come from the request's attachments). Conservative: when evidence is thin
    the answer is CAT_GENERAL, which is also the cheaper model."""
    band = class_band(user_class)
    if band == "6-8":
        return CAT_GENERAL           # the approved rule covers Classes 9-12
    text = (prompt or "").lower()
    stream_l = (stream or "").lower()
    scores = {
        CAT_MATH: _hits(text, _MATH),
        CAT_PHYSICS: _hits(text, _PHYSICS),
        CAT_CHEMISTRY: _hits(text, _CHEMISTRY),
        CAT_COMMERCE_NUMERIC: _hits(text, _COMMERCE),
        CAT_ICT_REASONING: _hits(text, _ICT),
    }
    arithmetic = bool(_ARITHMETIC_RE.search(text))
    # Two or more numbers (ASCII or Bangla digits) also make a question numerical:
    # "trial balance from these 5000 and 3000 figures" has no operator but is a calculation.
    numeric = arithmetic or len(_NUMBER_RE.findall(text)) >= 2
    if arithmetic:
        scores[CAT_MATH] += 1
    if numeric and ("commerce" in stream_l or "ব্যবসায়" in stream_l):
        scores[CAT_COMMERCE_NUMERIC] += 1
    # Commerce only counts as "reasoning" when numbers are present.
    if scores[CAT_COMMERCE_NUMERIC] and not numeric:
        scores[CAT_COMMERCE_NUMERIC] = 0
    best = max(scores, key=lambda k: (scores[k], k))
    return best if scores[best] >= 1 else CAT_GENERAL


@dataclass(frozen=True)
class RouteInput:
    operation: str = OP_CHAT
    plan: str = "free"
    user_class: Optional[str] = None
    stream: Optional[str] = None
    prompt: str = ""
    has_image: bool = False
    has_document: bool = False
    needs_structured_output: bool = False
    request_id: str = ""


@dataclass(frozen=True)
class RouteDecision:
    role: str
    model_id: str
    provider: str
    task_category: str
    reason: str
    plan: str
    policy_version: str
    thinking_level: Optional[str]
    capability_checks: dict = field(default_factory=dict)
    eligible_alternatives: tuple = ()
    health: str = "closed"
    distribution_bucket: Optional[int] = None
    distribution_deviation: bool = False

    def log_fields(self) -> str:
        return (
            f"route_model={self.model_id} route_role={self.role} task={self.task_category} "
            f"route_reason={self.reason} route_policy={self.policy_version} plan={self.plan} "
            f"health={self.health}"
        )


# Thinking-level policy per (role, task) - HYPOTHESES to be tuned from the
# benchmark, documented as such. The general model gets no level (the docs do
# not list its levels); the advanced model reasons harder on demanding tasks.
_ADVANCED_THINKING = {
    CAT_MATH: "medium", CAT_PHYSICS: "medium", CAT_CHEMISTRY: "medium",
    CAT_COMMERCE_NUMERIC: "medium", CAT_ICT_REASONING: "medium",
    CAT_MULTIMODAL: "low", CAT_DOCUMENT: "low", CAT_GENERAL: "low",
}


def _bucket(request_id: str) -> int:
    digest = hashlib.sha256((request_id or "").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def route(inp: RouteInput, *, health: HealthTracker = None) -> RouteDecision:
    health = health or default_tracker
    plan = "pro" if inp.plan == "pro" else "free"
    bucket = None
    deviation = False

    if inp.operation != OP_CHAT:
        # Quizzes, titles and notes AI are structured/short generations. They
        # use the general model by default; a quiz is the one place a wrong
        # answer key hurts, so quizzes on demanding subjects use the advanced
        # model (the classifier reads the topic text passed as the prompt).
        category = CAT_GENERAL
        if inp.operation == OP_QUIZ:
            category = classify_task(inp.prompt, inp.user_class, inp.stream)
        role = registry.ROLE_ADVANCED if category in REASONING_CATEGORIES else registry.ROLE_GENERAL
        reason = f"operation_{inp.operation}" + (f"_advanced_{category}" if role == registry.ROLE_ADVANCED else "_general")
    elif inp.has_image:
        category, role, reason = CAT_MULTIMODAL, registry.ROLE_ADVANCED, "multimodal_or_document"
    elif inp.has_document:
        category, role, reason = CAT_DOCUMENT, registry.ROLE_ADVANCED, "multimodal_or_document"
    else:
        category = classify_task(inp.prompt, inp.user_class, inp.stream)
        if category in REASONING_CATEGORIES:
            role, reason = registry.ROLE_ADVANCED, f"advanced_{category}"
        elif plan == "pro":
            bucket = _bucket(inp.request_id)
            if bucket < config.pro_advanced_share_percent():
                role, reason = registry.ROLE_ADVANCED, "pro_distribution_advanced"
            else:
                role, reason = registry.ROLE_GENERAL, "pro_distribution_general"
        else:
            role, reason = registry.ROLE_GENERAL, "free_general_text"

    # Capability gate (never route to a model that cannot do the job).
    needed_inputs = tuple(
        name for name, present in (("image", inp.has_image), ("pdf", inp.has_document)) if present
    )
    spec = registry.get_model(role)
    checks = registry.supports(spec, inputs=needed_inputs, structured=inp.needs_structured_output)
    if not all(checks.values()):
        alt = registry.get_model(registry.other_role(role))
        alt_checks = registry.supports(alt, inputs=needed_inputs, structured=inp.needs_structured_output)
        if all(alt_checks.values()):
            spec, checks, role, reason = alt, alt_checks, alt.role, reason + "+capability_alternative"

    # Health gate.
    alternatives = []
    other = registry.get_model(registry.other_role(spec.role))
    other_checks = registry.supports(other, inputs=needed_inputs, structured=inp.needs_structured_output)
    if all(other_checks.values()):
        alternatives.append(other.model_id)
    health_state = health.state(spec.model_id)
    if health_state != "closed" and alternatives and health.allow(other.model_id) and not health.allow(spec.model_id):
        spec, role, reason = other, other.role, reason + "+health_alternative"
        checks = other_checks
        health_state = health.state(spec.model_id)
        alternatives = [registry.get_model(registry.other_role(spec.role)).model_id]

    if plan == "pro" and reason.startswith("pro_distribution") is False and category == CAT_GENERAL:
        deviation = True   # general text that did not follow the 70/30 rule
    thinking = registry.thinking_level_for(spec, _ADVANCED_THINKING.get(category, "low")
                                           if spec.role == registry.ROLE_ADVANCED else None)
    return RouteDecision(
        role=spec.role, model_id=spec.model_id, provider=spec.provider,
        task_category=category, reason=reason, plan=plan, policy_version=POLICY_VERSION,
        thinking_level=thinking, capability_checks=dict(checks),
        eligible_alternatives=tuple(alternatives), health=health_state,
        distribution_bucket=bucket, distribution_deviation=deviation,
    )