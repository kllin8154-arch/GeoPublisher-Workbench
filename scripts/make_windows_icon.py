"""Create the local Windows application icon from geometric shapes (no external asset)."""
from pathlib import Path

from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
dest = root / "assets" / "geopublisher.ico"
dest.parent.mkdir(parents=True, exist_ok=True)
s = 256
image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
p = ImageDraw.Draw(image)
p.rounded_rectangle((8, 8, 248, 248), radius=48, fill="#18302F")
p.rounded_rectangle((32, 32, 224, 224), radius=32, fill="#203D39")
p.polygon([(53, 161), (128, 116), (204, 161), (128, 206)], fill="#72B9A7")
p.polygon([(53, 130), (128, 85), (204, 130), (128, 175)], fill="#D7E5CE")
p.polygon([(53, 101), (128, 55), (204, 101), (128, 146)], fill="#5FAE9C")
p.ellipse((171, 55, 202, 86), fill="#D58A5C")
image.save(dest, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(dest)
