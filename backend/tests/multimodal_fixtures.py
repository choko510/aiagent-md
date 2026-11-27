"""In-memory OOXML/PNG/PDF fixtures for multimodal unit tests."""

from __future__ import annotations

import io
import struct
import zipfile
import zlib


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def content_types(main: str, extra: dict[str, str] | None = None) -> bytes:
    kinds = {
        "/xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "/word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "/ppt/presentation.xml": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    }
    rows = f'<Override PartName="{main}" ContentType="{kinds[main]}"/>'
    if main == "/xl/workbook.xml":
        rows += '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    for part, kind in (extra or {}).items():
        rows += f'<Override PartName="{part}" ContentType="{kind}"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{rows}</Types>"
    ).encode()


def xlsx_bytes(
    rows: list[list[str | int | float | None]],
    *,
    formulas: dict[str, tuple[str, str]] | None = None,
    formula_types: dict[str, str] | None = None,
    hidden_sheet: bool = False,
    external_link: bool = False,
    shared_strings: bool = True,
) -> bytes:
    strings: list[str] = []
    cell_xml: list[str] = []
    for r_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c_index, value in enumerate(row, start=1):
            if value is None:
                continue
            ref = f"{chr(64 + c_index)}{r_index}"
            if isinstance(value, str) and shared_strings:
                strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            elif isinstance(value, str):
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        cell_xml.append(f'<row r="{r_index}">' + "".join(cells) + "</row>")
    for ref, (formula, cached) in (formulas or {}).items():
        row_no = int("".join(ch for ch in ref if ch.isdigit()))
        cell_type = (formula_types or {}).get(ref, "e")
        cell_xml[row_no - 1] = cell_xml[row_no - 1].replace(
            "</row>", f'<c r="{ref}" t="{cell_type}"><f>{formula}</f><v>{cached}</v></c></row>'
        )
    shared = ""
    if strings:
        items = "".join(f"<si><t>{s}</t></si>" for s in strings)
        shared = f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" uniqueCount="{len(strings)}">{items}</sst>'
    sheet_state = ' state="hidden"' if hidden_sheet else ""
    files = {
        "[Content_Types].xml": content_types("/xl/workbook.xml"),
        "xl/workbook.xml": (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="Sheet1" sheetId="1"{sheet_state} r:id="rId1"/></sheets></workbook>'
        ).encode(),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            + (
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink" Target="https://example.com/x" TargetMode="External"/>'
                if external_link
                else ""
            )
            + "</Relationships>"
        ).encode(),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(cell_xml)}</sheetData></worksheet>"
        ).encode(),
    }
    if shared:
        files["xl/sharedStrings.xml"] = shared.encode()
    return zip_bytes(files)


def docx_bytes(paragraphs: list[str], table: list[list[str]] | None = None, *, header: str | None = None) -> bytes:
    body: list[str] = []
    for index, text in enumerate(paragraphs):
        style = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if index == 0 else ""
        body.append(f'<w:p>{style}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')
    if table:
        trs = "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>"
            for row in table
        )
        body.append(f"<w:tbl>{trs}</w:tbl>")
    files = {
        "[Content_Types].xml": content_types("/word/document.xml"),
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>"
        ).encode(),
    }
    if header is not None:
        files["word/header1.xml"] = (
            '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>"
        ).encode()
    return zip_bytes(files)


def pptx_bytes(slides: list[dict[str, object]]) -> bytes:
    files: dict[str, bytes] = {"[Content_Types].xml": content_types("/ppt/presentation.xml")}
    sld_ids: list[str] = []
    rels: list[str] = []
    for index, slide in enumerate(slides, start=1):
        sld_ids.append(f'<p:sldId id="{255 + index}" r:id="rId{index}"/>')
        rels.append(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{index}.xml"/>'
        )
        title = str(slide.get("title", ""))
        body = str(slide.get("body", ""))
        shapes = (
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
            f"<p:txBody><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>"
        )
        if body:
            shapes += (
                '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                f"<p:txBody><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody></p:sp>"
            )
        table = slide.get("table")
        if table:
            rows = "".join(
                "<a:tr>"
                + "".join(f"<a:tc><a:txBody><a:p><a:r><a:t>{cell}</a:t></a:r></a:p></a:txBody></a:tc>" for cell in row)
                + "</a:tr>"
                for row in table
            )
            shapes += (
                '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="Table"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
                f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>{rows}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
            )
        files[f"ppt/slides/slide{index}.xml"] = (
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"
        ).encode()
    files["ppt/presentation.xml"] = (
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<p:sldIdLst>{"".join(sld_ids)}</p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/></p:presentation>'
    ).encode()
    files["ppt/_rels/presentation.xml.rels"] = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels)
        + "</Relationships>"
    ).encode()
    return zip_bytes(files)


def png_bytes(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def pdf_bytes(text: str = "Hello multimodal", *, extra: str = "", pages: int = 1) -> bytes:
    page_ids = [3 + i for i in range(pages)]
    content_ids = [3 + pages + i for i in range(pages)]
    font_id = 3 + 2 * pages
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>",
    ]
    for content_id in content_ids:
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        )
    for _ in content_ids:
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(f"<< /Length {len(content)} >> stream\n{content}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n{body}\nendobj\n".encode())
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    if extra:
        out.extend(extra.encode())
    return bytes(out)
