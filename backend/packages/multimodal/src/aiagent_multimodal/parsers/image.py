"""PNG/JPEG/WebP validation and optional local OCR (15-01 §9).

Dimensions are read from the container *before* decoding so a decompression
bomb is refused without allocating the bitmap. Decoding and OCR are optional
adapters; when absent the document explicitly says local OCR was unavailable
instead of inventing text.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from ..errors import MmiError
from ..limits import MmiLimits
from .base import BaseParser, Document, ImageRef, ParseInput, Section, TextBlock

_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_SOF: Final[frozenset[int]] = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    width_px: int
    height_px: int
    channels: int
    bits_per_channel: int
    exif_orientation: int = 1
    animated: bool = False

    @property
    def pixels(self) -> int:
        return self.width_px * self.height_px


def sniff_image(data: bytes) -> ImageInfo:
    if data.startswith(_PNG_SIGNATURE):
        return _png(data)
    if data.startswith(b"\xff\xd8\xff"):
        return _jpeg(data)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return _webp(data)
    raise MmiError("UNSUPPORTED_FORMAT", "image container is not PNG/JPEG/WebP")


def validate_image(info: ImageInfo, limits: MmiLimits) -> None:
    if info.width_px < 1 or info.height_px < 1:
        raise MmiError("PIXEL_LIMIT", "image has invalid dimensions")
    if max(info.width_px, info.height_px) > limits.max_image_edge:
        raise MmiError("PIXEL_LIMIT", "image edge exceeds limit")
    if info.pixels > limits.max_image_pixels:
        raise MmiError("PIXEL_LIMIT", "image pixel count exceeds limit")
    if info.channels > limits.max_image_channels:
        raise MmiError("PIXEL_LIMIT", "image channel count exceeds limit")


def _png(data: bytes) -> ImageInfo:
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise MmiError("BAD_ARCHIVE", "malformed PNG header")
    width, height = struct.unpack(">II", data[16:24])
    bit_depth = data[24]
    color_type = data[25]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 0)
    if channels == 0:
        raise MmiError("UNSUPPORTED_FORMAT", "unsupported PNG color type")
    if data[28] != 0:
        raise MmiError("BAD_ARCHIVE", "unsupported PNG interlace method")
    return ImageInfo("png", width, height, channels, bit_depth)


def _jpeg(data: bytes) -> ImageInfo:
    index = 2
    width = height = channels = 0
    orientation = 1
    bits = 8
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xDA:
            break
        if index + 4 > len(data):
            break
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if length < 2 or index + 2 + length > len(data):
            break
        segment = data[index + 4 : index + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            orientation = _exif_orientation(segment[6:])
        if marker in _SOF and len(segment) >= 6:
            bits = segment[0]
            height, width = struct.unpack(">HH", segment[1:5])
            channels = segment[5]
            if width and height:
                break
        index += 2 + length
    if not width or not height or not channels:
        raise MmiError("BAD_ARCHIVE", "JPEG dimensions are unavailable")
    return ImageInfo("jpeg", width, height, channels, bits, orientation)


def _exif_orientation(tiff: bytes) -> int:
    try:
        if tiff[:2] == b"II":
            endian = "<"
        elif tiff[:2] == b"MM":
            endian = ">"
        else:
            return 1
        ifd = struct.unpack(endian + "I", tiff[4:8])[0]
        count = struct.unpack(endian + "H", tiff[ifd : ifd + 2])[0]
        for i in range(min(count, 512)):
            entry = ifd + 2 + i * 12
            tag, kind, number = struct.unpack(endian + "HHI", tiff[entry : entry + 8])
            if tag == 0x0112 and kind == 3 and number == 1:
                value = struct.unpack(endian + "H", tiff[entry + 8 : entry + 10])[0]
                return value if 1 <= value <= 8 else 1
    except (struct.error, IndexError):
        return 1
    return 1


def _webp(data: bytes) -> ImageInfo:
    if len(data) < 30:
        raise MmiError("BAD_ARCHIVE", "malformed WebP container")
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        animated = bool(data[20] & 0x02)
        return ImageInfo("webp", width, height, 4, 8, 1, animated)
    if chunk == b"VP8 ":
        start = data.find(b"\x9d\x01\x2a", 20, 40)
        if start < 0 or start + 7 > len(data):
            raise MmiError("BAD_ARCHIVE", "malformed WebP frame header")
        width = struct.unpack("<H", data[start + 3 : start + 5])[0] & 0x3FFF
        height = struct.unpack("<H", data[start + 5 : start + 7])[0] & 0x3FFF
        return ImageInfo("webp", width, height, 3 if not data[20] & 0x10 else 4, 8)
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            raise MmiError("BAD_ARCHIVE", "malformed WebP lossless header")
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return ImageInfo("webp", width, height, 4, 8)
    raise MmiError("UNSUPPORTED_FORMAT", "unsupported WebP chunk")


class ImageParser(BaseParser):
    format = "*"
    revision = "1.0.0"

    def parse(self, inp: ParseInput) -> Document:
        info = sniff_image(inp.data)
        validate_image(info, inp.limits)
        decoded_limit = inp.limits.max_image_pixels * inp.limits.max_image_channels
        if info.pixels * info.channels > decoded_limit:
            raise MmiError("PIXEL_LIMIT", "decoded image bytes exceed limit")
        doc = self._document(
            inp,
            metadata={
                "image": {
                    "container": info.format,
                    "widthPx": info.width_px,
                    "heightPx": info.height_px,
                    "channels": info.channels,
                    "bitsPerChannel": info.bits_per_channel,
                    "pixels": info.pixels,
                    "exifOrientation": info.exif_orientation,
                    "animated": info.animated,
                    "metadataStripped": True,
                }
            },
        )
        image_ref = ImageRef(
            image_id="image:1",
            locator=None,
            width_px=info.width_px,
            height_px=info.height_px,
            digest=inp.source_sha256,
        )
        section = Section(
            section_id="image:1",
            kind="image",
            ordinal1=1,
            label="Image",
            locator=None,
            images=[image_ref],
        )
        if info.animated:
            doc.warnings = ("ANIMATED_IMAGE_FIRST_FRAME_ONLY",)
        if info.exif_orientation != 1:
            doc.warnings = (*doc.warnings, "EXIF_ORIENTATION_PRESENT")
        ocr_requested = bool(inp.options.get("run_ocr", False))
        if ocr_requested:
            self._run_ocr(inp, doc, section, info)
        else:
            doc.warnings = (*doc.warnings, "LOCAL_OCR_DISABLED")
        doc.sections.append(section)
        return doc.finalize()

    def _run_ocr(self, inp: ParseInput, doc: Document, section: Section, info: ImageInfo) -> None:
        from ..ocr.tesseract import TesseractOcr, TesseractUnavailable

        engine = TesseractOcr()
        if not engine.available():
            doc.warnings = (*doc.warnings, "LOCAL_OCR_UNAVAILABLE")
            return
        try:
            result = engine.run(inp.data, languages=str(inp.options.get("ocr_languages", "jpn+eng")))
        except TesseractUnavailable:
            doc.warnings = (*doc.warnings, "LOCAL_OCR_UNAVAILABLE")
            return
        except TimeoutError:
            raise MmiError("PARSE_TIMEOUT", "local OCR exceeded its deadline") from None
        if info.exif_orientation != 1:
            doc.warnings = (*doc.warnings, "OCR_COORDINATES_EXIF_UNCORRECTED")
        for index, region in enumerate(result.regions[: inp.limits.max_ocr_regions], start=1):
            bbox = _normalized(region.left, region.top, region.left + region.width, region.top + region.height, info)
            locator = self._locator(
                inp,
                kind="image",
                source_width_px=info.width_px,
                source_height_px=info.height_px,
                exif_orientation=info.exif_orientation,
                normalized_bbox=bbox,
                transform_id=result.transform_id,
                layer="ocr",
            )
            section.blocks.append(
                TextBlock(
                    block_id=f"ocr:{index}",
                    kind="paragraph",
                    text=region.text,
                    locator=locator,
                    recognition_confidence_raw=region.confidence,
                    extraction_method="ocr",
                    ordinal1=index,
                )
            )
        doc.metadata["ocr"] = {
            "engine": result.engine,
            "engineVersion": result.engine_version,
            "languages": result.languages,
            "languagePackDigest": result.language_pack_digest,
            "regions": len(result.regions),
        }
        if not result.regions:
            doc.warnings = (*doc.warnings, "OCR_NO_TEXT")


def _normalized(left: int, top: int, right: int, bottom: int, info: ImageInfo) -> tuple[float, float, float, float]:
    width = max(info.width_px, 1)
    height = max(info.height_px, 1)
    x0 = min(1.0, max(0.0, left / width))
    y0 = min(1.0, max(0.0, top / height))
    x1 = min(1.0, max(0.0, right / width))
    y1 = min(1.0, max(0.0, bottom / height))
    if x1 <= x0:
        x1 = min(1.0, x0 + 1e-6)
    if y1 <= y0:
        y1 = min(1.0, y0 + 1e-6)
    return (x0, y0, x1, y1)
