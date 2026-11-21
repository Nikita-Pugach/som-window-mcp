"""Tests for the Set-of-Mark selection and badge layout. Pure PIL, no Win32."""

from __future__ import annotations

from PIL import Image, ImageDraw

from som_window_mcp import overlay

SIZE = (400, 300)


def node(kind, name, rect, node_id=""):
    return {
        "path": "0",
        "type": kind,
        "name": name,
        "id": node_id,
        "rect": list(rect),
        "enabled": True,
    }


def select(nodes, max_marks=60, size=SIZE):
    return overlay.select_marks(nodes, (0, 0), 1.0, size, max_marks)


def test_named_duplicate_wins_over_unnamed_twin():
    # Windows Terminal exposes its caption buttons twice.
    nodes = [
        node("Button", "Свернуть", (100, 10, 60, 37)),
        node("Button", "", (103, 5, 58, 50), "MinimizeButton"),
    ]
    marks = select(nodes)
    assert len(marks) == 1
    assert marks[0].node["name"] == "Свернуть"


def test_tab_is_not_a_duplicate_of_its_own_close_button():
    nodes = [
        node("TabItem", "Some tab", (10, 10, 300, 40)),
        node("Button", "Закрыть вкладку", (270, 15, 40, 30), "CloseButton"),
    ]
    marks = select(nodes)
    assert len(marks) == 2, "a tab and the close button inside it are different targets"


def test_cells_inside_a_list_row_are_suppressed():
    # Explorer: row box plus a column cell that sticks out a few pixels.
    nodes = [
        node("ListItem", "0409", (200, 100, 700, 26)),
        node("Edit", "Размер", (815, 100, 94, 26)),
    ]
    marks = select(nodes)
    assert [m.node["type"] for m in marks] == ["ListItem"]


def test_free_standing_edit_is_kept():
    nodes = [
        node("ListItem", "0409", (200, 100, 700, 26)),
        node("Edit", "Поле поиска", (900, 100, 200, 26)),
    ]
    assert len(select(nodes, size=(1200, 400))) == 2


def test_numbering_follows_reading_order():
    nodes = [
        node("Button", "bottom", (10, 200, 50, 20)),
        node("Button", "top-right", (200, 10, 50, 20)),
        node("Button", "top-left", (10, 10, 50, 20)),
    ]
    marks = select(nodes)
    assert [m.node["name"] for m in marks] == ["top-left", "top-right", "bottom"]
    assert [m.number for m in marks] == [1, 2, 3]


def test_max_marks_caps_the_legend():
    nodes = [node("Button", f"b{i}", (10, 10 + i * 30, 50, 20)) for i in range(10)]
    assert len(select(nodes, max_marks=3)) == 3


def test_offscreen_nodes_are_dropped():
    nodes = [
        node("Button", "outside", (-500, -500, 10, 10)),
        node("Button", "inside", (10, 10, 50, 20)),
    ]
    marks = select(nodes)
    assert [m.node["name"] for m in marks] == ["inside"]


def test_tiny_nodes_are_dropped():
    nodes = [node("Button", "dot", (10, 10, 2, 2))]
    assert select(nodes) == []


# ------------------------------------------------------------------- layout
def test_badges_never_overlap_each_other():
    # A deliberately dense strip of controls, like a toolbar.
    nodes = [node("Button", f"b{i}", (10 + i * 14, 10, 13, 22)) for i in range(20)]
    marks = select(nodes)
    image = Image.new("RGB", SIZE, (250, 250, 250))
    draw = ImageDraw.Draw(image)
    font = overlay._load_font(15)
    boxes = overlay._place_badges(marks, draw, font, SIZE)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert overlay._overlap(boxes[i], boxes[j]) == 0
    for x, y, w, h in boxes:
        assert 0 <= x and 0 <= y and x + w <= SIZE[0] and y + h <= SIZE[1]


def test_render_draws_and_downscales():
    nodes = [node("Button", "a", (10, 10, 100, 40)), node("Button", "b", (10, 100, 100, 40))]
    image = Image.new("RGB", (2000, 1000), (255, 255, 255))
    rendered, marks, factor = overlay.render(image, nodes, (0, 0), max_marks=60, max_side=800)
    assert len(marks) == 2
    assert max(rendered.size) == 800
    assert factor < 1.0
    # something was actually drawn
    assert rendered.tobytes() != Image.new("RGB", rendered.size, (255, 255, 255)).tobytes()


def test_render_without_nodes_returns_untouched_image():
    image = Image.new("RGB", (100, 100), (255, 255, 255))
    rendered, marks, _ = overlay.render(image, [], (0, 0))
    assert marks == []
    assert rendered.size == image.size
