"""Set-of-Mark overlay: number every interactive element on the capture.

Why this exists: a vision model looking at a screenshot of a dense toolbar
guesses. A vision model looking at the *same* screenshot with a numbered box on
each control does not guess - it answers "7" and the legend resolves 7 to an
exact screen rect. The numbering is derived from the accessibility tree, not
from OCR or a detector, so every box is pixel-exact and every rect is stable.

Everything here is pure PIL: no model, no network, no heuristics beyond
overlap resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = (255, 45, 85)
TEXT_COLOR = (255, 255, 255)
OUTLINE = 2
# Two controls overlapping by more than this share of the smaller one are the
# same control exposed twice (Windows Terminal reports both its custom title
# bar buttons and the native caption buttons).
DUPLICATE_OVERLAP = 0.7
# A child control living inside a parent is not a duplicate: the tab and its
# close button are both legitimate targets. Real duplicates (Windows Terminal
# exposing its caption buttons twice) have similar sizes, so a big size gap
# means "parent/child", not "same thing".
DUPLICATE_AREA_RATIO = 4.0
MIN_BOX = 4
# Explorer exposes every list item as a container plus four column cells
# ("Name", "Date modified", ...). The cells are not separate targets, and at
# four per row they would eat the whole mark budget.
SUPPRESS_INSIDE = {"ListItem", "DataItem"}

_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("C:/Windows/Fonts/segoeuib.ttf"),
    Path("C:/Windows/Fonts/tahomabd.ttf"),
)


@dataclass
class Mark:
    number: int
    node: dict
    box: tuple[int, int, int, int]  # image-space x, y, w, h

    @property
    def screen_rect(self) -> list[int]:
        return self.node["rect"]


def _area(box: tuple[int, int, int, int]) -> int:
    return max(0, box[2]) * max(0, box[3])


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = min(ax + aw, bx + bw) - max(ax, bx)
    dy = min(ay + ah, by + bh) - max(ay, by)
    if dx <= 0 or dy <= 0:
        return 0
    return dx * dy


def _contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    # Not 1.0: Explorer's column cells stick out a few pixels past the row box
    # and would otherwise survive as separate marks.
    area = _area(inner)
    return area > 0 and _overlap(outer, inner) >= area * 0.9


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _clip(box, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    x, y, w, h = (round(value) for value in box)
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(size[0], x + w), min(size[1], y + h)
    if x2 - x1 < MIN_BOX or y2 - y1 < MIN_BOX:
        return None
    return (x1, y1, x2 - x1, y2 - y1)


def _scale(box, origin: tuple[int, int], factor: float) -> tuple[float, float, float, float]:
    x, y, w, h = box
    return (
        (x - origin[0]) * factor,
        (y - origin[1]) * factor,
        w * factor,
        h * factor,
    )


def _priority(node: dict) -> tuple[int, int, int]:
    # A control with a readable name is more useful to an agent than the same
    # control without one. Area beats AutomationId on purpose: an unnamed
    # caption button with an id must not outrank a named tab that contains it.
    return (
        1 if (node.get("name") or "").strip() else 0,
        _area(node["rect"]),
        1 if (node.get("id") or "").strip() else 0,
    )


def _is_duplicate(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    shared = _overlap(a, b)
    if not shared:
        return False
    area_a, area_b = _area(a), _area(b)
    smaller, larger = min(area_a, area_b), max(area_a, area_b)
    if smaller <= 0 or larger / smaller > DUPLICATE_AREA_RATIO:
        return False
    return shared / smaller > DUPLICATE_OVERLAP


def select_marks(
    nodes: list[dict],
    origin: tuple[int, int],
    factor: float,
    size: tuple[int, int],
    max_marks: int,
) -> list[Mark]:
    prepared: list[tuple[dict, tuple[int, int, int, int]]] = []
    for node in nodes:
        box = _clip(_scale(node["rect"], origin, factor), size)
        if box is not None:
            prepared.append((node, box))

    # De-duplicate before numbering so numbers never point at the same control
    # twice. Keep the best-scoring member of each cluster.
    kept: list[tuple[dict, tuple[int, int, int, int]]] = []
    containers: list[tuple[int, int, int, int]] = []
    for node, box in sorted(prepared, key=lambda item: _priority(item[0]), reverse=True):
        if any(_contains(container, box) for container in containers):
            # Inside a marked list row every cell is a value, not a target.
            # A button embedded in a row would also be dropped; that trade is
            # deliberate, because four cells per row would eat the budget.
            continue
        if any(_is_duplicate(box, other) for _, other in kept):
            continue
        kept.append((node, box))
        if node["type"] in SUPPRESS_INSIDE:
            containers.append(box)

    # Reading order: numbers should follow the eye, not the tree.
    kept.sort(key=lambda item: (item[1][1], item[1][0]))
    kept = kept[: max(1, max_marks)]

    return [
        Mark(number=index + 1, node=node, box=box)
        for index, (node, box) in enumerate(kept)
    ]


def _badge_free(
    box: tuple[int, int, int, int],
    taken: list[tuple[int, int, int, int]],
    size: tuple[int, int],
) -> bool:
    x, y, w, h = box
    if x < 0 or y < 0 or x + w > size[0] or y + h > size[1]:
        return False
    return not any(_overlap(box, other) > 0 for other in taken)


def _place_badges(
    marks: list[Mark], draw: ImageDraw.ImageDraw, font, size: tuple[int, int]
) -> list[tuple[int, int, int, int]]:
    """Place each number next to its box without covering other numbers."""
    taken: list[tuple[int, int, int, int]] = []
    placements: list[tuple[int, int, int, int]] = []
    font_height = font.size if hasattr(font, "size") else 11
    height = font_height + 7

    for mark in marks:
        label = str(mark.number)
        width = max(height, round(draw.textlength(label, font=font)) + 10)
        x, y, w, h = mark.box
        inside = (x + 2, y + max(0, (h - height) // 2))
        candidates = []
        # Wide rows get their number inside, on the left: above-row placement
        # would land on the neighbouring row and collide with its badge.
        if w >= width * 3 and h >= height:
            candidates.append(inside)
        candidates += [
            (x - width, y - height),        # above-left, outside
            (x, y - height),                # above, aligned
            (x + w, y - height),            # above-right, outside
            (x, y),                         # inside top-left
            (x + w - width, y),             # inside top-right
            (x - width, y),                 # left, outside
            (x, y + h),                     # below
            (x + w - width, y + h),         # below-right
            inside,
        ]
        chosen = None
        for cx, cy in candidates:
            box = (cx, cy, width, height)
            if _badge_free(box, taken, size):
                chosen = box
                break
        if chosen is None:
            # Dense toolbars: walk away from the ideal spot until a free slot
            # appears instead of stacking numbers on top of each other.
            anchor_x = min(max(0, x - width), max(0, size[0] - width))
            anchor_y = min(max(0, y - height), max(0, size[1] - height))
            for radius in (4, 8, 12, 16):
                for dx, dy in (
                    (0, -radius), (0, radius), (-radius, 0), (radius, 0),
                    (radius, -radius), (-radius, -radius),
                    (radius, radius), (-radius, radius),
                ):
                    box = (anchor_x + dx, anchor_y + dy, width, height)
                    if _badge_free(box, taken, size):
                        chosen = box
                        break
                if chosen is not None:
                    break
        if chosen is None:
            chosen = (anchor_x, anchor_y, width, height)
        taken.append(chosen)
        placements.append(chosen)
    return placements


def render(
    image: Image.Image,
    nodes: list[dict],
    origin: tuple[int, int],
    max_marks: int = 60,
    max_side: int = 1600,
) -> tuple[Image.Image, list[Mark], float]:
    """Draw numbered boxes. Returns (image, marks, scale_applied).

    Downscaling happens before drawing so badges stay sharp and the legend
    rects can be mapped with a single factor.
    """
    factor = 1.0
    if max_side > 0 and max(image.size) > max_side:
        factor = max_side / max(image.size)
        image = image.resize(
            (max(1, round(image.width * factor)), max(1, round(image.height * factor))),
            Image.LANCZOS,
        )

    marks = select_marks(nodes, origin, factor, image.size, max_marks)
    if not marks:
        return image, [], factor

    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    font = _load_font(15 if max(image.size) < 1600 else 17)
    placements = _place_badges(marks, draw, font, image.size)

    for mark, badge in zip(marks, placements):
        x, y, w, h = mark.box
        draw.rectangle([x, y, x + w - 1, y + h - 1], outline=ACCENT, width=OUTLINE)
        bx, by, bw, bh = badge
        draw.rounded_rectangle([bx, by, bx + bw - 1, by + bh - 1], radius=4, fill=ACCENT)
        label = str(mark.number)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (bx + (bw - (right - left)) / 2 - left, by + (bh - (bottom - top)) / 2 - top),
            label,
            font=font,
            fill=TEXT_COLOR,
        )

    return canvas, marks, factor
