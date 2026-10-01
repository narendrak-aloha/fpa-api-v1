"""Preserve explicit historical intent independently of model-generated DSL.

Only explicit AS OF timestamps and named month closes are resolved. Unsupported
or ambiguous wording requires clarification rather than a guessed current read.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from fpa_project.dsl.compiler import close_month_lookup, vintage_lookup
from fpa_project.dsl.parser import parse_query

MONTHS = ('january february march april may june july august september october november december').split()
HISTORICAL = re.compile(r'\bas\s+of\b|\b(?:at|before)\s+(?:the\s+)?(?:\w+\s+)?close\b', re.I)
MONTH_CLOSE = re.compile(r'\b(' + '|'.join(MONTHS) + r')(?:\s+(\d{4}))?\s+close\b', re.I)
TIMESTAMP = re.compile(r'\bas\s+of\s+[\'\"]?(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)?)(?![\w:+.\-])', re.I)


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class HistoricalConstraint:
    as_of: str
    vintage: int

    @property
    def instruction(self):
        return (f"Verified historical requirement: include AS OF '{self.as_of}' in every evidence query. "
                f"This resolves to ledger vintage {self.vintage}. Do not execute a current-vintage query. "
                "If unable to preserve this constraint, request clarification.")

    def validate(self, dsl):
        query = parse_query(dsl)
        if not query.as_of or instant(query.as_of) != instant(self.as_of):
            raise ValueError(f"Historical request requires AS OF '{self.as_of}'; generated DSL omitted or changed it")


def resolve_historical(request, executor):
    if not HISTORICAL.search(request) and not MONTH_CLOSE.search(request):
        return None
    if re.search(r'\bbefore\b', request, re.I):
        raise ValueError('Specify the historical AS OF timestamp; a close itself is not the instant before it')
    timestamps = list(TIMESTAMP.finditer(request))
    matches = list(MONTH_CLOSE.finditer(request))
    if len(timestamps) + len(matches) > 1 or len(re.findall(r'\bas\s+of\b', request, re.I)) > 1:
        raise ValueError('Historical request names multiple closes; specify one close or AS OF timestamp')
    timestamp = timestamps[0] if timestamps else None
    if timestamp:
        as_of = instant(timestamp.group(1)).replace(tzinfo=None).isoformat(timespec='seconds')
        rows = list(executor(*vintage_lookup(as_of))) if executor else []
    else:
        if len(matches) != 1:
            raise ValueError('Historical close is unclear; specify one close month and year or an AS OF timestamp')
        match = matches[0]
        years = set(re.findall(r'\b(?:19|20)\d{2}\b', request))
        year = match.group(2) or (next(iter(years)) if len(years) == 1 else None)
        if year is None:
            raise ValueError('Historical close year is unclear; specify the close year')
        month = MONTHS.index(match.group(1).lower()) + 1
        rows = list(executor(*close_month_lookup(int(year), month))) if executor else []
        if len(rows) != 1:
            raise ValueError('Historical close could not be uniquely verified; specify its sealed timestamp')
        as_of = instant(rows[0]['closed_at']).replace(tzinfo=None).isoformat(timespec='seconds')
    if not rows:
        raise ValueError('No sealed ledger close exists on or before the requested historical timestamp')
    return HistoricalConstraint(as_of=as_of, vintage=int(rows[0]['vintage']))
