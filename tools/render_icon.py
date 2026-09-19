"""Render the editable SVG to PNG and a Windows ICO with multiple resolutions."""
from pathlib import Path
import struct

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

root = Path(__file__).resolve().parents[1] / "assets"
renderer = QSvgRenderer(str(root / "gauge.svg"))
if not renderer.isValid():
    raise RuntimeError("Invalid SVG")


def render(size):
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("PNG rendering failed")
    return bytes(data)


(root / "gauge.png").write_bytes(render(512))
sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
images = [render(size) for size in sizes]
offset = 6 + 16 * len(sizes)
entries = []
for size, data in zip(sizes, images):
    entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
    offset += len(data)
(root / "gauge.ico").write_bytes(struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(images))
print("Created SVG-derived PNG and Windows ICO (9 sizes).")
