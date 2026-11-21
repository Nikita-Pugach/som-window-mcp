"""Tests for blank-frame detection and encoding."""

from __future__ import annotations

from PIL import Image, ImageDraw

from som_window_mcp import capture


def test_uniform_image_is_blank():
    assert capture.analyse(Image.new("RGB", (300, 200), (255, 255, 255)))["blank"] is True
    assert capture.analyse(Image.new("RGB", (300, 200), (0, 0, 0)))["blank"] is True


def test_image_with_content_is_not_blank():
    image = Image.new("RGB", (300, 200), (255, 255, 255))
    ImageDraw.Draw(image).rectangle([20, 20, 120, 80], fill=(0, 0, 0))
    assert capture.analyse(image)["blank"] is False


def test_analyse_reports_size():
    info = capture.analyse(Image.new("RGB", (321, 123), (10, 10, 10)))
    assert info["size"] == [321, 123]


def test_encode_picks_a_working_format():
    image = Image.new("RGB", (400, 300), (240, 240, 240))
    ImageDraw.Draw(image).text((10, 10), "hello", fill=(0, 0, 0))
    data, fmt = capture.encode(image)
    assert fmt in {"png", "jpeg"}
    assert len(data) > 100
    # decodable by the same library
    import io

    Image.open(io.BytesIO(data)).load()


def test_encode_downscales_to_max_side():
    image = Image.new("RGB", (4000, 2000), (255, 255, 255))
    data, _ = capture.encode(image, max_side=1000)
    import io

    with Image.open(io.BytesIO(data)) as decoded:
        assert max(decoded.size) == 1000


def test_encode_keeps_native_size_when_max_side_is_zero():
    image = Image.new("RGB", (500, 400), (255, 255, 255))
    data, _ = capture.encode(image, max_side=0)
    import io

    with Image.open(io.BytesIO(data)) as decoded:
        assert decoded.size == (500, 400)
