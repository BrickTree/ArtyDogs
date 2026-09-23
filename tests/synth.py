"""Synthetic WARDOGS-style screens for exercising the readout reader.

A terrain map with grid labels and place names, a squad-chat box full of decoy
coordinates, a cursor arrow, and the coordinate readout either beside the cursor
or pinned to a corner. Real game screenshots should replace this once we have
them; until then it keeps the reader honest about clutter and decoys.
"""
from __future__ import annotations

import random
import string
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONTS = ["arial.ttf", "arialbd.ttf", "bahnschrift.ttf", "segoeui.ttf", "tahoma.ttf", "verdana.ttf", "consola.ttf"]
W, H = 1920, 1080
MAP = (300, 0, W, H)  # map panel; a UI sidebar sits to its left
CHAT = (320, 860, 900, 1040)
FIXED_READOUT_AT = (1560, 1046)


@dataclass
class Case:
    image: Image.Image
    cursor: tuple[int, int]
    truth: tuple[float, float]
    meta: str


def _terrain(rng: random.Random) -> Image.Image:
    small = Image.new("RGB", (W // 24 + 2, H // 24 + 2))
    px = small.load()
    palette = [(70, 90, 55), (95, 100, 70), (120, 110, 85), (60, 75, 60), (140, 135, 120), (80, 95, 100)]
    for y in range(small.height):
        for x in range(small.width):
            base = rng.choice(palette)
            px[x, y] = tuple(max(0, min(255, c + rng.randint(-14, 14))) for c in base)
    return small.resize((W, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(name, size)


def make_case(rng: random.Random, layout: str) -> Case:
    img = _terrain(rng)
    d = ImageDraw.Draw(img, "RGBA")
    # grid every 100 px with edge labels, like a 100 m tactical grid at some zoom
    for gx in range(MAP[0], W, 100):
        d.line([(gx, 0), (gx, H)], fill=(25, 25, 25, 160))
    for gy in range(0, H, 100):
        d.line([(MAP[0], gy), (W, gy)], fill=(25, 25, 25, 160))
    small = _font("arial.ttf", 13)
    for i, gx in enumerate(range(MAP[0], W, 100)):
        d.text((gx + 4, 4), string.ascii_uppercase[i % 26], font=small, fill=(230, 230, 230, 255))
    for i, gy in enumerate(range(0, H, 100)):
        d.text((MAP[0] + 4, gy + 4), str(i + 1), font=small, fill=(230, 230, 230, 255))
    # place names, icons and numbers that must not be mistaken for the readout
    for _ in range(14):
        x, y = rng.randint(MAP[0] + 30, W - 160), rng.randint(30, H - 40)
        label = rng.choice(["Bakurani", "Tower 3", "Valkyra", "Havoc", "FOB 2", "Depot", "500m", "B-4", "Hill 312"])
        d.text((x, y), label, font=_font(rng.choice(FONTS), rng.randint(12, 18)), fill=(245, 245, 235, 255))
        d.ellipse((x - 14, y + 2, x - 4, y + 12), fill=rng.choice([(220, 60, 60, 255), (60, 140, 230, 255)]))
    # sidebar UI
    d.rectangle((0, 0, MAP[0], H), fill=(22, 24, 26, 255))
    for i, s in enumerate(["SQUAD 2", "Wolf  12/40", "Bravo  $1,250", "Kodiak", "SPH-2  27s", "Ammo 14"]):
        d.text((16, 20 + 34 * i), s, font=_font("segoeui.ttf", 16), fill=(210, 210, 210, 255))
    # squad chat with decoy coordinates
    d.rectangle(CHAT, fill=(0, 0, 0, 140))
    chat_font = _font("arial.ttf", 16)
    marks: list[tuple[tuple[float, float], tuple[int, int, int, int]]] = []
    for i in range(5):
        at = (CHAT[0] + 10, CHAT[1] + 8 + 32 * i)
        if rng.random() < 0.6 or (layout == "chat" and i == 4 and not marks):
            cx_, cy_ = round(rng.uniform(20, 140), 2), round(rng.uniform(20, 140), 2)
            s = f"[SQUAD] {rng.choice(['Wolf', 'Bravo', 'Mako'])}: X{cx_:.2f} Y{cy_:.2f}"
            marks.append(((cx_, cy_), d.textbbox(at, s, font=chat_font)))
        else:
            s = f"[ALL] {rng.choice(['Nyx', 'Ivan'])}: arty on B4 pls"
        d.text(at, s, font=chat_font, fill=(235, 235, 235, 255))
    if layout == "chat":  # hover a Mark Coordinates line; the map readout is not shown
        truth, bb = rng.choice(marks)
        cx, cy = (bb[0] + bb[2]) // 2 + 60, (bb[1] + bb[3]) // 2
        d.polygon([(cx, cy), (cx, cy + 18), (cx + 5, cy + 13), (cx + 12, cy + 13)], fill=(255, 255, 255, 255),
                  outline=(0, 0, 0, 255))
        return Case(img, (cx, cy), truth, "chat")

    font_name, size = rng.choice(FONTS), rng.choice([12, 13, 14, 16, 18, 20])
    style = rng.choice(["plain", "panel", "shadow"])
    tx, ty = round(rng.uniform(20, 140), 2), round(rng.uniform(20, 140), 2)
    text = f"X{tx:.2f} Y{ty:.2f}" if rng.random() < 0.7 else f"X {tx:.2f}  Y {ty:.2f}"
    f = _font(font_name, size)
    while True:  # keep the cursor on the map and away from the chat box
        cx, cy = rng.randint(MAP[0] + 40, W - 260), rng.randint(40, H - 80)
        if not (CHAT[0] - 40 <= cx <= CHAT[2] + 40 and CHAT[1] - 60 <= cy <= CHAT[3]):
            break
    if layout == "split":
        # What WARDOGS actually draws: crosshair rulers through the cursor, labelled
        # "y73.39" above and "x81.57" below, as two separate lines.
        d.line([(cx + 2, 0), (cx + 2, H)], fill=(235, 235, 235, 170))
        d.line([(MAP[0], cy - 5), (W, cy - 5)], fill=(235, 235, 235, 170))
        for label, at in ((f"y{ty:.2f}", (cx + 3, cy - 75)), (f"x{tx:.2f}", (cx + 29, cy - 20))):
            box = d.textbbox(at, label, font=f)
            if style == "panel":
                d.rectangle((box[0] - 5, box[1] - 3, box[2] + 5, box[3] + 3), fill=(10, 12, 10, 150))
            if style == "shadow":
                d.text((at[0] + 1, at[1] + 1), label, font=f, fill=(0, 0, 0, 255))
            d.text(at, label, font=f, fill=(245, 245, 240, 255))
        d.polygon([(cx, cy), (cx, cy + 18), (cx + 5, cy + 13), (cx + 12, cy + 13)],
                  fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
        return Case(img, (cx, cy), (tx, ty), f"split/{font_name}/{size}/{style}")
    if layout == "cursor":
        at = (cx + 20, cy + 24)
    else:
        bb = d.textbbox((0, 0), text, font=f)
        at = (FIXED_READOUT_AT[0], FIXED_READOUT_AT[1] - (bb[3] - bb[1]) - 6)
    box = d.textbbox(at, text, font=f)
    if style == "panel":
        d.rectangle((box[0] - 6, box[1] - 4, box[2] + 6, box[3] + 4), fill=(10, 12, 10, 175))
    if style == "shadow":
        d.text((at[0] + 1, at[1] + 1), text, font=f, fill=(0, 0, 0, 255))
    d.text(at, text, font=f, fill=rng.choice([(240, 240, 230), (250, 220, 120), (200, 240, 200)]) + (255,))
    # the mouse arrow itself
    d.polygon([(cx, cy), (cx, cy + 18), (cx + 5, cy + 13), (cx + 12, cy + 13)], fill=(255, 255, 255, 255),
              outline=(0, 0, 0, 255))
    return Case(img, (cx, cy), (tx, ty), f"{layout}/{font_name}/{size}/{style}")
