r"""Generate packaging/stork.ico - the app, shortcut, window and installer icon.

The manipulator this app drives is called "Stork", so the icon is a stork's
head: a white head and neck, a long red-orange bill and a black eye on a blue
plate. Kept to a handful of flat shapes because at 16x16 the icon is a few
coloured pixels and only the silhouette survives - the bill is what makes it
read as a stork rather than a generic bird, so it stays long and bright.

Drawn rather than embedded as a blob so it stays editable in a diff.

No Pillow dependency - the sizes are rendered with QPainter (PyQt5 is already a
requirement) and packed into an ICO container by hand. An ICO is a 6-byte
header, one 16-byte directory entry per image, then the image payloads; storing
each payload as PNG is valid for Vista and later and keeps the file small.

    python packaging/make_icon.py            # writes packaging/stork.ico
    python packaging/make_icon.py --png DIR  # also a PNG per size, to look at
"""
import argparse
import os
import struct
import sys

from PyQt5.QtCore import QBuffer, QByteArray, QPointF, Qt
from PyQt5.QtGui import (QColor, QGuiApplication, QImage, QLinearGradient,
                         QPainter, QPainterPath, QPen)

SIZES = (16, 24, 32, 48, 64, 128, 256)
ICON_NAME = "stork.ico"

SKY_TOP    = QColor("#3d8fd1")
SKY_BOTTOM = QColor("#1f5f99")
FEATHER    = QColor("#ffffff")
FEATHER_SH = QColor("#d9e2ea")   # the shaded side of the neck
BILL       = QColor("#f05a28")
BILL_DARK  = QColor("#c8431a")
EYE        = QColor("#111111")


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = float(size)

    def pt(x: float, y: float) -> QPointF:      # unit square -> pixels
        return QPointF(x * s, y * s)

    # Rounded plate.
    inset, radius = s * 0.03, s * 0.22
    plate = QPainterPath()
    plate.addRoundedRect(inset, inset, s - 2 * inset, s - 2 * inset, radius, radius)
    grad = QLinearGradient(0, 0, 0, s)
    grad.setColorAt(0.0, SKY_TOP)
    grad.setColorAt(1.0, SKY_BOTTOM)
    p.fillPath(plate, grad)
    p.setClipPath(plate)                         # the neck runs off the bottom

    # Neck: a gentle S rising from the bottom edge into the back of the head.
    neck = QPainterPath(pt(0.16, 1.02))
    neck.cubicTo(pt(0.20, 0.78), pt(0.32, 0.60), pt(0.30, 0.34))
    neck.lineTo(pt(0.50, 0.34))                  # hidden inside the head
    neck.cubicTo(pt(0.56, 0.60), pt(0.48, 0.80), pt(0.50, 1.02))
    neck.closeSubpath()
    p.fillPath(neck, FEATHER)
    # A shaded strip down the front of the neck, only where it can be seen.
    if size >= 32:
        shade = QPainterPath(pt(0.44, 0.52))
        shade.cubicTo(pt(0.50, 0.66), pt(0.45, 0.82), pt(0.47, 1.02))
        shade.lineTo(pt(0.50, 1.02))
        shade.cubicTo(pt(0.48, 0.80), pt(0.56, 0.60), pt(0.50, 0.34))
        shade.closeSubpath()
        p.fillPath(shade, FEATHER_SH)

    # Bill: long, straight and tapering, pointing forward and a little down.
    # Drawn before the head so the head overlaps its root cleanly.
    bill = QPainterPath(pt(0.46, 0.24))
    bill.lineTo(pt(0.93, 0.47))
    bill.lineTo(pt(0.91, 0.50))
    bill.lineTo(pt(0.46, 0.40))
    bill.closeSubpath()
    p.fillPath(bill, BILL)
    if size >= 48:                               # the line between the mandibles
        p.setPen(QPen(BILL_DARK, s * 0.012, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(pt(0.52, 0.33), pt(0.91, 0.485))
        p.setPen(Qt.NoPen)

    # Head.
    p.setPen(Qt.NoPen)
    p.setBrush(FEATHER)
    p.drawEllipse(pt(0.40, 0.31), s * 0.15, s * 0.13)

    # Eye: never smaller than a pixel, or it vanishes at 16x16.
    r = max(s * 0.032, 0.9)
    p.setBrush(EYE)
    p.drawEllipse(pt(0.435, 0.285), r, r)

    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    # Hold the QByteArray in a name: QBuffer does not own it, so a temporary
    # can be garbage-collected mid-write - an intermittent segfault.
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def build_ico(path: str) -> None:
    payloads = [png_bytes(render(n)) for n in SIZES]

    header = struct.pack("<HHH", 0, 1, len(SIZES))
    offset = len(header) + 16 * len(SIZES)
    entries, blobs = b"", b""
    for n, data in zip(SIZES, payloads):
        dim = 0 if n >= 256 else n          # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(header + entries + blobs)


def main() -> int:
    ap = argparse.ArgumentParser(description="Draw the Stork Controller icon")
    ap.add_argument("--png", metavar="DIR", help="also write one PNG per size here")
    args = ap.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)          # noqa: F841 - needed for QPainter
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), ICON_NAME)
    build_ico(out)
    print(f"wrote {out} ({os.path.getsize(out)} bytes, sizes {', '.join(map(str, SIZES))})")
    if args.png:
        os.makedirs(args.png, exist_ok=True)
        for n in SIZES:
            render(n).save(os.path.join(args.png, f"stork_{n}.png"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
