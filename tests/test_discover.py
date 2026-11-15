import pytest

from tally.importers.discover import clean_merchant


@pytest.mark.parametrize(
    ("description", "merchant"),
    [
        (
            "CORNER GROCER #77 LAKEVIEW ORAPPLE PAY ENDING IN 1234",
            "CORNER GROCER #77 LAKEVIEW OR",
        ),
        (
            "CORNER GROCER #77 LAKEVIEW ORAPPLE PAY ENDING IN 123401786R",
            "CORNER GROCER #77 LAKEVIEW OR",
        ),
        (
            "SQ *BLUE KETTLE SPRINGFIELD ILAPPLE PAY ENDING IN 12340009876543210",
            "SQ *BLUE KETTLE SPRINGFIELD IL",
        ),
        (
            "TST*HARBOR BISTRO SPRINGFIELD IL00012345ABCDEFGH1JKL",
            "TST*HARBOR BISTRO SPRINGFIELD IL",
        ),
        ("CAFE BADGEPAY 555-010-0199 MA", "CAFE BADGEPAY 555-010-0199 MA"),
        ("TACO STAND 729283 555-010-0123 NC", "TACO STAND 729283 555-010-0123 NC"),
        ("INTERNET PAYMENT - THANK YOU", "INTERNET PAYMENT - THANK YOU"),
        ("  EXTRA   SPACES  CA ", "EXTRA SPACES CA"),
    ],
)
def test_clean_merchant(description: str, merchant: str) -> None:
    assert clean_merchant(description) == merchant
