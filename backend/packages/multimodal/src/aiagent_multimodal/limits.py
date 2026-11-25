"""Finite, safe-side ingestion limits (15-01 §7).

These are ceilings, never performance promises. A deployment policy may lower
them; code must not raise them automatically after a limit error.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final, cast

MiB: Final[int] = 1024 * 1024


@dataclass(frozen=True, slots=True)
class MmiLimits:
    max_file_bytes: int = 20 * MiB
    max_task_files: int = 10
    max_task_bytes: int = 80 * MiB
    wall_clock_seconds: int = 60
    cpu_seconds: int = 20
    rss_bytes: int = 384 * MiB
    temp_bytes: int = 128 * MiB
    max_parallel_per_owner: int = 2
    max_parallel_host: int = 4

    max_pdf_pages: int = 100
    max_render_pixels_per_page: int = 4_000_000
    max_render_pixels_total: int = 40_000_000
    max_image_pixels: int = 30_000_000
    max_image_edge: int = 10_000
    max_image_channels: int = 4

    max_zip_entries: int = 2_000
    max_zip_uncompressed_bytes: int = 100 * MiB
    max_zip_entry_bytes: int = 16 * MiB
    max_zip_ratio: float = 100.0
    max_zip_depth: int = 8
    max_zip64_declared_bytes: int = 64 * MiB

    max_xml_nodes: int = 500_000
    max_xml_depth: int = 256
    max_xml_text_bytes: int = 64 * MiB

    max_csv_rows: int = 100_000
    max_csv_columns: int = 200
    max_cells: int = 2_000_000
    max_field_bytes: int = 32 * 1024
    max_csv_record_bytes: int = 256 * 1024

    max_xlsx_sheets: int = 20
    max_xlsx_rows_per_sheet: int = 100_000
    max_shared_strings_bytes: int = 16 * MiB
    max_shared_strings: int = 500_000

    max_ocr_regions: int = 20
    max_vision_crops: int = 3
    max_vision_tokens: int = 4_096
    max_vision_cost_usd: str = "0.20"
    max_pptx_slides: int = 500
    max_docx_paragraphs: int = 100_000

    def tightened(self, **overrides: int | float | str) -> MmiLimits:
        """Return a copy where overrides may only tighten a ceiling."""
        unknown = set(overrides) - set(self.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown limit(s): {sorted(unknown)}")
        for key, value in overrides.items():
            current = getattr(self, key)
            if isinstance(current, (int, float)) and not isinstance(current, bool) and value > current:  # type: ignore[operator]
                raise ValueError(f"limit {key} may only be tightened")
        return replace(self, **cast(Any, overrides))
