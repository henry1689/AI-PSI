# 变异测试报告（阶段 6.5 §六）

> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**

## 逐模块分数

| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |
|---|---|---|---|---|---|---|---|
| `src/ai_psi/reliability/invariants.py` | 2 | 26 | **7.7%** | 24 | 104 | 0 | 0 |

**合计：2/26 = 7.7%**

⚠️ `incompetent` 是「变异之后代码根本跑不起来」（例如把文档字符串
换成数字），它不反映测试强度，因此**不计分**。

⚠️ `标注等价物` 是落在**类型标注**范围内的变异。被测模块全部启用
`from __future__ import annotations`（PEP 563），标注在运行期
只是一段字符串——改动它**必然**不改变行为。排除它们是去掉噪声，
不是把分数调上去；前提由 `_assert_pep563_is_active` 逐个模块核对。

## 存活变异体（逐条）


## 存活变异体（逐条）

### `src/ai_psi/reliability/invariants.py`

- **core/ReplaceFalseWithTrue** @ 第 265 行
  ```diff
  -            ok=False,
  +            ok=True,
  ```
- **core/ReplaceBinaryOperator_Add_BitXor** @ 第 303 行
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index ^ 1) for index in range(experience_count)],
  ```
- **core/ReplaceBinaryOperator_Add_Mul** @ 第 303 行
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index * 1) for index in range(experience_count)],
  ```
- **core/ReplaceBinaryOperator_Add_Pow** @ 第 303 行
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index ** 1) for index in range(experience_count)],
  ```
- **core/NumberReplacer** @ 第 246 行
  ```diff
  -    one = _probe_proposal(1)
  +    one = _probe_proposal( 0)
  ```
- **core/ReplaceComparisonOperator_Eq_Is** @ 第 72 行
  ```diff
  -        if item.invariant_id == invariant_id:
  +        if item.invariant_id is invariant_id:
  ```
- **core/ReplaceBinaryOperator_Add_LShift** @ 第 303 行
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index << 1) for index in range(experience_count)],
  ```
- **core/NumberReplacer** @ 第 353 行
  ```diff
  -    probe = _probe_proposal(1)
  +    probe = _probe_proposal( 0)
  ```
- **core/ReplaceBinaryOperator_Add_Div** @ 第 303 行
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index / 1) for index in range(experience_count)],
  ```
- **core/ReplaceComparisonOperator_Eq_GtE** @ 第 72 行
  ```diff
  -        if item.invariant_id == invariant_id:
  +        if item.invariant_id >= invariant_id:
  ```
- **core/NumberReplacer** @ 第 196 行
  ```diff
  -            f"（构造 {forbidden[0]} 会失败）"
  +            f"（构造 {forbidden[ -1]} 会失败）"
  ```
- **core/NumberReplacer** @ 第 353 行
  ```diff
  -    probe = _probe_proposal(1)
  +    probe = _probe_proposal( 2)
  ```
- **core/NumberReplacer** @ 第 250 行
  ```diff
  -        one.meets_escalation_threshold(threshold=1)
  +        one.meets_escalation_threshold(threshold= 0)
  ```
- **core/NumberReplacer** @ 第 303 行
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index + 2) for index in range(experience_count)],
  ```
- **core/ReplaceBinaryOperator_Sub_BitXor** @ 第 324 行
  ```diff
  -    removed = sorted(EXPECTED_PROPOSAL_STATUSES - actual)
  +    removed = sorted(EXPECTED_PROPOSAL_STATUSES ^ actual)
  ```
- **core/ReplaceBinaryOperator_Sub_BitXor** @ 第 323 行
  ```diff
  -    added = sorted(actual - EXPECTED_PROPOSAL_STATUSES)
  +    added = sorted(actual ^ EXPECTED_PROPOSAL_STATUSES)
  ```
- **core/NumberReplacer** @ 第 303 行
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index + 0) for index in range(experience_count)],
  ```
- **core/NumberReplacer** @ 第 196 行
  ```diff
  -            f"（构造 {forbidden[0]} 会失败）"
  +            f"（构造 {forbidden[ 1]} 会失败）"
  ```
- **core/ReplaceBinaryOperator_Add_FloorDiv** @ 第 303 行
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index // 1) for index in range(experience_count)],
  ```
- **core/NumberReplacer** @ 第 246 行
  ```diff
  -    one = _probe_proposal(1)
  +    one = _probe_proposal( 2)
  ```
- **core/NumberReplacer** @ 第 224 行
  ```diff
  -    if PROPOSAL_ESCALATION_THRESHOLD < 2:
  +    if PROPOSAL_ESCALATION_THRESHOLD < 3:
  ```
- **core/ReplaceComparisonOperator_Lt_LtE** @ 第 224 行
  ```diff
  -    if PROPOSAL_ESCALATION_THRESHOLD < 2:
  +    if PROPOSAL_ESCALATION_THRESHOLD <= 2:
  ```
- **core/NumberReplacer** @ 第 159 行
  ```diff
  -    raise ConstitutionViolationError(msg, invariant_id=failures[0].invariant_id)
  +    raise ConstitutionViolationError(msg, invariant_id=failures[ -1].invariant_id)
  ```
- **core/ReplaceTrueWithFalse** @ 第 52 行
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```

