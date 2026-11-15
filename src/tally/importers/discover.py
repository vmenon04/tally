"""How Discover words card rows, so a merchant reads the same each time."""

import re

ACCOUNT = "discover"

# Glued onto Apple Pay purchases with no space, sometimes followed by extra digits.
_APPLE_PAY = re.compile(r"APPLE PAY ENDING IN\s*\S*$")
# Some processors glue a reference code onto the state: "SPRINGFIELD IL00012345ABCD...".
_REFERENCE = re.compile(r" ([A-Z]{2})\d[A-Z0-9]{5,}$")


def clean_merchant(description: str) -> str:
    """Remove the parts of a description that change from row to row at the same merchant."""
    merchant = _APPLE_PAY.sub("", description.strip())
    merchant = _REFERENCE.sub(r" \1", merchant)
    return " ".join(merchant.split())
