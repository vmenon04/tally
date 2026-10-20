"""Parsers for bank and card exports."""


class ParseError(ValueError):
    """An export file isn't in the layout its parser expects."""

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)
