"""SQLAlchemy models for the FP&A governance store (Postgres schema ``fpa_governance``).

These models are the source for ``alembic revision --autogenerate``. Triggers and
functions are not expressible here; they live in the migrations.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer,
    MetaData, Numeric, SmallInteger, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "fpa_governance"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


NOW = text("now()")
GEN_UUID = text("gen_random_uuid()")


def fk(target: str, **kwargs: Any) -> ForeignKey:
    return ForeignKey(f"{SCHEMA}.{target}", **kwargs)


# --- 001 identity and access -------------------------------------------------

class AppUser(Base):
    __tablename__ = "app_user"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Service and agent identities are users too, for attribution; they are
    # not people, and the variance report close guard asks exactly this.
    is_human: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    # 017: PENDING -> ACTIVE | REJECTED, ACTIVE <-> DISABLED; only a superadmin
    # moves it. ``active`` follows it.
    status: Mapped[str] = mapped_column(Text, server_default=text("'ACTIVE'"))
    password_hash: Mapped[str | None] = mapped_column(Text)
    status_changed_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    status_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status_note: Mapped[str | None] = mapped_column(Text)


class AskHistory(Base):
    """A question someone asked and the answer they got; theirs alone (migration 024)."""

    __tablename__ = "ask_history"
    __table_args__ = (
        CheckConstraint("btrim(question) <> ''", name="question_not_empty"),
        Index("ask_history_user_idx", "user_id", text("created_at DESC")),
    )

    ask_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text, fk("app_user.user_id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    # The scope the answer was computed under
    companies: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    response: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class UserSession(Base):
    """A token issued at signup or sign-in, kept as its sha256 only."""

    __tablename__ = "user_session"

    session_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    user_id: Mapped[str] = mapped_column(Text, fk("app_user.user_id", ondelete="CASCADE"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Role(Base):
    __tablename__ = "role"

    role_code: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text)


class UserRole(Base):
    __tablename__ = "user_role"

    user_id: Mapped[str] = mapped_column(Text, fk("app_user.user_id"), primary_key=True)
    role_code: Mapped[str] = mapped_column(Text, fk("role.role_code"), primary_key=True)


class UserCompanyScope(Base):
    """The entities a user may read. No rows means no scope: reads return nothing."""

    __tablename__ = "user_company_scope"

    user_id: Mapped[str] = mapped_column(Text, fk("app_user.user_id", ondelete="CASCADE"), primary_key=True)
    company_code: Mapped[str] = mapped_column(Text, fk("dim_company.company_code"), primary_key=True)


# --- 002 reference dimensions ------------------------------------------------

class DimCompany(Base):
    __tablename__ = "dim_company"
    __table_args__ = (
        CheckConstraint("country_code = upper(country_code)", name="country_code_upper"),
        CheckConstraint("functional_currency = upper(functional_currency)", name="functional_currency_upper"),
    )

    company_code: Mapped[str] = mapped_column(Text, primary_key=True)
    company_name: Mapped[str] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(CHAR(2))
    region: Mapped[str] = mapped_column(Text)
    functional_currency: Mapped[str] = mapped_column(CHAR(3))


class DimAccount(Base):
    __tablename__ = "dim_account"
    __table_args__ = (
        CheckConstraint("account_type IN ('Revenue', 'COGS', 'OpEx')", name="account_type"),
        CheckConstraint("engine_tag IN ('Services', 'Recurring', 'Shared')", name="engine_tag"),
    )

    account_code: Mapped[str] = mapped_column(Text, primary_key=True)
    account_name: Mapped[str] = mapped_column(Text)
    account_type: Mapped[str] = mapped_column(Text)
    engine_tag: Mapped[str] = mapped_column(Text)


class DimCostCenter(Base):
    __tablename__ = "dim_cost_center"

    cost_center_code: Mapped[str] = mapped_column(Text, primary_key=True)
    country_code: Mapped[str] = mapped_column(CHAR(2))
    practice_code: Mapped[str] = mapped_column(Text)


class LedgerVintage(Base):
    __tablename__ = "ledger_vintage"
    __table_args__ = (CheckConstraint("vintage > 0", name="vintage_positive"),)

    vintage: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    closed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text)


# --- 003 planning model registry ---------------------------------------------

class PlanningModel(Base):
    __tablename__ = "planning_model"
    __table_args__ = (CheckConstraint("plan_year BETWEEN 2000 AND 2200", name="plan_year_range"),)

    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    model_code: Mapped[str] = mapped_column(Text, unique=True)
    model_name: Mapped[str] = mapped_column(Text)
    plan_year: Mapped[int] = mapped_column(Integer)
    reporting_currency: Mapped[str] = mapped_column(CHAR(3))
    calc_order_dag: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class PlanningDimension(Base):
    __tablename__ = "planning_dimension"
    __table_args__ = (
        UniqueConstraint("model_id", "ordinal", name="uq_planning_dimension_model_ordinal"),
        CheckConstraint("ordinal > 0", name="ordinal_positive"),
    )

    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("planning_model.model_id", ondelete="CASCADE"), primary_key=True)
    dimension_code: Mapped[str] = mapped_column(Text, primary_key=True)
    ordinal: Mapped[int] = mapped_column(SmallInteger)


class PlanningMeasure(Base):
    __tablename__ = "planning_measure"
    __table_args__ = (
        CheckConstraint("aggregation_type IN ('additive', 'ratio', 'semi_additive')", name="aggregation_type"),
    )

    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("planning_model.model_id", ondelete="CASCADE"), primary_key=True)
    measure_code: Mapped[str] = mapped_column(Text, primary_key=True)
    aggregation_type: Mapped[str] = mapped_column(Text)
    sql_expression: Mapped[str] = mapped_column(Text)
    available: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class PlanDriver(Base):
    __tablename__ = "plan_driver"
    __table_args__ = (
        UniqueConstraint("model_id", "driver_code", "effective_from", name="uq_plan_driver_code_effective_from"),
        CheckConstraint("value_type IN ('numeric', 'percentage', 'currency', 'count')", name="value_type"),
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'RETIRED')", name="status"),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="effective_range"),
        Index("driver_effective_idx", "model_id", "driver_code", "effective_from", "effective_to"),
    )

    driver_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("planning_model.model_id"))
    driver_code: Mapped[str] = mapped_column(Text)
    driver_name: Mapped[str] = mapped_column(Text)
    formula: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[dt.date] = mapped_column(Date)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    unit: Mapped[str] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'ACTIVE'"))
    created_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


# --- 004 plan versions and scenarios -----------------------------------------

class PlanStateTransition(Base):
    __tablename__ = "plan_state_transition"
    __table_args__ = (
        UniqueConstraint("from_state", "to_state", "role_code", name="uq_plan_state_transition_rule"),
    )

    transition_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_state: Mapped[str] = mapped_column(Text)
    to_state: Mapped[str] = mapped_column(Text)
    role_code: Mapped[str] = mapped_column(Text, fk("role.role_code"))


class PlanVersion(Base):
    __tablename__ = "plan_version"
    __table_args__ = (
        CheckConstraint("plan_year BETWEEN 2000 AND 2200", name="plan_year_range"),
        CheckConstraint("state IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'LOCKED', 'SUPERSEDED', 'REJECTED')", name="state"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("approved_by IS NULL OR approved_by <> requested_by", name="no_self_approval"),
        CheckConstraint("state NOT IN ('APPROVED', 'LOCKED') OR covenant_ok", name="covenant_before_approval"),
        CheckConstraint("state NOT IN ('APPROVED', 'LOCKED') OR approved_by IS NOT NULL", name="approver_required"),
    )

    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    plan_version_code: Mapped[str] = mapped_column(Text, unique=True)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("planning_model.model_id"))
    plan_year: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text, server_default=text("'DRAFT'"))
    covenant_ok: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    covenant_note: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    approved_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    supersedes_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    # Bumped by trigger on every update. A writer names the version it read,
    # and is told it lost if the row has moved on (migration 011).
    row_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class ScenarioSet(Base):
    __tablename__ = "scenario_set"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "scenario_code", name="uq_scenario_set_plan_scenario"),
        CheckConstraint("scenario_code ~ '^[a-z][a-z0-9_]*$'", name="scenario_code_format"),
        CheckConstraint("state IN ('DRAFT', 'APPROVED', 'LOCKED')", name="state"),
        Index("one_base_scenario_per_plan", "plan_version_id", unique=True, postgresql_where=text("is_base")),
    )

    scenario_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id", ondelete="CASCADE"))
    scenario_code: Mapped[str] = mapped_column(Text)
    scenario_name: Mapped[str] = mapped_column(Text)
    is_base: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'DRAFT'"))


class ScenarioDriverOverride(Base):
    __tablename__ = "scenario_driver_override"

    scenario_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("scenario_set.scenario_set_id", ondelete="CASCADE"), primary_key=True)
    driver_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_driver.driver_id"), primary_key=True)
    override_value: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    rationale: Mapped[str] = mapped_column(Text)


class PlanFxRate(Base):
    __tablename__ = "plan_fx_rate"
    __table_args__ = (
        CheckConstraint("period_month = date_trunc('month', period_month)::date", name="period_month_first_day"),
        CheckConstraint("rate > 0", name="rate_positive"),
    )

    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id", ondelete="CASCADE"), primary_key=True)
    period_month: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    from_currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    to_currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True, server_default=text("'USD'"))
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8))


class PlanVersionLine(Base):
    __tablename__ = "plan_version_line"
    __table_args__ = (
        UniqueConstraint(
            "plan_version_id", "scenario_code", "company_code", "period_month", "account_code", "dim_signature_hash",
            name="uq_plan_version_line_grain",
        ),
        CheckConstraint("period_month = date_trunc('month', period_month)::date", name="period_month_first_day"),
        CheckConstraint("dim_signature_hash ~ '^[0-9a-f]{16}$'", name="dim_signature_hash_format"),
        CheckConstraint("jsonb_typeof(driver_derivation_trace) = 'object'", name="derivation_trace_object"),
        CheckConstraint("driver_derivation_trace <> '{}'::jsonb", name="derivation_trace_not_empty"),
        # 016: a line must name its driver, formula and inputs.
        CheckConstraint(
            "driver_derivation_trace ?& array['driver', 'formula', 'inputs'] "
            "AND coalesce(driver_derivation_trace->>'driver', '') <> '' "
            "AND coalesce(driver_derivation_trace->>'formula', '') <> '' "
            "AND jsonb_typeof(driver_derivation_trace->'inputs') = 'object'",
            name="derivation_trace_explains",
        ),
        CheckConstraint("amount_functional = round(quantity * unit_price, 2)", name="amount_equals_quantity_x_price"),
        Index("plan_line_lookup_idx", "plan_version_id", "scenario_code", "period_month", "company_code", "account_code"),
    )

    plan_line_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id", ondelete="CASCADE"))
    scenario_code: Mapped[str] = mapped_column(Text)
    company_code: Mapped[str] = mapped_column(Text, fk("dim_company.company_code"))
    period_month: Mapped[dt.date] = mapped_column(Date)
    account_code: Mapped[str] = mapped_column(Text, fk("dim_account.account_code"))
    dim_signature_hash: Mapped[str] = mapped_column(CHAR(16))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    amount_functional: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    functional_currency: Mapped[str] = mapped_column(CHAR(3))
    driver_derivation_trace: Mapped[Any] = mapped_column(JSONB)
    source_revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


# --- 005 plan approval -------------------------------------------------------

class PlanApproval(Base):
    __tablename__ = "plan_approval"
    __table_args__ = (
        CheckConstraint("decision IN ('PENDING', 'APPROVED', 'REJECTED')", name="decision"),
        CheckConstraint("decided_by IS NULL OR decided_by <> requested_by", name="no_self_approval"),
        CheckConstraint(
            "(decision = 'PENDING' AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(decision <> 'PENDING' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decision_complete",
        ),
    )

    approval_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    requested_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    decided_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    decision: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'"))
    covenant_ok: Mapped[bool] = mapped_column(Boolean)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# --- 006 variance reporting --------------------------------------------------

class VarianceReport(Base):
    """A persisted bridge: what it ran on, what it ran over, and what it found.

    OPEN -> INVESTIGATING (an agent may do this) -> REVIEWED -> CLOSED (only a
    human, enforced by trigger). A gap over the materiality threshold is born
    ESCALATED and cannot be downgraded.
    """

    __tablename__ = "variance_report"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'INVESTIGATING', 'ESCALATED', 'REVIEWED', 'CLOSED')", name="status"),
        CheckConstraint("status <> 'CLOSED' OR (closed_by IS NOT NULL AND closed_at IS NOT NULL)", name="closed_requires_closer"),
    )

    variance_report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    scenario_code: Mapped[str] = mapped_column(Text)
    as_of_vintage: Mapped[int | None] = mapped_column(Integer, fk("ledger_vintage.vintage"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'OPEN'"))
    created_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    closed_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    dsl: Mapped[str | None] = mapped_column(Text)
    measure: Mapped[str | None] = mapped_column(Text)
    rollup: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    convention: Mapped[str] = mapped_column(Text, server_default=text("'volume_first'"))
    report_currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'USD'"))
    vintage_closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    line_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_gap: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    materiality_threshold: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    ties: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    status_changed_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))


class VarianceReportLine(Base):
    """One node of the rollup. ``path`` is its position; level 0 is the total."""

    __tablename__ = "variance_report_line"
    __table_args__ = (
        CheckConstraint("line_no > 0", name="line_no_positive"),
        CheckConstraint("jsonb_typeof(dimension_key) = 'object'", name="dimension_key_object"),
        CheckConstraint("abs(residual) < tolerance", name="ties"),
        CheckConstraint(
            "round(actual_amount - plan_amount, 2) = round(price_variance + volume_variance + mix_variance + "
            "fx_variance + rate_variance + efficiency_variance + residual, 2)",
            name="legs_tie_to_gap",
        ),
    )

    variance_report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("variance_report.variance_report_id", ondelete="CASCADE"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    dimension_key: Mapped[Any] = mapped_column(JSONB)
    plan_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    actual_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    price_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    volume_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    mix_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    fx_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    rate_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    efficiency_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    residual: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    level: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    path: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    line_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    tolerance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("1"))
    mix_between_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))


class VarianceReportCitation(Base):
    """The cube rows behind a leaf line. A node cites its leaves' rows."""

    __tablename__ = "variance_report_citation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["variance_report_id", "line_no"],
            [f"{SCHEMA}.variance_report_line.variance_report_id", f"{SCHEMA}.variance_report_line.line_no"],
            name="fk_variance_report_citation_line", ondelete="CASCADE",
        ),
    )

    variance_report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    company_code: Mapped[str] = mapped_column(Text, primary_key=True)
    period_month: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    account_code: Mapped[str] = mapped_column(Text, primary_key=True)
    dim_signature_hash: Mapped[str] = mapped_column(CHAR(16), primary_key=True)
    plan_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    actual_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    # 016: what the row contributes to each leg is computed from these.
    plan_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    actual_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    plan_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    actual_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    plan_fx: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    actual_fx: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    account_type: Mapped[str | None] = mapped_column(Text)


# --- 007 audit and disclosure ------------------------------------------------

class LlmDisclosureLog(Base):
    __tablename__ = "llm_disclosure_log"

    disclosure_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    user_id: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    scope: Mapped[Any] = mapped_column(JSONB)
    field_classes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    model_name: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(CHAR(64))
    response_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    disclosed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("audit_entity_idx", "entity_type", "entity_id", "occurred_at"),)

    audit_event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    actor_user_id: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB)
    # All three are computed by the audit_event_chain trigger (migration 011):
    # event_key is the row's logical identity, event_hash chains it to the
    # row before. Application code inserts the facts above and nothing else.
    previous_hash: Mapped[str | None] = mapped_column(CHAR(64))
    event_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    event_key: Mapped[str] = mapped_column(CHAR(64), unique=True)


# --- 008 durable recompute ---------------------------------------------------

class PlanDriverBinding(Base):
    """Which plan lines a driver moves, and how hard.

    ``planning_model.calc_order_dag`` says how drivers depend on each other; it
    does not say which plan lines they reach. A binding closes that gap: moving
    this driver scales ``target`` on lines posting to ``account_code``, damped
    by ``elasticity`` (1.0 is proportional, 0.5 is half the move). The recompute
    engine reads nothing else, which is what keeps it auditable.
    """

    __tablename__ = "plan_driver_binding"
    __table_args__ = (
        UniqueConstraint("driver_id", "account_code", "target", name="uq_plan_driver_binding_driver_id"),
        CheckConstraint("target IN ('quantity', 'unit_price')", name="target"),
        CheckConstraint("elasticity >= 0", name="elasticity_non_negative"),
    )

    binding_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    driver_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_driver.driver_id", ondelete="CASCADE"))
    account_code: Mapped[str] = mapped_column(Text, fk("dim_account.account_code"))
    target: Mapped[str] = mapped_column(Text)
    elasticity: Mapped[Decimal] = mapped_column(Numeric(10, 6), server_default=text("1"))


class RecomputeRun(Base):
    """The durable record of one Temporal re-forecast execution.

    The workflow's own progress query is the live truth while a run is going;
    this table is what survives retention, lists past runs for the UI and gives
    a rejection or a cancellation somewhere permanent to land.
    """

    __tablename__ = "recompute_run"
    __table_args__ = (
        CheckConstraint(
            "state IN ('RUNNING', 'AWAITING_APPROVAL', 'PUBLISHING', 'COMPLETED', "
            "'REJECTED', 'EXPIRED', 'CANCELLED', 'COMPENSATED', 'FAILED', 'AWAITING_SUBMISSION', 'AWAITING_LOCK')",
            name="state",
        ),
        CheckConstraint("processed_rows >= 0 AND dirty_rows >= 0", name="counts_non_negative"),
        Index("recompute_run_plan_idx", "plan_version_id", "started_at"),
        Index("recompute_run_workflow_idx", "workflow_id", "started_at"),
    )

    # The run's first_execution_run_id: one row per re-forecast, stable across
    # continue-as-new. Not the workflow id, which every re-forecast of a plan
    # version shares (see migration 009).
    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(Text)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    requested_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    shocks: Mapped[Any] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text, server_default=text("'RUNNING'"))
    phase: Mapped[str] = mapped_column(Text, server_default=text("'STARTING'"))
    dirty_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    processed_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    decided_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PlanPublication(Base):
    """One row per attempt to put a revision of a plan version into the cube.

    ``revision`` is allocated here, under a unique constraint, so two runs can
    never claim the same one. It is also the compensation ledger: the row says
    whether the cube and the commitment ledger were left agreeing, and the
    workflow does not finish until it does.
    """

    __tablename__ = "plan_publication"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "revision", name="uq_plan_publication_plan_version_id"),
        # The idempotence guarantee, held by the database rather than by the
        # workflow: the same re-forecast asked for twice reuses one revision.
        UniqueConstraint("plan_version_id", "idempotency_key", name="uq_plan_publication_idempotency"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(
            "state IN ('RESERVED', 'PUBLISHED', 'COMMITTED', 'SUPERSEDED', 'COMPENSATED', 'COMPENSATION_FAILED')",
            name="state",
        ),
    )

    publication_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    revision: Mapped[int] = mapped_column(Integer)
    workflow_id: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'RESERVED'"))
    row_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    idempotency_key: Mapped[str] = mapped_column(Text)
    commitment_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    # The whole cumulative shock set this revision applied, [[driver, from, to], ...].
    # The next re-forecast starts from the latest COMMITTED one (migration 012).
    shocks: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    settled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# --- 013 agent proposals -------------------------------------------------------

class AgentProposal(Base):
    """An Agno run paused on a driver proposal, and the human decision on it.

    Created by migration 013 in raw SQL, including its guard trigger; this
    model exists so ``alembic check`` and ``--autogenerate`` see the table
    instead of proposing to drop it. Constraint names match what Postgres
    generated for the unnamed constraints in that migration.
    """

    __tablename__ = "agent_proposal"
    __table_args__ = (
        UniqueConstraint("run_id", name="agent_proposal_run_id_key"),
        CheckConstraint("state IN ('PENDING','APPROVED','REJECTED')", name="agent_proposal_state_check"),
        CheckConstraint("decided_by IS NULL OR decided_by <> requested_by", name="agent_proposal_check"),
        CheckConstraint("(state = 'PENDING') = (decided_by IS NULL)", name="agent_proposal_check1"),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    run_id: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    decided_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'"))
    provider: Mapped[str] = mapped_column(Text)
    scope: Mapped[Any] = mapped_column(JSONB)
    drafts: Mapped[Any] = mapped_column(JSONB)
    paused_run: Mapped[Any] = mapped_column(JSONB)
    resumed_run: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# --- 015 question-driven re-forecast and the automated covenant check -----------

REQUEST_STATES = (
    "PROPOSED", "CONTROLLER_REJECTED", "RUNNING", "COVENANT_FAILED", "AWAITING_CFO", "PUBLISHING",
    "PUBLISHED", "CFO_REJECTED", "EXPIRED", "CANCELLED", "COMPENSATED", "FAILED",
    "AWAITING_SUBMISSION", "AWAITING_CONTROLLER",
)


class ReforecastRequest(Base):
    """A re-forecast a planner asked for in words, drafted by the agent team.

    The request is the controller's to start: approving it starts the durable
    workflow. From then on its state only mirrors the run (trigger in 015), so
    nothing but the workflow can say it covenant-failed or published.
    """

    __tablename__ = "reforecast_request"
    __table_args__ = (
        CheckConstraint("state IN (" + ", ".join(f"'{s}'" for s in REQUEST_STATES) + ")", name="state"),
        CheckConstraint("from_value > 0 AND to_value > 0", name="values_positive"),
        CheckConstraint("controller_decided_by IS NULL OR controller_decided_by <> requested_by", name="no_self_decision"),
        Index("reforecast_request_state_idx", "state", "created_at"),
        UniqueConstraint("run_id", name="uq_reforecast_request_run_id"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    source_plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    driver_code: Mapped[str] = mapped_column(Text)
    from_value: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    to_value: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    # Empty arrays mean every company / every month of the plan.
    companies: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    period_months: Mapped[list[dt.date]] = mapped_column(ARRAY(Date), server_default=text("'{}'"))
    scope_label: Mapped[str] = mapped_column(Text, server_default=text("''"))
    question: Mapped[str] = mapped_column(Text)
    # What the planner was shown: the executed DSL, a sample of rows, the
    # narrative, and which team member produced the draft.
    evidence: Mapped[Any] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    requested_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'PROPOSED'"))
    controller_decided_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    controller_decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    controller_comment: Mapped[str | None] = mapped_column(Text)
    # The run this request started (recompute_run.run_id).
    run_id: Mapped[str | None] = mapped_column(Text)
    outcome_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class CovenantRule(Base):
    """One covenant the recomputed lines must meet. Data, not code: controller-only writes."""

    __tablename__ = "covenant_rule"
    __table_args__ = (
        CheckConstraint("metric IN ('services_revenue', 'delivery_cost', 'gross_margin_pct')", name="metric"),
        CheckConstraint("measure IN ('LEVEL', 'CHANGE_PCT')", name="measure"),
        CheckConstraint("comparator IN ('>=', '<=')", name="comparator"),
        CheckConstraint("scope IN ('REQUEST', 'PLAN')", name="scope"),
    )

    rule_code: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    metric: Mapped[str] = mapped_column(Text)
    # LEVEL: the recomputed value itself. CHANGE_PCT: percent change against
    # the plan currently in the cube.
    measure: Mapped[str] = mapped_column(Text)
    comparator: Mapped[str] = mapped_column(Text)
    threshold: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    # REQUEST: only the companies and months the request changes. PLAN: all of it.
    scope: Mapped[str] = mapped_column(Text, server_default=text("'REQUEST'"))
    scenarios: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{base,stretch,downside}'"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class CovenantCheck(Base):
    """One rule evaluated for one scenario of one run. Append-only (trigger in 015)."""

    __tablename__ = "covenant_check"
    __table_args__ = (
        UniqueConstraint("run_id", "revision", "rule_code", "scenario_code", name="uq_covenant_check_run_id"),
        Index("covenant_check_version_idx", "plan_version_id"),
    )

    check_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text)
    workflow_id: Mapped[str] = mapped_column(Text)
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), fk("reforecast_request.request_id"))
    # The successor version the recomputed lines belong to.
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    revision: Mapped[int] = mapped_column(Integer)
    rule_code: Mapped[str] = mapped_column(Text, fk("covenant_rule.rule_code"))
    scenario_code: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    metric: Mapped[str] = mapped_column(Text)
    measure: Mapped[str] = mapped_column(Text)
    before_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    after_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    measured_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    comparator: Mapped[str] = mapped_column(Text)
    threshold: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    passed: Mapped[bool] = mapped_column(Boolean)
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
