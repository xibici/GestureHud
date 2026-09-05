"""Icon artwork shared by the tray icon and the exe's own icon.

Generated in code rather than shipped as a static asset so the two can never
drift apart - GestureHud.spec renders this same drawing to icon.ico for the
exe (File Explorer, taskbar, Task Manager, the UAC prompt) at build time.
"""
from PIL import Image, ImageDraw


def make_icon_image(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margin = max(2, size // 32)
    bbox = [margin, margin, size - margin, size - margin]
    d.pieslice(bbox, 90, 270, fill=(245, 197, 66, 255))   # sun half
    d.pieslice(bbox, 270, 90, fill=(66, 148, 245, 255))   # speaker half
    d.ellipse(bbox, outline=(28, 28, 30, 255), width=max(1, size // 32))
    return img
