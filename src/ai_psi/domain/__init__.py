"""AI-PSI 领域层。

本包是全部认知逻辑的共同语言，**只依赖 pydantic 与标准库**——
不做 IO、不含 async、不 import 项目内其他包（架构规则 1）。

之所以把这条规则称为"硬性"：领域对象一旦依赖了外部世界，
所有使用它的地方都会被间接耦合，Provider 替换与内存测试都会失效。

导出约定：本模块只做便利性再导出。**内部模块之间仍应显式 import**，
避免出现"只改 ``__init__`` 就意外改变依赖关系"的情况。
"""

from __future__ import annotations

from ai_psi.domain.assumptions import Assumption
from ai_psi.domain.beliefs import Belief
from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.common import (
    SCHEMA_VERSION_V1,
    EntityMetadata,
    UtcDatetime,
    UtcDatetimeOptional,
    utc_now,
)
from ai_psi.domain.concepts import Concept
from ai_psi.domain.concerns import Concern
from ai_psi.domain.events import Event, ModelInvocationInfo
from ai_psi.domain.evidence import Evidence
from ai_psi.domain.experiences import Experience
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.improvement_proposals import (
    PROPOSAL_ESCALATION_THRESHOLD,
    ImprovementProposal,
)
from ai_psi.domain.inquiries import Inquiry
from ai_psi.domain.judgments import Judgment
from ai_psi.domain.memories import Memory
from ai_psi.domain.observations import Observation
from ai_psi.domain.reflections import Reflection
from ai_psi.domain.situations import Situation
from ai_psi.domain.user_models import UserModel

__all__ = [
    "PROPOSAL_ESCALATION_THRESHOLD",
    "SCHEMA_VERSION_V1",
    "Assumption",
    "Belief",
    "CognitiveBudget",
    "CognitiveRound",
    "Concept",
    "Concern",
    "EntityMetadata",
    "Event",
    "Evidence",
    "Experience",
    "Hypothesis",
    "ImprovementProposal",
    "Inquiry",
    "Judgment",
    "Memory",
    "ModelInvocationInfo",
    "Observation",
    "Reflection",
    "Situation",
    "UserModel",
    "UtcDatetime",
    "UtcDatetimeOptional",
    "utc_now",
]
