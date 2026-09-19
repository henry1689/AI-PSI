"""经验的身份、评价与合并语义（阶段 6.5 §六 补齐）。

🔴 **本文件是变异测试的产物，不是补覆盖率。**

第一次全量变异跑完，``domain/experiences.py`` 只有 **43.6%**——133 个变异体
里 75 个存活，而且是**成片**存活的：

* 身份字段的 ``min_length=1`` 两侧（``0`` 与 ``2``）都没有断言；
* 三个 ``@model_validator`` 去掉装饰器之后没有任何用例变红——
  也就是说**校验函数从来没有真正拒绝过任何东西**；
* ``assess_experiences`` 的三个 ``for`` 循环被改成 ``for x in []``
  也全绿——合并逻辑的每一条规则都只被"输入为空"的用例覆盖过。

这三类共同指向一件事：**对象被构造出来了，但没有人试过把它构造错。**
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import (
    ConfidenceBand,
    ErrorType,
    ExperienceEvaluation,
    ExperienceEvaluator,
    ExperienceKind,
    VerificationStatus,
)
from ai_psi.domain.experiences import (
    EXTRACTOR_VERSION,
    Experience,
    ExperienceAssessment,
    ExperienceEvaluationRecord,
    assert_evaluator_may_produce,
    assess_experiences,
    canonical_key_for,
    evaluation_target_for_judgment,
)

pytestmark = pytest.mark.unit

#: 身份字段——都由 ``min_length=1`` 约束，且都参与某种"这是谁"的判定。
#:
#: ⚠️ ``canonical_key`` 与 ``independence_group`` **不在**这张表里：
#: 两者都由一致性校验钉死成派生值，而派生值最短也有好几十个字符，
#: 因此它们的 ``min_length`` 改不出行为差异
#: （登记为等价变异，见 ``mutation/run.py``）。
_IDENTITY_FIELDS = (
    "situation_signature",
    "inquiry_type",
    "evaluation_target",
    "extractor_version",
)


def _record(**overrides: Any) -> ExperienceEvaluationRecord:
    """构造一条追加评价。默认是"用户纠正 → CONFIRMED"，且带证据。"""
    payload: dict[str, Any] = {
        "created_by": "test",
        "experience_id": uuid4(),
        "experience_canonical_key": "round-outcome|1|judgment:x|v1",
        "evaluation": ExperienceEvaluation.CONFIRMED,
        "evaluator_type": ExperienceEvaluator.USER_CORRECTION,
        "evaluator_version": "user-correction/1",
        "evidence_refs": [uuid4()],
        "evaluated_at": datetime.fromisoformat("2026-09-19T00:00:00+00:00"),
    }
    payload.update(overrides)
    return ExperienceEvaluationRecord(**payload)


class TestIdentityFieldsRejectBlankButAcceptOneCharacter:
    """🔴 变异测试发现：``min_length=1`` 的**两侧**都是空的。

    * ``1 → 0``：空字符串过关。身份字段为空等于**没有身份**——
      ``canonical_key`` 会拿着一个空的目标去算键，而键是去重的依据。
    * ``1 → 2``：单字符被拒。**这一侧同样要紧**：把下界从 1 抬到 2
      不会让任何东西报错，只会让一类取值静默地写不进来。

    原有用例只测了 ``situation_signature`` 一个字段的"空串被拒"，
    另外四个字段两边都没测。
    """

    @pytest.mark.parametrize("field_name", _IDENTITY_FIELDS)
    def test_blank_is_refused(self, make_experience, field_name: str) -> None:
        with pytest.raises(ValidationError):
            make_experience(**{field_name: ""})

    @pytest.mark.parametrize("field_name", _IDENTITY_FIELDS)
    def test_a_single_character_is_enough(self, make_experience, field_name: str) -> None:
        assert getattr(make_experience(**{field_name: "x"}), field_name) == "x"

    @pytest.mark.parametrize("field_name", ["experience_canonical_key", "evaluator_version"])
    def test_the_evaluation_record_has_the_same_boundary(self, field_name: str) -> None:
        with pytest.raises(ValidationError):
            _record(**{field_name: ""})
        assert getattr(_record(**{field_name: "x"}), field_name) == "x"


class TestTheValidatorsActuallyRefuseThings:
    """🔴 变异测试发现：三个 ``@model_validator(mode="after")``
    **去掉装饰器之后没有任何用例变红**。

    装饰器一掉，被装饰的函数就只是一个普通方法——**永远不被调用**，
    而对象照常构造成功。所以这三条各自需要一个"必须炸"的反例。

    这也是这一节最值得留的教训：校验函数被写出来了、被注释解释了、
    看起来也跑过了（构造函数里确实调用了它），但**没有人试过让它拒绝**。
    """

    def test_a_hand_written_canonical_key_is_refused(self, make_experience) -> None:
        """``canonical_key`` 不是一个自由字段，是**派生值**。

        允许随手填一个，等于允许"两条不同的经验共用一个键"
        （被唯一约束静默拒绝）或"同一条经验换一个键"（被重复计数）。
        """
        with pytest.raises(ValidationError, match="canonical_key"):
            make_experience(canonical_key="我自己编的")

    def test_the_key_follows_the_facts(self, make_experience) -> None:
        """反向：事实改了、键没跟着改，同样要拒绝。

        这条才是那句「改了事实却忘了改键」的直接反例——
        上一条只证明了"乱填会被拒"，这一条证明的是**精确一致**。
        """
        experience = make_experience()
        round_id = uuid4()
        with pytest.raises(ValidationError, match="canonical_key"):
            make_experience(
                cognitive_round_id=round_id,
                # 键仍然是按**旧**回合算的
                canonical_key=experience.canonical_key,
            )

    def test_the_relation_does_not_matter_only_equality_does(self, make_experience) -> None:
        """🔴 ``!=`` 改成 ``>``（或 ``<``）之后，**一半的错键会被放行**。

        上面那一问用的键大还是小，取决于另一个回合 id 的字典序——
        也就是说它**能不能杀掉这条变异体是随机的**：id 恰好更小时杀掉，
        更大时放行。

        ``extractor_version`` 是键的最后一段，把它换成别的字符串就能
        **确定地**造出更大与更小两个方向的错键，不需要猜 uuid 的排序。
        两个方向都要试：判据只能是"相等"，不能是"谁大谁小"。
        """
        round_id, judgment_id = uuid4(), uuid4()
        target = evaluation_target_for_judgment(judgment_id)
        for version in ("0-比真版本小", "zzz-比真版本大"):
            with pytest.raises(ValidationError, match="canonical_key"):
                make_experience(
                    cognitive_round_id=round_id,
                    judgment_id=judgment_id,
                    evaluation_target=target,
                    canonical_key=canonical_key_for(
                        cognitive_round_id=round_id,
                        evaluation_target=target,
                        experience_kind=ExperienceKind.ROUND_OUTCOME,
                        extractor_version=version,
                    ),
                )

    def test_the_right_key_is_accepted(self, make_experience) -> None:
        """正向对照：没有它，上面两条对"一律拒绝"的实现也成立。"""
        round_id, judgment_id = uuid4(), uuid4()
        target = evaluation_target_for_judgment(judgment_id)
        built = make_experience(
            cognitive_round_id=round_id,
            judgment_id=judgment_id,
            evaluation_target=target,
            canonical_key=canonical_key_for(
                cognitive_round_id=round_id,
                evaluation_target=target,
                experience_kind=ExperienceKind.ROUND_OUTCOME,
                extractor_version=EXTRACTOR_VERSION,
            ),
        )
        assert built.canonical_key.endswith(EXTRACTOR_VERSION)

    # ------------------------------------------------------------------
    # 🔴 §八 评审 B 的 B0：``independence_group`` 曾经是一个
    # **没有任何校验**的自由字符串，而它才是门槛的计量单位。
    # 评审用"1 个回合 + 1 次真实纠正"从正式入口落库了一条提案。
    # ------------------------------------------------------------------

    def test_a_hand_written_independence_group_is_refused(self, make_experience) -> None:
        """🔴 分组是**派生值**，不是自由字段。

        它是门槛的计量单位——"这件事发生过几次"就是数它有几个不同取值。
        允许随手填，等于让**调用方**决定门槛有没有被跨过，
        而下游（门禁、提案、审计数字）看不出任何异常。

        ⚠️ **两个方向都要试**，理由与前面对 `canonical_key` 的那条完全相同：
        判据是"相等"，不是"谁大谁小"。只试一个方向的话，
        把 `!=` 改成 `<` 的变异体**能不能被杀死取决于字符串的字典序**——
        那是随机。**变异测试实测到了这一点**：第一版只用了
        `"idem:我自己编的"`（它恰好 `< "round:<uuid>"`），
        于是 `!=` → `<` 活了下来。

        🔴 `"aaa-…"` 与 `"zzz-…"` 是**确定**更小 / 更大的两个字符串。
        """
        for forged in ("aaa-比真分组小", "zzz-比真分组大"):
            with pytest.raises(ValidationError, match="independence_group"):
                make_experience(independence_group=forged)

    def test_the_group_follows_the_round(self, make_experience) -> None:
        """事实改了、分组没跟着改，同样要拒绝——与 ``canonical_key`` 对称。

        上一条只证明"乱填会被拒"，这一条证明的是**精确一致**：
        拿着**另一个回合**算出来的分组来构造，也必须炸。

        ⚠️ 两个回合 id 都用**确定的**排位（`int=1` 最小、
        `int=2**128-1` 最大）并**双向**跑一遍。用随机 uuid 的话，
        哪边大取决于运气——于是判据是 `<` 还是 `!=` 就测不出来。
        """
        low = UUID(int=1)
        high = UUID(int=2**128 - 1)
        for donor, target in ((low, high), (high, low)):
            with pytest.raises(ValidationError, match="independence_group"):
                make_experience(
                    cognitive_round_id=target,
                    independence_group=f"round:{donor}",
                )

    def test_the_idempotency_key_is_what_moves_the_group(self, make_experience) -> None:
        """正向对照：没有它，上面两条对"一律拒绝"的实现也成立。

        ⚠️ 造键要传 ``idempotency_key``，**不能**传 ``independence_group``——
        这正是 ``Experience`` 必须**同时**存下幂等键的原因：
        只存分组的话，校验器没有重算的输入，那条校验就等于没有。
        """
        round_id = uuid4()
        without_key = make_experience(cognitive_round_id=round_id, judgment_id=uuid4())
        assert without_key.independence_group == f"round:{round_id}"

        with_key = make_experience(
            cognitive_round_id=uuid4(), judgment_id=uuid4(), idempotency_key="client-retry-1"
        )
        assert with_key.independence_group == "idem:client-retry-1"

    def test_the_group_ignores_the_judgment_but_the_key_does_not(self, make_experience) -> None:
        """🔴 同一回合的**不同判断**共用分组，但它们的 ``canonical_key`` 不同。

        两件事都要钉住：

        * 分组的输入里**没有** ``judgment_id`` —— 同一回合的不同判断
          共享同一份证据、同一次模型调用、同一段推理上下文，因此
          **不是**彼此的独立证据（§二.12 有意反转了阶段 6 的结论）；
        * ``canonical_key`` 里有 —— 它们确实是不同的认知产物，
          不该被唯一约束当成同一条。
        """
        round_id = uuid4()
        first = make_experience(cognitive_round_id=round_id, judgment_id=uuid4())
        second = make_experience(cognitive_round_id=round_id, judgment_id=uuid4())
        assert first.judgment_id != second.judgment_id
        assert first.canonical_key != second.canonical_key
        assert first.independence_group == second.independence_group

    def test_internal_metacognition_cannot_self_confirm_at_extraction_time(
        self, make_experience
    ) -> None:
        """🔴 §二.4 的硬线在**抽取时刻**这一侧也必须真的执行。

        把 ``_check_extraction_time_evaluation`` 的装饰器去掉之后，
        一条"SUSPECTED + 用户纠正"的经验能直接构造出来——
        用户纠正发生在回合之后，抽取时刻不该有它。
        """
        with pytest.raises(ValidationError, match="内部元认知"):
            make_experience(
                evaluation=ExperienceEvaluation.SUPPORTED,
                evaluator_type=ExperienceEvaluator.INTERNAL_METACOGNITION,
            )

    def test_an_append_evaluation_cannot_be_self_confirmed(self) -> None:
        """同一个函数在**追加评价**这一侧的执行点。"""
        with pytest.raises(ValidationError, match="内部元认知"):
            _record(
                evaluation=ExperienceEvaluation.CONFIRMED,
                evaluator_type=ExperienceEvaluator.INTERNAL_METACOGNITION,
            )

    def test_a_confirmation_without_evidence_is_refused(self) -> None:
        """没有证据引用的"确认"是一条**不可复核的断言**。"""
        with pytest.raises(ValidationError, match="evidence_refs"):
            _record(evaluation=ExperienceEvaluation.SUPPORTED, evidence_refs=[])

    def test_the_higher_band_is_covered_too(self) -> None:
        """🔴 ``>=`` 的**上边界**：``CONFIRMED`` 同样必须给出证据。

        改成 ``==``（或 ``is``——在两档的 rank 都是小整数时与 ``==`` 等价）
        之后，只有**恰好 SUPPORTED** 那一档要求证据，
        **更高**的 ``CONFIRMED`` 反而不要求。而 CONFIRMED 才是那个
        会把经验永久抬到最高处的评价。
        """
        with pytest.raises(ValidationError, match="evidence_refs"):
            _record(evaluation=ExperienceEvaluation.CONFIRMED, evidence_refs=[])

    def test_a_confirmation_with_evidence_is_accepted(self) -> None:
        assert _record(evaluation=ExperienceEvaluation.SUPPORTED, evidence_refs=[uuid4()])

    def test_suspected_does_not_demand_evidence(self) -> None:
        """🔴 边界值本身就是契约：``>= SUPPORTED`` 里的 ``>=``。

        改成 ``>`` 之后 ``SUPPORTED`` 不再要求证据，而"CONFIRMED 要求证据"
        那条断言照样通过——于是一条**没有证据的 SUPPORTED** 会被接受，
        而它同样会让经验永久地高于其他经验。

        ⚠️ 这里只有 SUSPECTED 一档：``UNASSESSED`` 的评价记录**整体**
        被 ``assert_evaluator_may_produce`` 拒绝（"做过评价就是有状态"），
        根本走不到这条长度检查。
        """
        assert (
            _record(evaluation=ExperienceEvaluation.SUSPECTED, evidence_refs=[]).evaluation
            is ExperienceEvaluation.SUSPECTED
        )

    def test_an_unassessed_record_is_refused_outright(self) -> None:
        """没有"评估者说这件事没被评估过"这种东西。"""
        with pytest.raises(ValidationError, match="unassessed"):
            _record(evaluation=ExperienceEvaluation.UNASSESSED, evidence_refs=[])


class TestTheOriginRoundAliasIsAProperty:
    """🔴 变异测试发现：``origin_round_id`` 上的 ``@property`` 去掉之后全绿。

    去掉之后它变成一个普通方法，``experience.origin_round_id`` 拿到的是
    绑定方法而不是 UUID——静默地类型就不对了。"
    """

    def test_it_returns_the_round_id_itself(self, make_experience) -> None:
        round_id = uuid4()
        experience = make_experience(cognitive_round_id=round_id)
        assert experience.origin_round_id == round_id
        assert isinstance(experience.origin_round_id, UUID)

    def test_it_is_not_a_second_field(self, make_experience) -> None:
        """🔴 别名**不是**第二个字段：两栏会在某条更新路径上分家。"""
        assert "origin_round_id" not in Experience.model_fields
        assert "cognitive_round_id" in Experience.model_fields


class TestAttributabilityHasABoundary:
    """🔴 变异测试发现：``is_attributable`` 里的 ``>=`` 边界没有被断言。

    ``>= LOW`` 改成 ``> LOW`` 之后，"恰好 LOW"这一档从**可以**归因变成
    不可以——而既有用例只测了 VERY_LOW（不可）与 MODERATE（可），
    恰好跳过了边界本身。
    """

    def test_exactly_low_is_attributable(self, make_experience) -> None:
        assert make_experience(
            error_type=ErrorType.REASONING_ERROR,
            attribution_confidence=ConfidenceBand.LOW,
        ).is_attributable

    def test_one_band_below_is_not(self, make_experience) -> None:
        assert not make_experience(
            error_type=ErrorType.REASONING_ERROR,
            attribution_confidence=ConfidenceBand.VERY_LOW,
        ).is_attributable

    def test_no_error_type_short_circuits(self, make_experience) -> None:
        """``and`` 的前一半：没有错误类型时，再高的置信度也不可归因。

        改成 ``or`` 之后，"置信度高"这一个条件就足以判为可归因——
        一条**没有错误类型**的经验会被当成可归因的。
        """
        assert not make_experience(
            error_type=None, attribution_confidence=ConfidenceBand.VERY_HIGH
        ).is_attributable


class TestAssessExperiencesActuallyCombines:
    """🔴 变异测试发现：``assess_experiences`` 的三个 ``for`` 循环
    被改成 ``for x in []`` 之后**全部存活**。

    也就是说，既有用例只验证过"空输入返回空"，合并规则的每一条
    （取最高档、忽略孤立记录、收集评估者与证据）都没有被验证过。
    """

    def test_every_experience_gets_an_assessment(self, make_experience) -> None:
        experiences = [make_experience() for _ in range(3)]
        assert len(assess_experiences(experiences)) == 3

    def test_a_record_raises_the_evaluation(self, make_experience) -> None:
        """🔴 这条是整个模块存在的理由：抽取时刻最多 SUSPECTED，
        后续评价把它抬起来。循环体被清空的话它会留在 SUSPECTED。"""
        experience = make_experience(
            evaluation=ExperienceEvaluation.SUSPECTED,
            evaluator_type=ExperienceEvaluator.INTERNAL_METACOGNITION,
        )
        record = _record(
            experience_id=experience.id,
            evaluation=ExperienceEvaluation.CONFIRMED,
            evaluator_type=ExperienceEvaluator.USER_CORRECTION,
        )

        (assessment,) = assess_experiences([experience], [record])
        assert assessment.evaluation is ExperienceEvaluation.CONFIRMED

    def test_a_later_lower_ranked_record_does_not_downgrade(self, make_experience) -> None:
        """🔴 **取最高档，不是取最后一条**（见 ``assess_experiences`` 的说明）。

        降级是**无声**的：它的表现只是"这条模式突然少了一次计数"。

        ⚠️ 「更低档」只可能出现在**两条记录之间**，不会出现在
        「记录 vs 经验」之间：抽取时刻最高只到 ``SUSPECTED``，
        而记录最低也只能是 ``SUSPECTED``（``UNASSESSED`` 被校验器挡掉）。
        所以反例必须构造"先高后低"的顺序。
        """
        experience = make_experience()
        high = _record(
            experience_id=experience.id,
            evaluation=ExperienceEvaluation.CONFIRMED,
            evaluator_type=ExperienceEvaluator.USER_CORRECTION,
        )
        low = _record(
            experience_id=experience.id,
            evaluation=ExperienceEvaluation.SUSPECTED,
            evaluator_type=ExperienceEvaluator.INTERNAL_METACOGNITION,
            evidence_refs=[],
        )

        (assessment,) = assess_experiences([experience], [high, low])
        assert assessment.evaluation is ExperienceEvaluation.CONFIRMED

    def test_the_order_of_records_does_not_matter(self, make_experience) -> None:
        experience = make_experience(
            evaluation=ExperienceEvaluation.SUSPECTED,
            evaluator_type=ExperienceEvaluator.INTERNAL_METACOGNITION,
        )
        low = _record(
            experience_id=experience.id,
            evaluation=ExperienceEvaluation.SUSPECTED,
            evaluator_type=ExperienceEvaluator.LATER_EVIDENCE,
            evidence_refs=[],
        )
        high = _record(
            experience_id=experience.id,
            evaluation=ExperienceEvaluation.SUPPORTED,
            evaluator_type=ExperienceEvaluator.USER_CORRECTION,
            evidence_refs=[uuid4()],
        )

        forward = assess_experiences([experience], [low, high])[0]
        backward = assess_experiences([experience], [high, low])[0]
        assert forward.evaluation is backward.evaluation is ExperienceEvaluation.SUPPORTED

    def test_every_evaluator_is_kept(self, make_experience) -> None:
        """🔴 只留最高分那个会丢掉"这条 CONFIRMED 是谁说的"。"""
        experience = make_experience(
            evaluation=ExperienceEvaluation.SUSPECTED,
            evaluator_type=ExperienceEvaluator.INTERNAL_METACOGNITION,
        )
        record = _record(
            experience_id=experience.id,
            evaluation=ExperienceEvaluation.CONFIRMED,
            evaluator_type=ExperienceEvaluator.USER_CORRECTION,
        )

        (assessment,) = assess_experiences([experience], [record])
        assert set(assessment.evaluator_types) == {
            ExperienceEvaluator.INTERNAL_METACOGNITION,
            ExperienceEvaluator.USER_CORRECTION,
        }

    def test_evidence_refs_are_collected_and_deduplicated(self, make_experience) -> None:
        shared = uuid4()
        experience = make_experience(
            evaluation=ExperienceEvaluation.SUSPECTED,
            evaluator_type=ExperienceEvaluator.INTERNAL_METACOGNITION,
            evaluation_evidence_refs=[shared],
        )
        record = _record(
            experience_id=experience.id,
            evaluation=ExperienceEvaluation.CONFIRMED,
            evaluator_type=ExperienceEvaluator.USER_CORRECTION,
            evidence_refs=[shared, uuid4()],
        )

        (assessment,) = assess_experiences([experience], [record])
        assert len(assessment.evidence_refs) == 2
        assert shared in assessment.evidence_refs

    def test_an_experience_without_an_evaluator_stays_unassessed(self, make_experience) -> None:
        """没有评估者的经验不该凭空多出一个评估者。"""
        experience = make_experience()
        (assessment,) = assess_experiences([experience])
        assert assessment.evaluation is ExperienceEvaluation.UNASSESSED
        assert assessment.evaluator_types == ()

    def test_an_orphan_record_is_ignored(self, make_experience) -> None:
        """指向不存在经验的记录被忽略——但它不该让整次读取失败。"""
        experience = make_experience()
        orphan = _record(experience_id=uuid4())  # 不是这一条的 id

        (assessment,) = assess_experiences([experience], [orphan])
        assert assessment.evaluation is ExperienceEvaluation.UNASSESSED
        assert assessment.evaluator_types == ()

    def test_an_orphan_record_does_not_invent_an_experience(self, make_experience) -> None:
        assert assess_experiences([], [_record()]) == ()

    def test_assessments_are_in_the_order_of_the_input(self, make_experience) -> None:
        experiences = [make_experience() for _ in range(3)]
        assessed = assess_experiences(list(reversed(experiences)))
        assert [item.experience.id for item in assessed] == [
            item.id for item in reversed(experiences)
        ]


class TestTheAssessmentIsAnImmutableValue:
    """🔴 变异测试发现：``ExperienceAssessment`` 的 ``frozen=True``
    改成 ``False`` 之后全绿。

    它是**合并结果**：调用方拿到之后再改它的 ``evaluation``，
    等于让"门槛按什么计数"与"评估出来的结论"分家。
    """

    def test_it_cannot_be_mutated(self, make_experience) -> None:
        (assessment,) = assess_experiences([make_experience()])
        with pytest.raises(FrozenInstanceError):
            assessment.evaluation = ExperienceEvaluation.CONFIRMED  # type: ignore[misc]

    def test_the_fields_are_what_they_say(self, make_experience) -> None:
        assessment = ExperienceAssessment(
            experience=make_experience(),
            evaluation=ExperienceEvaluation.SUSPECTED,
        )
        assert assessment.evaluator_types == ()
        assert assessment.evidence_refs == ()


class TestTheSingleEnforcementPointHasEveryBranch:
    """🔴 §二.4 的**唯一执行点**（:func:`assert_evaluator_may_produce`）。

    变异测试在这里留下 8 个存活——因为它每个分支的判断都是
    比较或 ``is``，而既有用例只走了"内部元认知 + SUPPORTED 会抛"这一条。

    每一格都必须被单独走过：这是一个**二维**规则
    （谁说的 × 说了什么），只测对角线等于没测。
    """

    @pytest.mark.parametrize("evaluator", list(ExperienceEvaluator))
    @pytest.mark.parametrize("evaluation", list(ExperienceEvaluation))
    def test_the_full_matrix(
        self, evaluator: ExperienceEvaluator, evaluation: ExperienceEvaluation
    ) -> None:
        allowed = (
            evaluator is ExperienceEvaluator.INTERNAL_METACOGNITION
            and evaluation in {ExperienceEvaluation.SUSPECTED}
        ) or (
            evaluator is not ExperienceEvaluator.INTERNAL_METACOGNITION
            and evaluation is not ExperienceEvaluation.UNASSESSED
        )
        if allowed:
            assert_evaluator_may_produce(evaluator=evaluator, evaluation=evaluation, where="矩阵")
            return
        with pytest.raises(ValueError):
            assert_evaluator_may_produce(evaluator=evaluator, evaluation=evaluation, where="矩阵")

    def test_no_evaluator_means_no_assessment(self) -> None:
        """「谁说的」与「说了什么」必须同时存在，也必须同时缺席。"""
        assert_evaluator_may_produce(
            evaluator=None,
            evaluation=ExperienceEvaluation.UNASSESSED,
            where="矩阵",
        )
        with pytest.raises(ValueError, match="没有评估者"):
            assert_evaluator_may_produce(
                evaluator=None,
                evaluation=ExperienceEvaluation.SUSPECTED,
                where="矩阵",
            )

    def test_the_error_message_says_where(self) -> None:
        """两处调用共用这一个函数；报错必须说得出是哪一条路径。"""
        with pytest.raises(ValueError, match="抽取时刻"):
            assert_evaluator_may_produce(
                evaluator=ExperienceEvaluator.INTERNAL_METACOGNITION,
                evaluation=ExperienceEvaluation.CONFIRMED,
                where="Experience（抽取时刻）",
            )


class TestTheEnumLiteralsSomeEquivalencesRestOn:
    """🔴 **这条用例守的不是行为，是别处几条等价登记的前提。**

    ``mutation/run.py`` 里登记了若干"枚举比较"的等价变异，它们的成立
    依赖两件事：

    1. **成员是单例**，所以 ``x is M`` 与 ``x == M`` 对成员输入同答案；
    2. **成员的字面量**：StrEnum 的 ``<`` / ``>=`` 比的是**字符串**，
       而 ``ExperienceEvaluation`` 的另外三个值恰好都排在
       ``"unassessed"`` **前面**，于是 ``x < UNASSESSED`` 与
       ``x is not UNASSESSED`` 在这些取值上答案相同。

    第 2 条是**侥幸**，不是必然：改掉任何一个成员的字面量，
    一批"等价"立刻变成真变异。这条用例的作用就是在那一刻先红，
    而不是让等价表悄悄开始放行真变异。
    """

    def test_the_evaluation_literals_are_what_the_equivalences_assume(self) -> None:
        assert [member.value for member in ExperienceEvaluation] == [
            "unassessed",
            "suspected",
            "supported",
            "confirmed",
        ]
        assert all(
            member.value < "unassessed"
            for member in ExperienceEvaluation
            if member is not ExperienceEvaluation.UNASSESSED
        )

    def test_the_ranks_are_distinct(self) -> None:
        """等秩即同成员——这是"赋一个等秩的评价是空操作"那条等价的前提。"""
        ranks = [member.rank for member in ExperienceEvaluation]
        assert len(set(ranks)) == len(ranks)

    def test_the_evaluator_literals_are_stable(self) -> None:
        assert [member.value for member in ExperienceEvaluator] == [
            "internal_metacognition",
            "later_evidence",
            "user_correction",
            "independent_evaluation",
        ]


class TestVerificationStatusIsIndependentOfEvaluation:
    """两者语义不同，不该被同一个变异体一起改掉。"""

    def test_they_move_independently(self, make_experience) -> None:
        experience = make_experience(
            verification_status=VerificationStatus.UNVERIFIED,
            evaluation=ExperienceEvaluation.CONFIRMED,
            evaluator_type=ExperienceEvaluator.USER_CORRECTION,
            evaluation_evidence_refs=[uuid4()],
        )
        assert experience.verification_status is VerificationStatus.UNVERIFIED
        assert experience.evaluation is ExperienceEvaluation.CONFIRMED
