class DSLValidationError(ValueError):
    """The DSL is syntactically valid but violates the schema or semantics."""


class ParseError(ValueError):
    """The input does not match the FinOpsExpr query grammar."""

