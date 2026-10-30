"""Shared MXSSH icon artwork for the Windows tray and executable."""

from PIL import Image, ImageDraw


def create_icon():
    image = Image.new("RGBA", (256, 256), (23, 27, 34, 255))
    draw = ImageDraw.Draw(image)
    draw.line([(56, 76), (108, 120), (56, 164)], fill=(107, 170, 255, 255), width=20)
    draw.line([(128, 172), (200, 172)], fill=(107, 170, 255, 255), width=20)
    return image
