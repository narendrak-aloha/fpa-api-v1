from decimal import Decimal as D

import pytest

from fpa_project.bridge_service import _attach_contributions
from fpa_project.dsl.bridge import BridgeLine, decompose
from fpa_project.dsl.compiler import SecurityContext, compile_query
from fpa_project.dsl.parser import parse_query
from fpa_project.dsl.schema import Schema
from fpa_project.query_calculations import extract_ratio_calculations, ratio_projection


def row(pq='10', aq='12', pp='100', ap='80', kind='Revenue', convention='volume_first'):
    return dict(plan_quantity=D(pq), actual_quantity=D(aq), plan_unit_price=D(pp), actual_unit_price=D(ap),
                plan_fx=D('0.25'), actual_fx=D('0.24'), actual_amount=D(aq)*D(ap), plan_amount=D(pq)*D(pp),
                account_type=kind, convention=convention)


@pytest.mark.parametrize('leg', ['price', 'volume', 'mix', 'fx', 'rate', 'efficiency'])
def test_explanation_result_matches_the_decomposition(leg):
    rows = [row(), row(pq='5', aq='3', pp='200', ap='210')]
    _attach_contributions(rows, leg, margin=False)
    lines = [BridgeLine(path=(), plan_quantity=r['plan_quantity'], actual_quantity=r['actual_quantity'],
                        plan_unit_price=r['plan_unit_price'], actual_unit_price=r['actual_unit_price'],
                        plan_fx=r['plan_fx'], actual_fx=r['actual_fx']) for r in rows]
    expected = getattr(decompose(lines).root, leg)
    assert abs(sum((r['contribution'] for r in rows), D(0)) - expected) <= D('0.01')
    for r in rows:
        assert r['calculation']['result'] == str(r['contribution'])
        assert r['calculation']['unit'] == 'USD'
        assert r['calculation']['substitution']


def test_volume_uses_all_group_rows_before_pagination():
    rows = [row(), row(pq='10', aq='10', pp='300')]
    _attach_contributions(rows, 'volume', margin=False)
    first_page = rows[:1]
    assert first_page[0]['calculation']['inputs']['Group weighted unit price (USD)'] == '50.00'
    assert first_page[0]['contribution'] == D('100')


def test_price_keeps_precision_hidden_by_table_rounding():
    r = row(aq='60.251437', pp='2371.15', ap='834.73')
    r['plan_fx'] = D('0.2545')
    _attach_contributions([r], 'price', margin=False)
    assert r['calculation']['inputs']['Actual quantity'] == '60.251437'
    assert '60.251437' in r['calculation']['substitution']


def test_price_first_and_margin_cost_sign_are_explicit():
    r = row(kind='COGS', convention='price_first')
    _attach_contributions([r], 'rate', margin=True)
    assert r['contribution'] == D('50')
    assert 'Plan quantity' in r['calculation']['formula']
    assert r['calculation']['substitution'].startswith('−1 ×')
    assert any('margin' in n for n in r['calculation']['notes'])


def test_non_applicable_and_legacy_inputs_are_not_invented():
    r = row(kind='COGS')
    _attach_contributions([r], 'price', margin=False)
    assert r['calculation']['substitution'] == '0'
    r['plan_quantity'] = None
    _attach_contributions([r], 'price', margin=False)
    assert r['calculation'] is None and r['contribution'] is None


def test_zero_planned_quantity_is_documented():
    r = row(pq='0')
    _attach_contributions([r], 'mix', margin=False)
    assert any('zero planned quantity' in n for n in r['calculation']['notes'])


def test_ratio_inputs_use_same_scoped_query_and_keep_original_columns():
    dsl = 'SELECT utilisation AS used, gross_margin_pct BY practice FOR PERIOD 2026-Q2'
    compiled = compile_query(dsl, security_context=SecurityContext(frozenset({'RTPL1'})))
    sql, descriptors = ratio_projection(compiled.sql, parse_query(dsl), Schema())
    assert sql.split(' FROM ', 1)[1] == compiled.sql.split(' FROM ', 1)[1]
    assert compiled.params
    r = dict(practice='Engineering', used=D('0.75'), gross_margin_pct=D('0.2'),
             __calculation_0_numerator=D('75'), __calculation_0_denominator=D('100'),
             __calculation_1_numerator=D('20'), __calculation_1_denominator=D('100'))
    explanations = extract_ratio_calculations(r, descriptors)
    assert set(r) == {'practice', 'used', 'gross_margin_pct'}
    assert explanations[0]['substitution'] == '75 ÷ 100'
    assert explanations[0]['result'] == '0.75'


def test_ratio_zero_denominator_and_missing_operands():
    descriptor = dict(metric='utilisation', column='utilisation', keys=['n', 'd'])
    result = extract_ratio_calculations(dict(n=0, d=0, utilisation=None), [descriptor])[0]
    assert result['substitution'] is None and result['result'] is None
    assert any('undefined' in n for n in result['notes'])
    assert extract_ratio_calculations(dict(utilisation=D('0.5')), [descriptor]) == []


def test_window_queries_keep_the_original_query():
    dsl = 'SELECT YOY(utilisation) BY practice FOR PERIOD 2026-Q2'
    compiled = compile_query(dsl)
    assert ratio_projection(compiled.sql, parse_query(dsl), Schema()) == (compiled.sql, [])


def test_tools_keep_original_table_shape_and_store_executed_operands():
    from fpa_project.agent_team.tools import FPATools
    from fpa_project.agent_team.security import UserScope

    statements = []
    def executor(sql, params):
        statements.append((sql, params))
        return [dict(practice='Engineering', utilisation=D('0.75'),
                     __calculation_0_numerator=D('75'), __calculation_0_denominator=D('100'))]

    tools = FPATools(UserScope(user_id='test', allowed_companies=frozenset({'RTPL1'})),
                     executor=executor, include_calculations=True)
    dsl = 'SELECT utilisation BY practice FOR PERIOD 2026-Q2'
    result = tools.run_finops_query(dsl)
    assert result.status == 'SUCCESS'
    assert result.columns == ['practice', 'utilisation']
    assert len(statements) == 1
    assert '__calculation_0_numerator' in statements[0][0]
    assert 'RTPL1' in str(statements[0][1])
    assert tools.calculation_rows[dsl][0]['calculations'][0]['substitution'] == '75 ÷ 100'
