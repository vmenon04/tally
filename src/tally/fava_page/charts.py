"""Small inline SVG charts for the Tally page: no scripts, nothing fetched, any screen size.

Colors come from CSS classes the page's template defines, so they follow Fava's light and
dark themes.
"""

from collections.abc import Callable, Sequence
from decimal import Decimal
from html import escape

from tally.ledger.picture import Month

type Money = Callable[[Decimal], str]

_W, _H = 640, 220
_PAD_TOP, _PAD_BOTTOM = 26, 40


def months(found: Sequence[Month], money: Money) -> str:
    """Pay and spending side by side for each month, with the share kept underneath."""
    top = max([*(m.pay for m in found), *(m.spent for m in found), Decimal(1)])
    slot = _W / len(found)
    bar = min(slot * 0.3, 40)
    height = _H - _PAD_TOP - _PAD_BOTTOM
    parts = []
    for i, m in enumerate(found):
        x = slot * i + slot / 2
        for j, (value, kind) in enumerate(((m.pay, "pay"), (m.spent, "spent"))):
            h = float(max(value, Decimal(0)) / top) * height
            left = x - bar - 2 if j == 0 else x + 2
            y = _PAD_TOP + height - h
            parts.append(
                f'<rect class="t-{kind}" x="{left:.1f}" y="{y:.1f}" width="{bar:.1f}" '
                f'height="{h:.1f}" rx="3"><title>{escape(m.name)} {kind}: '
                f"{escape(money(value))}</title></rect>"
            )
        rate = "" if m.rate is None else f"{m.rate * 100:.0f}% kept"
        parts.append(
            f'<text class="t-label" x="{x:.1f}" y="{_H - 22}" text-anchor="middle">'
            f"{escape(m.name)}</text>"
            f'<text class="t-small" x="{x:.1f}" y="{_H - 6}" text-anchor="middle">{rate}</text>'
        )
    base = _PAD_TOP + height
    return (
        f'<svg class="t-chart" viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-label="Pay and spending by month">'
        f'<line class="t-axis" x1="0" x2="{_W}" y1="{base}" y2="{base}"/>'
        + "".join(parts)
        + "</svg>"
    )


def net_worth(points: Sequence[tuple[object, Decimal]], money: Money) -> str:
    """Net worth at each month's end, as a line with the area under it filled."""
    values = [v for _, v in points]
    low, high = min([*values, Decimal(0)]), max([*values, Decimal(1)])
    span = high - low or Decimal(1)
    height = _H - _PAD_TOP - _PAD_BOTTOM
    step = _W / max(len(points) - 1, 1)

    def y(v: Decimal) -> float:
        return _PAD_TOP + height - float((v - low) / span) * height

    xy = [(i * step, y(v)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{yy:.1f}" for x, yy in xy)
    zero = y(Decimal(0))
    area = f"0,{zero:.1f} {line} {xy[-1][0]:.1f},{zero:.1f}"
    dots = "".join(
        f'<circle class="t-dot" cx="{x:.1f}" cy="{yy:.1f}" r="4"><title>{d:%b %d}: '
        f"{escape(money(v))}</title></circle>"
        for (x, yy), (d, v) in zip(xy, points, strict=True)
    )
    labels = "".join(
        f'<text class="t-label" x="{min(max(x, 24), _W - 24):.1f}" y="{_H - 14}" '
        f'text-anchor="middle">{d:%b}</text>'
        for (x, _), (d, _) in zip(xy, points, strict=True)
    )
    last_x, last_y = xy[-1]
    return (
        f'<svg class="t-chart" viewBox="-8 0 {_W + 16} {_H}" role="img" '
        f'aria-label="Net worth by month">'
        f'<polygon class="t-area" points="{area}"/>'
        f'<polyline class="t-line" points="{line}"/>'
        + dots
        + labels
        + f'<text class="t-value" x="{last_x - 6:.1f}" y="{max(last_y - 12, 14):.1f}" '
        f'text-anchor="end">{escape(money(values[-1]))}</text>' + "</svg>"
    )
