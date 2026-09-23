from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .errors import DSLValidationError, ParseError
from .lexer import Token, tokenize
from .schema import Schema


@dataclass(frozen=True)
class Number:
    value: Decimal


@dataclass(frozen=True)
class Reference:
    name: str


@dataclass(frozen=True)
class Unary:
    operator: str
    operand: "FormulaNode"


@dataclass(frozen=True)
class Binary:
    operator: str
    left: "FormulaNode"
    right: "FormulaNode"


@dataclass(frozen=True)
class Function:
    name: str
    arguments: tuple["FormulaNode", ...]


FormulaNode = Number | Reference | Unary | Binary | Function


class FormulaParser:
    def __init__(self, source: str):
        self.tokens = tokenize(source)
        self.index = 0

    @property
    def token(self) -> Token:
        return self.tokens[self.index]

    def accept(self, value: str) -> bool:
        if self.token.value.upper() == value.upper():
            self.index += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise ParseError(f"expected {value!r} at position {self.token.position}")

    def parse(self) -> FormulaNode:
        # Parse the complete expression and reject trailing tokens so a valid
        # prefix cannot hide an unsafe suffix.
        node = self.parse_expr()
        if self.token.kind != "EOF":
            raise ParseError(f"unexpected token {self.token.value!r} at position {self.token.position}")
        return node

    def parse_expr(self) -> FormulaNode:
        # Addition/subtraction form the lowest-precedence binary layer.
        node = self.parse_term()
        while self.token.value in {"+", "-"}:
            operator = self.token.value
            self.index += 1
            node = Binary(operator, node, self.parse_term())
        return node

    def parse_term(self) -> FormulaNode:
        # Multiplication/division bind more tightly than addition/subtraction.
        node = self.parse_factor()
        while self.token.value in {"*", "/"}:
            operator = self.token.value
            self.index += 1
            node = Binary(operator, node, self.parse_factor())
        return node

    def parse_factor(self) -> FormulaNode:
        # Exponentiation is right-associative through recursive parsing.
        node = self.parse_unary()
        if self.accept("^"):
            node = Binary("^", node, self.parse_factor())
        return node

    def parse_unary(self) -> FormulaNode:
        # Unary negation is represented explicitly instead of folded into a
        # numeric literal, preserving formula intent for validation.
        if self.accept("-"):
            return Unary("-", self.parse_primary())
        return self.parse_primary()

    def parse_primary(self) -> FormulaNode:
        # Primary nodes are numbers, grouped expressions, function calls, or
        # references; all later validation operates on this typed tree.
        if self.token.kind == "NUMBER":
            value = Decimal(self.token.value)
            self.index += 1
            return Number(value)
        if self.accept("("):
            node = self.parse_expr()
            self.expect(")")
            return node
        if self.token.kind == "IDENT":
            name = self.token.value
            self.index += 1
            if self.accept("("):
                args: list[FormulaNode] = []
                if not self.accept(")"):
                    args.append(self.parse_expr())
                    while self.accept(","):
                        args.append(self.parse_expr())
                    self.expect(")")
                return Function(name.upper(), tuple(args))
            return Reference(name)
        raise ParseError(f"expected formula operand at position {self.token.position}")


def parse_formula(source: str) -> FormulaNode:
    return FormulaParser(source).parse()


ALLOWED_FUNCTIONS = {
    "PRIOR", "LEAD", "YOY", "CAGR", "YTD", "QTD", "MTD", "ROLLING",
    "BY", "WHERE", "SUM", "AVG", "MIN", "MAX", "ABS", "ROUND",
}

FUNCTION_ARITY = {
    "PRIOR": (1, 2), "LEAD": (1, 2), "YOY": (1, 1), "CAGR": (1, 2),
    "YTD": (1, 1), "QTD": (1, 1), "MTD": (1, 1), "ROLLING": (1, 2),
    "BY": (1, 2), "WHERE": (1, 2), "SUM": (1, 1), "AVG": (1, 1),
    "MIN": (1, 1), "MAX": (1, 1), "ABS": (1, 1), "ROUND": (1, 2),
}


def referenced_names(node: FormulaNode) -> set[str]:
    # Resolve dependencies recursively for validation and cycle detection.
    if isinstance(node, Reference):
        return {node.name}
    if isinstance(node, Number):
        return set()
    if isinstance(node, Unary):
        return referenced_names(node.operand)
    if isinstance(node, Binary):
        return referenced_names(node.left) | referenced_names(node.right)
    names: set[str] = set()
    for argument in node.arguments:
        names |= referenced_names(argument)
    return names


# A reference inside one of these reads a *different period's* value of the
# name, so it is not an edge in the same-period dependency graph. A growth
# rate off last year's number is a legitimate model; a driver that needs its
# own current value to compute its own current value is a broken graph. This
# set is the whole difference between those two, and it is why PRIOR is an
# operator rather than an offset argument.
TIME_SHIFTED_FUNCTIONS = {"PRIOR", "LEAD"}


def dependency_names(node: FormulaNode) -> set[str]:
    """Names this formula needs *in the same period*: the cycle-detection edges."""
    if isinstance(node, Reference):
        return {node.name}
    if isinstance(node, Number):
        return set()
    if isinstance(node, Unary):
        return dependency_names(node.operand)
    if isinstance(node, Binary):
        return dependency_names(node.left) | dependency_names(node.right)
    if node.name in TIME_SHIFTED_FUNCTIONS:
        # The first argument is read from another period; a numeric offset
        # references nothing.
        return set().union(*(dependency_names(a) for a in node.arguments[1:]))
    return set().union(*(dependency_names(a) for a in node.arguments)) if node.arguments else set()


def validate_formula(node: FormulaNode, schema: Schema) -> None:
    def walk(current: FormulaNode) -> None:
        # Every reference must resolve to a registered metric or driver before
        # a proposed formula can be considered safe.
        if isinstance(current, Reference):
            if current.name in schema.metric_names:
                schema.require_metric(current.name)
            elif current.name not in schema.driver_names:
                raise DSLValidationError(f"unknown formula reference: {current.name}")
            return
        if isinstance(current, Unary):
            walk(current.operand)
            return
        if isinstance(current, Binary):
            walk(current.left)
            walk(current.right)
            return
        if isinstance(current, Function):
            if current.name not in ALLOWED_FUNCTIONS:
                raise DSLValidationError(f"unknown formula function: {current.name}")
            minimum, maximum = FUNCTION_ARITY[current.name]
            if not minimum <= len(current.arguments) <= maximum:
                expected = str(minimum) if minimum == maximum else f"{minimum} or {maximum}"
                raise DSLValidationError(
                    f"{current.name} expects {expected} argument(s), got {len(current.arguments)}"
                )
            if current.name in {"SUM", "AVG"}:
                for argument in current.arguments:
                    if isinstance(argument, Reference) and schema.metrics.get(argument.name, {}).get("kind") == "ratio":
                        raise DSLValidationError(f"{current.name} cannot aggregate ratio measure: {argument.name}")
            for argument in current.arguments:
                walk(argument)

    walk(node)


def detect_cycles(formulas: dict[str, FormulaNode]) -> None:
    """Reject a same-period cycle; a PRIOR/LEAD self-reference is not one."""
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        # DFS distinguishes the active recursion stack from completed nodes,
        # allowing shared dependencies while rejecting only real cycles.
        if name in visiting:
            cycle = " -> ".join(path + [name])
            raise DSLValidationError(f"formula cycle detected: {cycle}")
        if name in visited or name not in formulas:
            return
        visiting.add(name)
        for reference in dependency_names(formulas[name]):
            visit(reference, path + [name])
        visiting.remove(name)
        visited.add(name)

    for name in formulas:
        visit(name, [])
