"""Mock Provider —— 让整条认知流水线在**零外部 API** 下完整运行（任务书阶段 3）。

两条使用方式：

1. **默认规则引擎**（无参构造）。它按任务名从输入数据块中推导出**结构合法**的
   输出。它不懂物理也不懂哲学——它保证的是"流水线能跑完、结构能通过校验"，
   这正是阶段 3 验收里"不使用外部 API 也能完整运行"的含义。
2. **脚本化响应**（``responses`` 参数）。场景测试为内容敏感的任务
   （判断合成、回答渲染等）提供固定输出，让断言能落在真实内容上。

🔴 **Mock 解析的是渲染后的提示词文本**，与真实模型看到的完全一致——
它没有任何旁路字段。若哪天 Mock 能拿到真实模型拿不到的东西，
"阶段 3 全绿"就会变成一句没有意义的保证（risks.md R13）。

🔴 **Mock 绝不绕过 Schema 校验。** 它返回的原始字典走的是与真实 Provider
完全相同的 ``model_validate`` 路径；场景 I 正是靠这一点验证
"非法结构不得产生部分写入"。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_psi.domain.exceptions import StructuredOutputError
from ai_psi.prompts.payload import extract_payload
from ai_psi.providers.base import InvocationContext, LLMMessage, ModelConfig
from ai_psi.providers.response import ProviderResponse, TokenUsage

__all__ = ["MockFault", "MockProvider", "MockResponder"]

#: 一条脚本化响应：结构化任务用字典，文本任务用字符串。
MockResponse = dict[str, Any] | str


@runtime_checkable
class MockResponder(Protocol):
    """自定义响应函数。

    签名刻意与"模型看到的东西"对齐：只知道任务名、输入数据、第几次尝试。
    """

    def __call__(
        self,
        *,
        task_name: str,
        payload: dict[str, Any],
        attempt: int,
        is_text_task: bool,
    ) -> MockResponse:
        """构造一次响应。"""
        ...


class MockFault(BaseModel):
    """注入到 Mock 响应中的故障（场景 I）。

    故障是**按任务**注入的，且只影响前 ``times`` 次尝试：
    这让我们能验证"重试后成功"与"重试耗尽后失败"两条路径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_name: str = Field(min_length=1)
    mode: Literal[
        "drop_field",
        "invalid_enum",
        "forbidden_extra_field",
        "not_an_object",
        "empty_text",
    ]
    field: str | None = Field(
        default=None,
        description="点分路径，如 ``judgment.confidence_band``；``drop_field`` 与 "
        "``invalid_enum`` 需要它",
    )
    times: int = Field(default=1, ge=1, description="只在前 N 次尝试中生效")

    def model_post_init(self, __context: Any) -> None:
        """校验字段要求。"""
        if self.mode in {"drop_field", "invalid_enum"} and not self.field:
            msg = f"故障模式 {self.mode!r} 必须指定 field"
            raise ValueError(msg)
        if self.mode == "not_an_object" and self.field:
            msg = "故障模式 'not_an_object' 不接受 field"
            raise ValueError(msg)


class MockProvider:
    """确定性的 Mock LLM Provider。"""

    def __init__(
        self,
        *,
        model: str = "mock-model-v1",
        responses: Mapping[str, Sequence[MockResponse]] | None = None,
        responder: MockResponder | None = None,
        faults: Sequence[MockFault] = (),
        name: str = "mock",
    ) -> None:
        """初始化。

        Args:
            model: 模型标识，写入调用记录。
            responses: 任务名到响应序列的映射。按调用次数依次取用，
                **取完最后一个之后一直重复它**——这正是"模型连续返回同一观点"
                这类场景需要的行为。
            responder: 自定义响应函数；未提供时使用内置规则引擎。
            faults: 注入的故障。
            name: Provider 名称。
        """
        self._model = model
        self._name = name
        self._responses: dict[str, Sequence[MockResponse]] = dict(responses or {})
        self._responder = responder
        self._faults = tuple(faults)
        self._counters: dict[str, int] = {}

    # ------------------------------------------------------------------
    # LLMProvider 协议
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Provider 名称。"""
        return self._name

    @property
    def model(self) -> str:
        """模型标识。"""
        return self._model

    @property
    def call_counts(self) -> dict[str, int]:
        """各任务的调用次数（供测试断言预算消耗）。"""
        return dict(self._counters)

    def reset_counters(self) -> None:
        """清零调用计数（供测试在多个回合之间重置）。"""
        self._counters.clear()

    async def generate_structured[T: BaseModel](
        self,
        *,
        task_name: str,
        messages: list[LLMMessage],
        response_model: type[T],
        model_config: ModelConfig,
        invocation_context: InvocationContext,
    ) -> ProviderResponse[T]:
        """返回经 Schema 校验的结构化对象。

        ⚠️ token 用量是**估算值**（按字符数除以 4）。
        真实 Provider 必须报告真实用量——把估算和实测混在一起
        会让成本核算失去意义，因此 :meth:`TokenUsage.estimated` 只在这里用。

        Raises:
            StructuredOutputError: 输入块缺失、响应不是对象，或校验失败。
        """
        del model_config
        payload = extract_payload(list(messages))
        attempt = self._bump(task_name)
        raw = self._raw_response(task_name=task_name, payload=payload, attempt=attempt)

        if isinstance(raw, str):
            msg = f"任务 {task_name!r} 期望结构化响应，Mock 却配置了字符串"
            raise StructuredOutputError(msg, task_name=task_name, provider=self._name)

        raw = self._apply_faults(task_name=task_name, attempt=attempt, raw=raw)

        if not isinstance(raw, dict):
            msg = f"任务 {task_name!r} 的 Mock 响应不是 JSON 对象"
            raise StructuredOutputError(msg, task_name=task_name, provider=self._name)

        try:
            value = response_model.model_validate(raw)
        except ValidationError as exc:
            # 🔴 只记录**字段路径与原因**，绝不记录模型原始输出——
            # 原始输出可能包含用户正文或注入内容（§17.1）。
            details = tuple(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            msg = f"任务 {task_name!r} 的 Mock 响应不符合 {response_model.__name__} Schema"
            raise StructuredOutputError(
                msg,
                validation_errors=details,
                task_name=task_name,
                provider=self._name,
            ) from exc

        usage = TokenUsage.estimated(
            text=value.model_dump_json(),
            prompt_chars=sum(len(message.content) for message in messages),
        )
        return ProviderResponse(
            value=value,
            usage=usage,
            finish_reason="stop",
            raw_hash=_hash(value.model_dump_json()),
        )

    async def generate_text(
        self,
        *,
        task_name: str,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        invocation_context: InvocationContext,
    ) -> ProviderResponse[str]:
        """返回纯文本响应。

        Raises:
            StructuredOutputError: 输入块缺失，或响应为空。
        """
        del model_config
        payload = extract_payload(list(messages))
        attempt = self._bump(task_name)
        raw = self._raw_response(task_name=task_name, payload=payload, attempt=attempt)
        raw = self._apply_faults(task_name=task_name, attempt=attempt, raw=raw)

        if isinstance(raw, str):
            text = raw
        else:
            msg = f"任务 {task_name!r} 期望文本响应，Mock 却配置了结构化对象"
            raise StructuredOutputError(msg, task_name=task_name, provider=self._name)

        if not text.strip():
            msg = f"任务 {task_name!r} 的 Mock 文本响应为空"
            raise StructuredOutputError(msg, task_name=task_name, provider=self._name)
        return ProviderResponse(
            value=text,
            usage=TokenUsage.estimated(
                text=text,
                prompt_chars=sum(len(message.content) for message in messages),
            ),
            finish_reason="stop",
            raw_hash=_hash(text),
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _bump(self, task_name: str) -> int:
        """递增并返回该任务的调用序号（从 0 开始）。"""
        index = self._counters.get(task_name, 0)
        self._counters[task_name] = index + 1
        return index

    def _raw_response(
        self,
        *,
        task_name: str,
        payload: dict[str, Any],
        attempt: int,
    ) -> MockResponse:
        """取脚本化响应，或回落到内置规则引擎。"""
        scripted = self._responses.get(task_name)
        if scripted:
            position = min(attempt, len(scripted) - 1)
            return scripted[position]
        if self._responder is not None:
            is_text = task_name == "response_renderer"
            return self._responder(
                task_name=task_name,
                payload=payload,
                attempt=attempt,
                is_text_task=is_text,
            )
        return _default_response(task_name, payload)

    def _apply_faults(
        self,
        *,
        task_name: str,
        attempt: int,
        raw: MockResponse,
    ) -> MockResponse:
        """注入故障。

        Raises:
            StructuredOutputError: 故障模式要求字典响应，但脚本给的是字符串。
        """
        current = raw
        for fault in self._faults:
            if fault.task_name != task_name or attempt >= fault.times:
                continue
            if fault.mode == "not_an_object":
                current = "这不是一个 JSON 对象"
                continue
            if fault.mode == "empty_text":
                current = "   "
                continue
            if not isinstance(current, dict):
                msg = f"故障注入需要字典响应，任务 {task_name!r} 收到字符串"
                raise StructuredOutputError(msg, task_name=task_name, provider=self._name)
            current = _mutate(current, fault)
        return current


def _hash(text: str) -> str:
    """返回内容的 SHA-256（只存哈希，不存内容）。"""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mutate(raw: dict[str, Any], fault: MockFault) -> dict[str, Any]:
    """按故障模式修改响应字典（返回副本，不改原脚本）。"""
    mutated = dict(raw)
    if fault.mode == "forbidden_extra_field":
        # 模型多返回一个 ``reasoning`` 字段——``extra="forbid"`` 必须拒绝它（红线一）
        mutated["reasoning"] = "这是被禁止的思维链字段，必须被 Schema 拒绝"
        return mutated
    assert fault.field is not None  # model_post_init 已保证
    if fault.mode == "drop_field":
        _delete_path(mutated, fault.field)
    elif fault.mode == "invalid_enum":
        _set_path(mutated, fault.field, "__不是合法枚举值__")
    return mutated


def _delete_path(target: dict[str, Any], path: str) -> None:
    """按点分路径删除字段；路径不存在时静默跳过。"""
    parts = path.split(".")
    cursor: Any = target
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return
        cursor = cursor[part]
    if isinstance(cursor, dict):
        cursor.pop(parts[-1], None)


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    """按点分路径设置字段；中间层不存在时创建空字典。"""
    parts = path.split(".")
    cursor: Any = target
    for part in parts[:-1]:
        if not isinstance(cursor, dict):
            return
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    if isinstance(cursor, dict):
        cursor[parts[-1]] = value


# ---------------------------------------------------------------------------
# 内置规则引擎
# ---------------------------------------------------------------------------

_SHORT_QUESTION_MARKERS = ("多少", "是什么", "什么是", "什么时候", "谁", "哪里", "几", "是否")
_VALUE_MARKERS = ("应该", "该不该", "值得", "应不应该", "对不对")
_CONFLICT_MARKERS = ("冲突", "矛盾", "两难", "争执", "分歧")
_CAUSAL_MARKERS = ("为什么", "原因", "导致", "造成", "因为")


def _shorten(text: str, limit: int) -> str:
    """截断文本用于展示。"""
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit]}…"


def _default_response(task_name: str, payload: dict[str, Any]) -> MockResponse:
    """内置规则引擎：按任务名从输入推导结构合法的响应。

    ⚠️ **这些规则只保证结构合法，不保证内容正确。**
    它们的作用是让流水线在没有真实模型时也能完整跑通；
    任何依赖内容正确性的断言都应当使用脚本化响应。

    Raises:
        StructuredOutputError: 任务名没有对应的规则。
    """
    builder = _RULES.get(task_name)
    if builder is None:
        msg = f"Mock 规则引擎不认识任务 {task_name!r}，请为其配置脚本化响应"
        raise StructuredOutputError(msg, task_name=task_name)
    return builder(payload)


def _concerns(payload: dict[str, Any]) -> MockResponse:
    message = _shorten(str(payload.get("user_message", "")), 60)
    return {
        "concerns": [
            {
                "statement": f"用户提出了需要回应的请求：{message}",
                "why_it_matters": "用户明确提出的请求，属于本次会话的直接目标",
                "category": "user_request",
                "impact": "moderate",
                "urgency": "moderate",
                "uncertainty": "moderate",
                "expected_information_value": "moderate",
                "cognitive_cost": "low",
                "should_start_round": True,
            }
        ]
    }


def _depth_signals_from_text(text: str) -> dict[str, Any]:
    """Mock 专用的浅层词面启发（**只用于让默认流水线跑得通**）。

    🔴 这不是生产逻辑。真实系统里"该多深"由模型给出的深度信号
    加上 :func:`ai_psi.cognition.depth_router.route_depth` 的确定性规则共同决定。
    """
    simple_fact = any(marker in text for marker in _SHORT_QUESTION_MARKERS)
    return {
        "simple_fact_with_sufficient_evidence": simple_fact,
        "needs_explanation_or_comparison": any(marker in text for marker in _CAUSAL_MARKERS),
        "multiple_plausible_interpretations": False,
        "user_explicitly_philosophical": any(marker in text for marker in _VALUE_MARKERS),
        "framework_conflict": False,
        "estimated_impact": "moderate",
        # 认得出是简单事实问题时，"措辞歧义"就不该再标成中等——
        # 那会让深度路由把 D0 挡在门外（D0 要求 ambiguity 低于 moderate）。
        # 认不出来时保守地标 moderate，让路由停在 D1 而不是冒进到 D0。
        "ambiguity": "low" if simple_fact else "moderate",
        "evidence_conflict": "high" if any(m in text for m in _CONFLICT_MARKERS) else "low",
        "value_conflict": "high" if any(m in text for m in _VALUE_MARKERS) else "low",
        "long_term_relevance": "low",
    }


def _inquiry(payload: dict[str, Any]) -> MockResponse:
    question = str(payload.get("concern_statement") or payload.get("user_message") or "未命名问题")
    message = str(payload.get("user_message", ""))
    return {
        "inquiry": {
            "question": question,
            "why_it_matters": str(payload.get("why_it_matters") or "用户明确提出的问题"),
            "scope": ["与本问题直接相关的可观察信息"],
            "out_of_scope": ["无法获取的第三方内部状态", "与本问题无关的历史话题"],
            "known_observations": list(payload.get("known_observations", [])),
            "key_unknowns": ["缺少可核验的一手材料"],
            "ambiguous_concepts": [],
            "assumptions_to_check": ["问题中的关键术语按通常含义理解"],
            "expected_output_type": "direct_answer",
            "verification_method": None,
            "stop_conditions": ["已给出可回答的结论，或明确说明当前无法判断"],
            "reopen_conditions": ["出现新的相关材料"],
            "depth_signals": _depth_signals_from_text(message),
        }
    }


def _hypotheses(payload: dict[str, Any]) -> MockResponse:
    maximum = int(payload.get("max_hypotheses", 4))
    question = _shorten(str(payload.get("question", "")), 40)
    candidates: list[dict[str, Any]] = [
        {
            "statement": f"关于「{question}」，当前材料不足以支持任何关于意图的判断",
            "category": "non_agentic",
            "predicted_observations": ["缺少可直接检验的一手材料"],
            "falsification_conditions": ["出现可核验的一手材料并指向某个具体意图"],
            "applicability": ["仅适用于本次讨论"],
            "uncertainty_type": "alethic",
        },
        {
            "statement": f"关于「{question}」，还有尚未获取的背景信息可能改变结论",
            "category": "alternative",
            "predicted_observations": ["补充背景信息后结论发生变化"],
            "falsification_conditions": ["穷尽可获取的背景信息后结论依然不变"],
            "applicability": ["仅适用于本次讨论"],
            "uncertainty_type": "epistemic",
        },
    ]
    return {"hypotheses": candidates[: max(1, min(maximum, len(candidates)))]}


def _logical(payload: dict[str, Any]) -> MockResponse:
    return {
        "claims": list(payload.get("claims", [])),
        "premises": list(payload.get("premises", [])),
        "inference_types": ["inductive"] if payload.get("claims") else [],
        "valid_links": [],
        "weak_links": [],
        "fallacy_risks": [],
        "counterexamples": [],
        "missing_information": ["尚无可核验的一手材料"],
    }


def _causal(payload: dict[str, Any]) -> MockResponse:
    claims = list(payload.get("causal_claims", []))
    return {
        "causal_claims": claims,
        "proposed_mechanisms": [],
        "confounders": [],
        "alternative_causes": [],
        "evidence_for_causation": [],
        # 🔴 默认规则不承认任何因果——没有机制的因果主张只能算相关
        "correlation_only": claims,
        "limitations": ["本次分析没有可用的机制证据"],
    }


def _concepts(payload: dict[str, Any]) -> MockResponse:
    terms = list(payload.get("concepts", []))
    return {
        "concepts": [
            {
                "term": term,
                "working_definition": f"本次讨论中，「{term}」按用户的用法理解",
                "alternative_definitions": [],
                "boundaries": [],
                "ambiguity_notes": [],
                "context_scope": ["本次讨论"],
            }
            for term in terms
        ],
        "ambiguities": [],
        "equivocation_risks": [],
        "false_dichotomy_risks": [],
        "descriptive_normative_confusions": [],
    }


def _dialectical(payload: dict[str, Any]) -> MockResponse:
    return {
        "current_position": str(payload.get("position", "")),
        "strongest_support": list(payload.get("supporting_reasons", [])),
        "strongest_opposition": [],
        "shared_premises": [],
        "scope_of_each_side": [],
        # 🔴 irreducible_tension 与 conditional_synthesis 至少填一个——
        # 两者皆空等于"这次分析没有产生判断力"（§9.8）
        "irreducible_tension": "现有材料不足以判定该张力能否被消解",
        "conditional_synthesis": None,
        "value_judgement_required": bool(payload.get("value_conflicts")),
        "is_false_balance": False,
    }


def _philosophical(payload: dict[str, Any]) -> MockResponse:
    unknowns = list(payload.get("factual_unknowns", []))
    return {
        "central_question": _shorten(str(payload.get("question", "")), 60),
        "ontological_questions": [],
        "epistemological_questions": [],
        "value_questions": list(payload.get("value_conflicts", [])),
        "agency_and_responsibility": [],
        "temporal_perspectives": [],
        "hidden_worldviews": [],
        "alternative_frameworks": [],
        "unresolved_tensions": list(payload.get("value_conflicts", [])),
        "practical_implications": [],
        # 🔴 事实层未知必须留下痕迹，不能被抽象语言消化掉（不变量 9）
        "epistemic_limits": [*unknowns, "框架分析不能替代缺失的事实材料"],
    }


def _judgment(payload: dict[str, Any]) -> MockResponse:
    unknowns = list(payload.get("key_unknowns", []))
    question = _shorten(str(payload.get("question", "")), 40)
    max_band = str(payload.get("max_confidence_band", "moderate"))
    # 🔴 不变量 3：有未解决未知时不得给出无保留结论
    action = "answer_with_caveat" if unknowns else "answer"
    return {
        "judgment": {
            "conclusion": f"关于「{question}」，当前能够给出的只是暂定看法",
            "rationale_summary": ["现有材料支持有限度的判断"],
            "strongest_counterarguments": [],
            "unresolved_unknowns": unknowns,
            "applicability": ["仅基于本次提供的材料"],
            "confidence_band": _min_band("low", max_band),
            "confidence_basis": ["结论建立在当前可获得的材料之上"],
            "revision_conditions": ["出现新的可核验材料"],
            "recommended_epistemic_action": action,
            "uncertainty_type": "alethic",
        }
    }


_BANDS = ("very_low", "low", "moderate", "high", "very_high")


def _min_band(proposed: str, ceiling: str) -> str:
    """返回两个置信档位中较低的一个。"""
    if ceiling not in _BANDS:
        return proposed if proposed in _BANDS else "low"
    return min(proposed, ceiling, key=_BANDS.index)


def _metacognition(payload: dict[str, Any]) -> MockResponse:
    new_evidence = bool(payload.get("new_evidence_present", False))
    returning = bool(payload.get("new_reasoning_path_present", False))
    loops_remaining = int(payload.get("loops_remaining", 0))
    calls_remaining = int(payload.get("model_calls_remaining", 0))
    can_continue = loops_remaining > 0 and calls_remaining > 0 and (new_evidence or returning)
    decision = "continue" if can_continue else "stop"
    return {
        "confirmation_bias_risk": "very_low",
        "user_pleasing_bias_risk": "very_low",
        "abstraction_escape_risk": "very_low",
        "unsupported_certainty_detected": False,
        "missing_counterexample_detected": False,
        "marginal_value": "moderate" if can_continue else "very_low",
        "proposed_decision": decision,
        "reasons": (
            ["本轮出现了新的分析材料，继续下去仍有边际收益"]
            if can_continue
            else ["没有新证据，也没有新的推理路径，继续分析不会带来新东西"]
        ),
    }


def _renderer(payload: dict[str, Any]) -> MockResponse:
    """默认渲染规则：**从 ResponsePlan 逐条组装**，不做任何自由发挥。

    这正是"渲染器不得改变判断的事实内容与置信等级"的最强实现——
    它没有内容可以改变，只有排版。
    """
    raw_plan = payload.get("plan")
    plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    lines: list[str] = []

    if plan.get("needs_clarification") and plan.get("clarification_question"):
        lines.append(str(plan["clarification_question"]))

    points = plan.get("direct_answer_points") or [str(payload.get("conclusion", ""))]
    lines.extend(str(point) for point in points)

    rationale = payload.get("rationale_summary") or []
    if rationale:
        lines.append("依据：" + "；".join(str(item) for item in rationale))

    alternatives = plan.get("alternative_explanations_to_show") or []
    if alternatives:
        lines.append("其他可能的解释：" + "；".join(str(item) for item in alternatives))

    uncertainties = plan.get("uncertainties_to_surface") or []
    if uncertainties:
        lines.append("需要说明的不确定性：" + "；".join(str(item) for item in uncertainties))

    applicability = payload.get("applicability") or []
    if applicability:
        lines.append("适用范围：" + "；".join(str(item) for item in applicability))

    return "\n".join(line for line in lines if line.strip())


#: 任务名到内置规则的映射。**新增 Prompt 任务时必须在此登记**，
#: 否则默认流水线会在该任务上失败（这是刻意的：静默返回空对象更糟）。
_RULES: dict[str, Callable[[dict[str, Any]], MockResponse]] = {
    "concern_detector": _concerns,
    "inquiry_framer": _inquiry,
    "hypothesis_generator": _hypotheses,
    "logical_analyzer": _logical,
    "causal_analyzer": _causal,
    "concept_analyzer": _concepts,
    "dialectical_analyzer": _dialectical,
    "philosophical_analyzer": _philosophical,
    "judgment_synthesizer": _judgment,
    "metacognition": _metacognition,
    "response_renderer": _renderer,
}
