r"""Generate packaging/galil_suite.ico - the app, shortcut and installer icon.

The project shipped no artwork, and Windows needs a real multi-resolution .ico
for the desktop shortcut, the taskbar and the Add/Remove Programs entry.

Drawn rather than embedded as a blob so it stays editable in a diff: a three
arrow XYZ triad, which is what the app actually drives. Colours are picked to
stay distinguishable at 16x16, where the icon is mostly a coloured smudge and
silhouette is all that survives.

No Pillow dependency - the sizes are rendered with QPainter (PyQt5 is already a
requirement) and packed into an ICO container by hand. An ICO is a 6-byte
header, one 16-byte directory entry per image, then the image payloads; storing
each payload as PNG is valid for Vista and later and keeps the file small.

    python packaging/make_icon.py
"""
import os
import struct
import sys

from PyQt5.QtCore import QBuffer, QByteArray, QPointF, Qt
from PyQt5.QtGui import (QColor, QGuiApplication, QImage, QPainter, QPainterPath,
                         QPen, QPolygonF)

SIZES = (16, 24, 32, 48, 64, 128, 256)

BG     = QColor("#161c26")   # matches ui/dark_theme.qss
X_COL  = QColor("#ff6b5a")   # X / axis A
Y_COL  = QColor("#5ad18f")   # Y / axis B
Z_COL  = QColor("#5aa9ff")   # Z / axis C
ORIGIN = QColor("#f2f5f8")


def _arrow(p: QPainter, s: float, ox: float, oy: float,
           dx: float, dy: float, colour: QColor, width: float) -> None:
    """One axis: a shaft from the origin plus a solid arrowhead."""
    x2, y2 = ox + dx, oy + dy
    pen = QPen(colour, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(QPointF(ox, oy), QPointF(x2, y2))

    # Arrowhead, built in the direction of travel so it sits flush on the shaft.
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return
    ux, uy = dx / length, dy / length
    px, py = -uy, ux                      # perpendicular
    head = max(s * 0.11, 2.2)
    half = head * 0.52
    tip = QPointF(x2 + ux * width * 0.2, y2 + uy * width * 0.2)
    base_x, base_y = x2 - ux * head, y2 - uy * head
    poly = QPolygonF([tip,
                      QPointF(base_x + px * half, base_y + py * half),
                      QPointF(base_x - px * half, base_y - py * half)])
    p.setPen(Qt.NoPen)
    p.setBrush(colour)
    p.drawPolygon(poly)


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    s = float(size)
    # Rounded plate, inset slightly so the corners are not clipped.
    inset = s * 0.04
    radius = s * 0.22
    path = QPainterPath()
    path.addRoundedRect(inset, inset, s - 2 * inset, s - 2 * inset, radius, radius)
    p.fillPath(path, BG)
    p.setPen(QPen(QColor("#2b3646"), max(s * 0.02, 0.6)))
    p.drawPath(path)

    # Origin sits low-left of centre so all three arrows get room.
    ox, oy = s * 0.36, s * 0.64
    width = max(s * 0.075, 1.4)
    reach = s * 0.30

    _arrow(p, s, ox, oy,  reach * 1.05,          0.0,          X_COL, width)  # +X right
    _arrow(p, s, ox, oy,  0.0,          -reach * 1.05,         Y_COL, width)  # +Y up
    _arrow(p, s, ox, oy, -reach * 0.72,  reach * 0.72,         Z_COL, width)  # +Z toward viewer

    p.setPen(Qt.NoPen)
    p.setBrush(ORIGIN)
    r = max(s * 0.045, 1.0)
    p.drawEllipse(QPointF(ox, oy), r, r)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer(QByteArray())
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


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
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)          # noqa: F841 - needed for QPainter
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "galil_suite.ico")
    build_ico(out)
    print(f"wrote {out} ({os.path.getsize(out)} bytes, sizes {', '.join(map(str, SIZES))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
