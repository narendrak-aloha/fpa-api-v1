from __future__ import annotations

from dataclasses import dataclass
import re

from .errors import ParseError


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    position: int


TOKEN_RE = re.compile(
    r"(?P<WS>\s+)"
    r"|(?P<STRING>'(?:''|[^'])*')"
    r"|(?P<DOTDOT>\.\.)"
    r"|(?P<OP>!=|>=|<=|[=><+\-*/^(),])"
    r"|(?P<PERIOD_MONTH>(?:(?<=-)0[1-9]|(?<=-)1[0-2]))"
    r"|(?P<NUMBER>(?:0|[1-9]\d*)(?:\.\d+)?)"
    r"|(?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)"
)


def tokenize(source: str) -> list[Token]:
    # Tokenization is deliberately structural: it recognizes literals and
    # operators but never evaluates or interpolates user-provided values.
    tokens: list[Token] = []
    pos = 0
    while pos < len(source):
        match = TOKEN_RE.match(source, pos)
        if not match:
            raise ParseError(f"unexpected character at position {pos}: {source[pos]!r}")
        kind = match.lastgroup
        value = match.group()
        if kind != "WS":
            tokens.append(Token(kind, value, pos))
        pos = match.end()
    tokens.append(Token("EOF", "", len(source)))
    return tokens
