from __future__ import annotations

import pytest

from anlas_sync.extract import extract_family_samplers


SAMPLER_ENUM = {"KE": "k_euler", "KEA": "k_euler_ancestral", "KL": "k_lms"}
FAMILY_ENUM = {"v4": "v4", "v5": "v5"}

# 模块 32036 家族-采样器表的真实结构（已简化）：
# let n=e=>{switch(e){case r.lh.<家族>:return <混淆变量>;...}},o=[...],l=[...];function h(e)
GOOD_JS = (
    "let n=e=>{switch(e){case r.lh.v4:return o;case r.lh.v5:return l;}},"
    "o=[{value:r.l1.KE},{value:r.l1.KEA}],l=[{value:r.l1.KE},{value:r.l1.KL}];"
    "function h(e)"
)


def test_extracts_family_samplers_from_expected_shape():
    assert extract_family_samplers(GOOD_JS, SAMPLER_ENUM, FAMILY_ENUM) == {
        "v4": ["k_euler", "k_euler_ancestral"],
        "v5": ["k_euler", "k_lms"],
    }


def test_aborts_when_switch_shape_changes_instead_of_returning_empty_table():
    """switch 变形会让 mapping 为空 -> 整张表为空，必须中止而不是写出空表。

    这张表是 app 层 v4/v5 采样器硬校验的依据，空表会让对应模型的文生图
    全部被代理 400 拦掉（generate_validation 只剩 V4_V5_EXTRA_SAMPLERS 三个）。
    """
    js = "let n=e=>{switch(e){default:return o;}},o=[{value:r.l1.KE}];function h(e)"

    with pytest.raises(SystemExit) as exc:
        extract_family_samplers(js, SAMPLER_ENUM, FAMILY_ENUM)
    assert "为空" in str(exc.value)


def test_aborts_when_minified_variable_names_change():
    """混淆变量名从 o/l/s/d/u 改掉后 tables 取不到值，每个家族都会是空列表。

    2026-08-22 那次前端大改就挪过混淆变量名（见 ANALYSIS.md §12.5），
    正则里 o/l/s/d/u 是硬编码的，这条最可能在下次重构时踩到。
    """
    js = (
        "let n=e=>{switch(e){case r.lh.v4:return z;case r.lh.v5:return y;}},"
        "z=[{value:r.l1.KE}],y=[{value:r.l1.KE}];function h(e)"
    )

    with pytest.raises(SystemExit) as exc:
        extract_family_samplers(js, SAMPLER_ENUM, FAMILY_ENUM)
    message = str(exc.value)
    assert "为空" in message
    assert "v4" in message and "v5" in message


def test_aborts_when_single_family_table_is_empty():
    """只要有一个家族拿不到采样器就中止——该家族下的模型会被全部拦成 400。"""
    js = (
        "let n=e=>{switch(e){case r.lh.v4:return o;case r.lh.v5:return s;}},"
        "o=[{value:r.l1.KE}],l=[{value:r.l1.KE}];"
        "function h(e)"
    )

    with pytest.raises(SystemExit) as exc:
        extract_family_samplers(js, SAMPLER_ENUM, FAMILY_ENUM)
    assert "v5" in str(exc.value)


def test_missing_table_anchors_still_abort():
    """原有的锚点守卫不能被新守卫顶掉。"""
    with pytest.raises(SystemExit):
        extract_family_samplers("no anchors here", SAMPLER_ENUM, FAMILY_ENUM)
