"""Country codes as people read them.

``dim_company.country_code`` is what authorization, the DSL, SQL and the audit
log all use, and none of that changes here. Only the last step changes: text a
person reads says "Poland", not "PL". The two live apart on purpose — a name is
a rendering of a code, never a second identity for it, so nothing downstream
ever looks a name up to decide what a caller may see.
"""

from __future__ import annotations

# The codes dim_company holds, to the name a reader expects. A code missing
# here is shown as itself: an unnamed country is better than a wrong one.
COUNTRY_CODE_NAMES = {
    "AE": "United Arab Emirates",
    "AU": "Australia",
    "CA": "Canada",
    "DE": "Germany",
    "IN": "India",
    "PL": "Poland",
    "SG": "Singapore",
    "UK": "United Kingdom",
    "US": "United States",
}

# What a planner may type, to the code. Built from the names above, plus the
# shorthands people actually use. Lower-cased keys; the caller lower-cases too.
COUNTRY_NAMES = {name.lower(): code for code, name in COUNTRY_CODE_NAMES.items()} | {
    "great britain": "UK", "britain": "UK", "gb": "UK",
    "usa": "US", "america": "US", "uae": "AE",
}


def country_name(code: str) -> str:
    """The readable name for a country code, or the code when there is none."""
    return COUNTRY_CODE_NAMES.get(str(code).strip().upper(), str(code))


def requested_countries(dsl: str) -> set[str]:
    """The countries a query asks for outright, as country codes.

    Only ``geo_country =`` and ``geo_country IN`` name a country the caller is
    asking to see. ``!=`` and ``NOT IN`` exclude one, which is a question about
    everywhere else and needs no authorisation of its own. A query with no
    country predicate asks for whatever the caller's scope holds, so it names
    nothing here.
    """
    from fpa_project.dsl.parser import parse_query

    asked: set[str] = set()
    for predicate in parse_query(dsl).predicates:
        if predicate.field != "geo_country" or predicate.operator.upper() not in {"=", "IN"}:
            continue
        asked.update(str(value.value).strip().upper() for value in predicate.values)
    return asked


def refusal_text(codes: list[str]) -> str:
    """The access-control refusal, worded the same way every time.

    Countries are named, never coded, for the same reason the rest of the
    user-facing text names them.
    """
    names = " and ".join(country_name(code) for code in codes)
    return f"You are not authorized to view the {names} country OUT of SCOPE"
