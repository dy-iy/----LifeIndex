"""Conservative geometry for complete, axis-labelled weekly timetable images.

No course names, dates, school bells or screenshot fingerprints are built in.
This module does not perform OCR.  It binds coloured card rectangles to visible
grid positions and provides legible crops for a separate, schema-bound reader.
If the grid cannot be independently checked, callers must retain the original
image and use their normal image-understanding path.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from math import ceil
from statistics import median
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError


LAYOUT_VERSION = "weekly-grid-v1"
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_CARDS = 30
_MIMES = {"image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True)
class VisionImage:
    data: bytes
    mime_type: str
    caption: str


@dataclass(frozen=True)
class ScheduleImageLayout:
    """Bytes are temporary model input; only metadata belongs in graph state."""

    _metadata: dict[str, Any]
    images: tuple[VisionImage, ...]

    def metadata(self) -> dict[str, Any]:
        # Return a JSON-shaped copy; callers cannot mutate subsequent reads.
        import copy
        return copy.deepcopy(self._metadata)


def _components(image: Image.Image) -> list[dict]:
    """Run-length connected components avoid an image-sized Python object map."""
    width, height = image.size
    foreground = bytes(
        1 if (max(pixel) >= 90 and min(pixel) >= 35 and max(pixel) - min(pixel) >= 26)
        else 2 if (95 <= min(pixel) <= max(pixel) <= 222 and max(pixel) - min(pixel) <= 18)
        else 0
        for pixel in image.get_flattened_data()
    )
    parents: list[int] = []
    boxes: list[list[int]] = []

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def merge(left: int, right: int) -> int:
        left, right = root(left), root(right)
        if left == right:
            return left
        parents[right] = left
        a, b = boxes[left], boxes[right]
        a[:] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]), a[4] + b[4]]
        return left

    previous: list[tuple[int, int, int, int]] = []
    for y in range(height):
        row = foreground[y * width:(y + 1) * width]
        current = []
        cursor = prior_cursor = 0
        while cursor < width:
            while cursor < width and not row[cursor]:
                cursor += 1
            if cursor == width:
                break
            start, kind, end = cursor, row[cursor], cursor + 1
            while end < width and row[end] == kind:
                end += 1
            if end - start > width * .30:
                # JPEG antialiasing at a full-width footer can otherwise join
                # the last card to a one-pixel horizontal screen boundary.
                cursor = end
                continue
            label = len(parents)
            parents.append(label)
            boxes.append([start, y, end, y + 1, end - start])
            while prior_cursor < len(previous) and previous[prior_cursor][1] < start:
                prior_cursor += 1
            index = prior_cursor
            while index < len(previous) and previous[index][0] <= end:
                if previous[index][3] == kind:
                    label = merge(label, previous[index][2])
                index += 1
            current.append((start, end, label, kind))
            cursor = end
        previous = current
    result = []
    for index, box in enumerate(boxes):
        if root(index) != index:
            continue
        left, top, right, bottom, area = box
        card_width, card_height = right - left, bottom - top
        if (max(26, width * .035) <= card_width <= width * .20
                and card_height >= max(35, height * .022, card_width * .50)
                and area / (card_width * card_height) >= .42):
            result.append({"bbox": (left, top, right, bottom), "area": area})
    return result


def _cluster(values: list[float], tolerance: float) -> list[float]:
    clusters: list[list[float]] = []
    for value in sorted(values):
        if not clusters or value - median(clusters[-1]) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [float(median(group)) for group in clusters]


def _header_lines(gray: Image.Image, top: int, bottom: int) -> list[float]:
    """Require faint grid lines over most header height, not text strokes."""
    if bottom - top < 30:
        return []
    pixels, width = gray.load(), gray.width
    scores = []
    for x in range(3, width - 3):
        hits = sum(205 <= pixels[x, y] <= 245
                   and (pixels[x - 3, y] + pixels[x + 3, y]) / 2 - pixels[x, y] >= 4
                   for y in range(top, bottom))
        if hits / (bottom - top) >= .62:
            scores.append(float(x))
    return _cluster(scores, 4)


def _axis_labels(gray: Image.Image, left: float, step: float, top: int, bottom: int) -> list[tuple[int, int]]:
    # Period numerals are dark and centred in the left axis.  Grey time labels
    # are ignored; if indistinguishable, regularity checks below reject it.
    pixels = gray.load()
    x0, x1 = max(0, round(left - .80 * step)), max(1, round(left - .22 * step))
    active = []
    for y in range(top, bottom):
        active.append(sum(pixels[x, y] < 90 for x in range(x0, x1)) >= 1)
    # Thin serifs can vanish for a couple of scanlines after downsampling.
    for index in range(1, len(active) - 3):
        if active[index - 1] and not active[index]:
            for distance in (1, 2, 3):
                if active[index + distance]:
                    active[index:index + distance] = [True] * distance
                    break
    groups = []
    start = None
    for index, present in enumerate(active + [False]):
        if present and start is None:
            start = index
        elif not present and start is not None:
            if index - start >= max(5, step * .06):
                groups.append((top + start, top + index))
            start = None
    return groups


def _png(image: Image.Image, caption: str) -> VisionImage:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return VisionImage(buffer.getvalue(), "image/png", caption)


def _images(source: Image.Image, metadata: dict) -> tuple[VisionImage, ...]:
    width, height = source.size
    left, top, right, bottom = metadata["grid_bbox"]
    header = source.crop((0, 0, width, top))
    if header.width > 1400:
        header.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
    # The complete left axis is kept at native scale or enlarged; its printed
    # times must be read, never reconstructed from a generic school timetable.
    axis = source.crop((0, max(0, top - 4), max(1, left), bottom))
    axis_scale = min(2.0, 1900 / max(1, axis.height))
    axis = axis.resize((max(1, round(axis.width * axis_scale)), max(1, round(axis.height * axis_scale))), Image.Resampling.LANCZOS)
    board = Image.new("RGB", (max(header.width, axis.width + 400), header.height + axis.height + 90), "white")
    draw = ImageDraw.Draw(board)
    font = ImageFont.load_default(size=20)
    board.paste(header, (0, 0))
    draw.text((12, header.height + 8), "Read weekday/date header and exact printed period times.", font=font, fill="black")
    board.paste(axis, (0, header.height + 45))
    draw.multiline_text((axis.width + 20, header.height + 60),
                        "GEOMETRY ONLY\nRows are positions, not OCR.\nVerify first row is period 1.\nVerify columns run Mon to Sun.\nDo not invent missing times.",
                        font=font, fill="black", spacing=14)
    result = [_png(board, "日期/星期表头和原图左侧节次时间。必须核对七列为周一到周日、第一行为第1节；看不清时报告不确定。")]
    cards = metadata["cards"]
    for offset in range(0, len(cards), 6):
        subset = cards[offset:offset + 6]
        cell_width, cell_height, label_height = 400, 850, 48
        atlas = Image.new("RGB", (cell_width * min(3, len(subset)), cell_height * ceil(len(subset) / 3)), "white")
        draw = ImageDraw.Draw(atlas)
        for index, card in enumerate(subset):
            x, y = index % 3 * cell_width, index // 3 * cell_height
            draw.text((x + 8, y + 5), f'{card["card_id"]} | day={card["weekday"]} | rows={card["period_start"]}-{card["period_end"]}', font=font, fill="black")
            box = card["bbox"]
            crop = source.crop((max(0, box[0] - 3), max(0, box[1] - 3), min(width, box[2] + 3), min(height, box[3] + 3)))
            scale = min(3, (cell_width - 20) / crop.width, (cell_height - label_height - 12) / crop.height)
            crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.Resampling.LANCZOS)
            atlas.paste(crop, (x + (cell_width - crop.width) // 2, y + label_height))
            draw.rectangle((x, y, x + cell_width - 1, y + cell_height - 1), outline="#bbbbbb", width=2)
        result.append(_png(atlas, "按 card_id 逐张读取完整课程名与地点；day/rows 是程序从原图网格绑定的位置，不能根据文字重新排列。"))
    return tuple(result)


def analyze_schedule_image(data: bytes, mime_type: str = "image/jpeg") -> ScheduleImageLayout | None:
    """Return checked geometry/crops, or None without guessing unsupported grids.

    `weekday` uses ISO 1..7 *only after* the OCR stage verifies the printed
    header is Monday..Sunday. Period numbers are likewise provisional row
    positions until the OCR stage verifies the complete 1-based left axis.
    These explicit checks are part of the metadata contract, not optional hints.
    """
    if mime_type not in _MIMES or not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        return None
    try:
        with Image.open(BytesIO(data)) as opened:
            if opened.width * opened.height > MAX_IMAGE_PIXELS or min(opened.size) < 240:
                return None
            source = ImageOps.exif_transpose(opened).convert("RGB")
        working = source.copy()
        working.thumbnail((900, 3000), Image.Resampling.LANCZOS)
        scale_x, scale_y = source.width / working.width, source.height / working.height
        candidates = _components(working)
        if not 5 <= len(candidates) <= MAX_CARDS:
            return None
        card_width = median(item["bbox"][2] - item["bbox"][0] for item in candidates)
        # Reject mixed shapes instead of silently dropping a potential course.
        if any(abs((item["bbox"][2] - item["bbox"][0]) / card_width - 1) > .14 for item in candidates):
            return None
        centres = _cluster([(item["bbox"][0] + item["bbox"][2]) / 2 for item in candidates], card_width * .15)
        if len(centres) < 3:
            return None
        spacing = [right - left for left, right in zip(centres, centres[1:])]
        step = min(spacing)
        if not 1.00 <= step / card_width <= 1.35:
            return None
        if any(abs(gap / step - round(gap / step)) > .06 for gap in spacing):
            return None
        first_top = min(item["bbox"][1] for item in candidates)
        gray = working.convert("L")
        lines = _header_lines(gray, max(0, round(first_top - step)), max(0, round(first_top - step * .10)))
        grid_left = None
        for left in lines:
            if not .55 * step <= left <= 1.7 * step or abs(working.width - (left + 7 * step)) > .20 * step:
                continue
            if all(any(abs(line - (left + index * step)) <= .055 * step for line in lines) for index in range(7)):
                grid_left = left
                break
        if grid_left is None:
            return None
        final_bottom = max(item["bbox"][3] for item in candidates)
        labels = _axis_labels(gray, grid_left, step, max(0, first_top - round(step * .12)), min(working.height, final_bottom + round(step * .85)))
        if not 5 <= len(labels) <= 16:
            return None
        label_centres = [(top + bottom) / 2 for top, bottom in labels]
        row_step = median(b - a for a, b in zip(label_centres, label_centres[1:]))
        if not .55 <= row_step / step <= 2.2 or any(abs((b - a) / row_step - 1) > .075 for a, b in zip(label_centres, label_centres[1:])):
            return None
        if not -.02 <= (labels[0][0] - first_top) / row_step <= .25:
            return None
        # Card starts and ends must agree on the same row grid. Infer the
        # common inset from both edges, rather than accepting off-grid blocks.
        starts = [item["bbox"][1] for item in candidates]
        ends = [item["bbox"][3] for item in candidates]
        grid_top = median(starts[index] - round((starts[index] - first_top) / row_step) * row_step for index in range(len(starts)))
        inset = median((round((end - grid_top) / row_step) * row_step + grid_top - end) for end in ends)
        if not -.015 * row_step <= inset <= .10 * row_step:
            return None
        cards = []
        for item in candidates:
            left, top, right, bottom = item["bbox"]
            column = round(((left + right) / 2 - grid_left) / step - .5)
            start = round((top - grid_top) / row_step)
            end = round((bottom + inset - grid_top) / row_step)
            if not (0 <= column < 7 and 0 <= start < end <= len(labels)):
                return None
            if (abs((left + right) / 2 - (grid_left + (column + .5) * step)) > .06 * step
                    or abs(top - (grid_top + start * row_step)) > .06 * row_step
                    or abs(bottom + inset - (grid_top + end * row_step)) > .06 * row_step):
                return None
            cards.append({"weekday": column + 1, "period_start": start + 1, "period_end": end,
                          "bbox": [round(left * scale_x), round(top * scale_y), round(right * scale_x), round(bottom * scale_y)]})
        cards.sort(key=lambda item: (item["weekday"], item["period_start"]))
        for index, card in enumerate(cards):
            card["card_id"] = f"C{index + 1:02d}"
            if index and cards[index - 1]["weekday"] == card["weekday"] and cards[index - 1]["period_end"] >= card["period_start"]:
                return None
        top_original = max(0, round((grid_top - max(0, inset) / 2) * scale_y))
        bottom_original = min(source.height, round((grid_top + len(labels) * row_step) * scale_y))
        metadata = {"version": LAYOUT_VERSION, "image_width": source.width, "image_height": source.height,
                    "requires_axis_verification": True, "expected_weekday_order": [1, 2, 3, 4, 5, 6, 7],
                    "grid_bbox": [round(grid_left * scale_x), top_original, min(source.width, round((grid_left + 7 * step) * scale_x)), bottom_original],
                    "columns": [{"weekday": index + 1, "x_start": round((grid_left + index * step) * scale_x), "x_end": round((grid_left + (index + 1) * step) * scale_x)} for index in range(7)],
                    "periods": [{"period": index + 1, "y_start": round((grid_top + index * row_step) * scale_y), "y_end": min(source.height, round((grid_top + (index + 1) * row_step) * scale_y))} for index in range(len(labels))],
                    "cards": cards}
        return ScheduleImageLayout(metadata, _images(source, metadata))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
