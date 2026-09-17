"""全部 Prompt 契约的注册清单（任务书 §8.3）。

🔴 **改提示词 = 改这里 + 改模板 + 追加 changelog。**
三件事缺一，契约构造就会失败——版本号在 changelog 中没有条目是**报错**，
不是警告。

任务清单对应任务书 §9 的认知模块。与 `docs/prompt_contracts.md` §3 的差异：
``response_planner`` **不再是 Prompt 任务**，改为确定性代码
（理由见 :class:`~ai_psi.prompts.schemas.ResponsePlan` 的文档与 ADR-0015）。
因此本模块恰好注册 **11 个**模型任务。
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel

from ai_psi.prompts import schemas as s
from ai_psi.prompts.registry import ChangelogEntry, PromptContract, PromptExample, PromptRegistry

__all__ = ["CONTRACTS", "PROMPT_VERSION_V1", "build_default_registry"]

#: 全部任务的首个语义版本。各任务独立演进，此处只是共同起点。
PROMPT_VERSION_V1: Final[str] = "1.0.0"

# 🔴 **版本号跟踪的是「模板正文」，不是「传输参数」。**
#
# ``max_output_tokens`` 是发给供应商的**上限**，它不改变提示词说了什么，
# 也就不改变模型的行为——除非它小到把输出截断（那时是配置错误，不是语义变化）。
# 因此阶段 4 按实测重标定这个数值时**没有**递增版本号；
# 改动与依据记录在 ADR-0016，模板正文一字未改。
#
# 反过来，任何改动 ``templates/`` 下文件内容的变更**必须**递增版本号。

_INITIAL: Final[tuple[ChangelogEntry, ...]] = (
    ChangelogEntry(
        version=PROMPT_VERSION_V1,
        change="初版",
        reason="阶段 3 首次建立 Prompt 契约体系",
    ),
)


def _contract(
    *,
    task_name: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel] | None,
    max_input_tokens: int,
    max_output_tokens: int,
    example_name: str,
    example_payload: dict[str, Any],
    version: str = PROMPT_VERSION_V1,
    changelog: tuple[ChangelogEntry, ...] | None = None,
) -> PromptContract:
    """构造一个契约，并统一挂上一个测试样例。

    🔴 **版本是逐个任务独立演进的。** 改了一个任务的提示词，
    不该让另外十个任务的版本号跟着动——那会让"版本"失去定位能力：
    回放历史回合时，需要知道的是**那一次用的到底是哪份模板**。

    Args:
        task_name: 全局唯一任务名。
        description: 该任务做什么。
        input_model: 输入 Schema。
        output_model: 输出 Schema；``None`` 表示纯文本任务。
        max_input_tokens: 输入估算长度上限。
        max_output_tokens: 输出长度上限。
        example_name: 样例名。
        example_payload: 契约自带的测试样例输入。
        version: 语义版本号；默认初版。
        changelog: 变更记录；``None`` 时使用初版记录。

    Returns:
        构造好的契约。
    """
    return PromptContract(
        task_name=task_name,
        version=version,
        description=description,
        input_model=input_model,
        output_model=output_model,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        examples=(
            PromptExample(
                name=example_name,
                payload=example_payload,
                notes="契约完整性测试用它验证输入 Schema 可被实例化",
            ),
        ),
        changelog=changelog if changelog is not None else _INITIAL,
    )


#: 全部 Prompt 契约。任务名即 :attr:`ModelInvocationInfo.task_name`（不变量 18）。
CONTRACTS: Final[tuple[PromptContract, ...]] = (
    _contract(
        task_name="concern_detector",
        description="从当前事件与会话状态中识别值得投入认知资源的关切（§9.1）",
        input_model=s.ConcernDetectorInput,
        output_model=s.ConcernDetectorOutput,
        max_input_tokens=4000,
        max_output_tokens=1500,
        example_name="用户提出一个事实问题",
        example_payload={
            "user_message": "水在标准大气压下通常多少摄氏度沸腾？",
            "conversation_summary": [],
            "open_questions": [],
            "confirmed_user_goals": [],
            "system_status": ["正常"],
        },
        version="1.1.0",
        changelog=(
            ChangelogEntry(
                version="1.0.0",
                change="初版",
                reason="阶段 3 首次建立 Prompt 契约体系",
            ),
            ChangelogEntry(
                version="1.1.0",
                change=(
                    "明确「用户正在提问时，至少必须输出一个 user_request 类关切」；"
                    "把空列表的适用范围限定为系统自发触发的场景"
                ),
                reason=(
                    "阶段 4 的 live 测试第一次跑真实模型时发现：对"
                    "「水在标准大气压下多少摄氏度沸腾？」这类直接提问，"
                    "模型选择了「没有值得启动认知的关切」这条出口，"
                    "回合以 NO_CONCERN_DETECTED 结束，用户拿不到任何回答。"
                    "旧提示词把「空列表是合法结论」写得很显眼，"
                    "却没有说明它**不适用于用户正在提问的情形**"
                ),
            ),
        ),
    ),
    _contract(
        task_name="inquiry_framer",
        description="把关切转化为边界清晰、可结束的认知问题，并给出深度信号（§9.2、§7.2）",
        input_model=s.InquiryFramerInput,
        output_model=s.InquiryFramerOutput,
        max_input_tokens=4000,
        max_output_tokens=1500,
        example_name="事实问题的框定",
        example_payload={
            "concern_statement": "用户想知道标准大气压下水沸腾的温度",
            "why_it_matters": "这是用户明确提出的问题",
            "user_message": "水在标准大气压下通常多少摄氏度沸腾？",
            "known_observations": [],
            "current_beliefs": [],
        },
    ),
    _contract(
        task_name="hypothesis_generator",
        description="生成 1–4 个有实质差异的候选假设，至少包含一个非人格化解释（§9.6）",
        input_model=s.HypothesisGeneratorInput,
        output_model=s.HypothesisGeneratorOutput,
        max_input_tokens=6000,
        max_output_tokens=2000,
        example_name="关系推测类问题的多假设",
        example_payload={
            "question": "朋友只回复一个「嗯」有哪些可能的解释？",
            "scope": ["可观察的通信行为"],
            "key_unknowns": ["朋友当时的处境"],
            "evidence_summaries": ["用户转述：对方只回了一个「嗯」"],
            "max_hypotheses": 4,
            "requires_non_agentic": True,
        },
    ),
    _contract(
        task_name="logical_analyzer",
        description="检查前提与结论的连接、谬误风险与缺失信息（§9.7）",
        input_model=s.LogicalAnalyzerInput,
        output_model=s.LogicalAnalysisOutput,
        max_input_tokens=6000,
        max_output_tokens=1800,
        example_name="简单论证的逻辑检查",
        example_payload={
            "question": "朋友的简短回复是否说明关系变差？",
            "claims": ["回复很短说明他不在意我"],
            "premises": ["在意一个人就会认真回复"],
            "hypotheses": ["他当时正在忙"],
        },
    ),
    _contract(
        task_name="causal_analyzer",
        description="区分相关与因果，列出混杂因素与替代原因（§9.7 的因果部分）",
        input_model=s.CausalAnalyzerInput,
        output_model=s.CausalAnalysisOutput,
        max_input_tokens=4000,
        max_output_tokens=2000,
        example_name="相关被当成因果",
        example_payload={
            "question": "工作压力是否导致了他的沉默？",
            "causal_claims": ["他最近不说话，是因为工作压力大"],
        },
    ),
    _contract(
        task_name="concept_analyzer",
        description="识别歧义概念、隐含定义、概念偷换与过度二分（§9.5）",
        input_model=s.ConceptAnalyzerInput,
        output_model=s.ConceptAnalysisOutput,
        max_input_tokens=4000,
        max_output_tokens=2600,
        example_name="价值冲突中的概念澄清",
        example_payload={
            "question": "一个人应该坚持自我，还是适应环境？",
            "concepts": ["坚持自我", "适应环境"],
            "context": ["用户把二者当作互斥选项"],
        },
    ),
    _contract(
        task_name="dialectical_analyzer",
        description="给出最强支持与最强反方，指出真实张力或有条件的综合（§9.8）",
        input_model=s.DialecticalAnalyzerInput,
        output_model=s.DialecticalAnalysisOutput,
        max_input_tokens=6000,
        max_output_tokens=1800,
        example_name="价值取舍问题",
        example_payload={
            "question": "一个人应该坚持自我，还是适应环境？",
            "position": "应当优先坚持自我",
            "supporting_reasons": ["失去自我会带来长期痛苦"],
            "value_conflicts": ["个人完整性 vs 社会融入"],
        },
    ),
    _contract(
        task_name="philosophical_analyzer",
        description="D4 的框架层分析：本体、认识、价值与主体性问题（§9.9）",
        input_model=s.PhilosophicalAnalyzerInput,
        output_model=s.PhilosophicalAnalysisOutput,
        max_input_tokens=6000,
        max_output_tokens=2500,
        example_name="存在论层面的框架冲突",
        example_payload={
            "question": "一个人应该坚持自我，还是适应环境？",
            "value_conflicts": ["个体自主 vs 关系归属"],
            "factual_unknowns": ["该选择在具体处境下的长期后果未知"],
        },
    ),
    _contract(
        task_name="judgment_synthesizer",
        description="把假设、证据与未知合成为有依据、有限定、可修正的暂定判断（§9.10）",
        input_model=s.JudgmentSynthesizerInput,
        output_model=s.JudgmentSynthesizerOutput,
        max_input_tokens=10000,
        max_output_tokens=2000,
        example_name="证据不足时的暂定判断",
        example_payload={
            "question": "朋友的简短回复有哪些可能的解释？",
            "scope": ["可观察的通信行为"],
            "evidence_summaries": ["用户转述：对方只回了一个「嗯」"],
            "hypothesis_summaries": ["他当时正在忙（候选）", "消息可能没被认真看（候选）"],
            "key_unknowns": ["朋友当时的处境"],
            "max_confidence_band": "low",
        },
    ),
    _contract(
        task_name="metacognition",
        description="模型层的偏差自检：迎合、确认偏差、抽象逃逸、不恰当确定（§9.11）",
        input_model=s.MetacognitionInput,
        output_model=s.MetacognitionOutput,
        max_input_tokens=4000,
        max_output_tokens=1500,
        example_name="第二轮元认知检查",
        example_payload={
            "question": "朋友的简短回复有哪些可能的解释？",
            "judgment_conclusion": "目前没有足够依据判断对方的意图",
            "judgment_confidence": "low",
            "unresolved_unknowns": ["朋友当时的处境"],
            "loop_index": 1,
            "loops_remaining": 0,
            "model_calls_remaining": 2,
            "new_evidence_present": False,
            "new_reasoning_path_present": False,
            "repeated_claim_score": 1.0,
            "stop_conditions_satisfied": True,
            "user_message_summary": "他是不是讨厌我？",
        },
    ),
    _contract(
        task_name="response_renderer",
        description="把已定的 ResponsePlan 渲染为自然语言；不得改变判断内容与置信等级（§9.12）",
        input_model=s.ResponseRendererInput,
        output_model=None,
        max_input_tokens=6000,
        max_output_tokens=2000,
        example_name="带限定的回答渲染",
        example_payload={
            "plan": {
                "direct_answer_points": ["没有足够依据判断对方意图"],
                "uncertainties_to_surface": ["朋友当时的处境未知"],
                "alternative_explanations_to_show": ["他当时正在忙"],
                "withheld_candidates": [],
                "needs_clarification": False,
                "clarification_question": None,
                "length_hint": "short",
                "depth_hint": "d1",
                "tone": "cautious",
                "allows_strong_conclusion": False,
                "response_style": "structured",
            },
            "conclusion": "目前没有足够依据判断对方的意图",
            "rationale_summary": ["单一观察不足以支撑人际推断"],
            "applicability": ["仅基于这一次通信记录"],
            "confidence_band": "low",
            "epistemic_action": "answer_with_caveat",
            "response_style": "structured",
        },
    ),
)


def build_default_registry() -> PromptRegistry:
    """构造使用包内模板的默认注册表。

    Returns:
        默认 :class:`~ai_psi.prompts.registry.PromptRegistry`。
    """
    return PromptRegistry(CONTRACTS)
