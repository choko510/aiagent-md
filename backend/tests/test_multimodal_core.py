"""Core multimodal unit tests: gates, parsers, extraction, verification."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import stat
import threading
import zipfile

import pytest
from aiagent_multimodal.archive_gate import inspect_archive
from aiagent_multimodal.blobstore import QuarantineBlobStore
from aiagent_multimodal.contracts import MmiSourceLocator, canonical_parse_key
from aiagent_multimodal.detection import DeclaredInput, detect
from aiagent_multimodal.errors import MmiError
from aiagent_multimodal.extraction.fields import extract_candidates
from aiagent_multimodal.limits import MmiLimits
from aiagent_multimodal.parser_registry import ParserRegistry
from aiagent_multimodal.parsers.base import Document, ParseInput, Section, TextBlock
from aiagent_multimodal.parsers.csv import CsvParser
from aiagent_multimodal.parsers.docx import DocxParser
from aiagent_multimodal.parsers.image import ImageParser
from aiagent_multimodal.parsers.pdf import PdfParser, preflight_pdf
from aiagent_multimodal.parsers.pptx import PptxParser
from aiagent_multimodal.parsers.xlsx import XlsxParser
from aiagent_multimodal.verification import verify_candidates
from aiagent_multimodal.xml_gate import parse_xml
from multimodal_fixtures import (
    docx_bytes,
    pdf_bytes,
    png_bytes,
    pptx_bytes,
    xlsx_bytes,
    zip_bytes,
)


def make_input(fmt: str, data: bytes, *, options: dict[str, object] | None = None) -> ParseInput:
    registry = ParserRegistry()
    return ParseInput(
        source_artifact_id="art_01",
        source_sha256=hashlib.sha256(data).hexdigest(),
        format=fmt,
        data=data,
        parser_revision="1.0.0",
        build_digest=registry.build_digest,
        policy_revision="policy_01",
        options=options or {},
    )


def test_detection_pdf_and_png() -> None:
    assert detect(pdf_bytes(), DeclaredInput(file_name="a.pdf"), MmiLimits()).format == "pdf"
    assert detect(png_bytes(), DeclaredInput(file_name="a.png"), MmiLimits()).format == "png"


def test_detection_ooxml_formats() -> None:
    limits = MmiLimits()
    assert detect(xlsx_bytes([["a"]]), DeclaredInput(file_name="a.xlsx"), limits).format == "xlsx"
    assert detect(docx_bytes(["a"]), DeclaredInput(file_name="a.docx"), limits).format == "docx"
    assert detect(pptx_bytes([{"title": "a"}]), DeclaredInput(file_name="a.pptx"), limits).format == "pptx"


def test_detection_rejects_macro_and_polyglot() -> None:
    macro = zip_bytes(
        {
            "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
            "word/document.xml": b"<w:document xmlns:w='x'/>",
            "word/vbaProject.bin": b"macro",
        }
    )
    with pytest.raises(MmiError) as exc:
        detect(macro, DeclaredInput(file_name="a.docx"), MmiLimits())
    assert exc.value.code == "UNSUPPORTED_ACTIVE_CONTENT"
    with pytest.raises(MmiError) as exc:
        detect(b"%PDF-1.4\n" + png_bytes(), DeclaredInput(file_name="a.pdf"), MmiLimits())
    assert exc.value.code == "AMBIGUOUS_FORMAT"


def test_detection_mime_mismatch_and_unknown_text() -> None:
    with pytest.raises(MmiError) as exc:
        detect(png_bytes(), DeclaredInput(file_name="a.pdf", declared_mime="application/pdf"), MmiLimits())
    assert exc.value.code == "MIME_MISMATCH"
    with pytest.raises(MmiError) as exc:
        detect(b"hello world\n", DeclaredInput(file_name="a.txt"), MmiLimits())
    assert exc.value.code == "UNSUPPORTED_FORMAT"


def test_detection_cp932_requires_explicit_encoding() -> None:
    data = "a,b\n1,日本\n".encode("cp932")
    with pytest.raises(MmiError) as exc:
        detect(data, DeclaredInput(file_name="a.csv"), MmiLimits())
    assert exc.value.code == "INVALID_ENCODING"
    found = detect(data, DeclaredInput(file_name="a.csv", encoding_hint="cp932"), MmiLimits())
    assert found.encoding == "cp932"


def test_archive_gate_valid_and_limits() -> None:
    report = inspect_archive(zip_bytes({"a.txt": b"hello"}), MmiLimits())
    assert report.has("a.txt")
    assert report.expanded_bytes == 5
    with pytest.raises(MmiError) as exc:
        inspect_archive(zip_bytes({"a.txt": b"x" * 1000}), MmiLimits(max_zip_ratio=2.0))
    assert exc.value.code == "EXPANSION_LIMIT"


def test_archive_rejects_traversal_duplicate_symlink_and_nested() -> None:
    for files in ({"../evil": b"x"}, {"/abs": b"x"}, {"C:/x": b"x"}, {"a:b": b"x"}):
        with pytest.raises(MmiError):
            inspect_archive(zip_bytes(files), MmiLimits())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("dup.txt", b"a")
        zf.writestr("DUP.txt", b"b")
    with pytest.raises(MmiError) as exc:
        inspect_archive(buffer.getvalue(), MmiLimits())
    assert exc.value.code == "BAD_ARCHIVE"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, b"target")
    with pytest.raises(MmiError):
        inspect_archive(buffer.getvalue(), MmiLimits())
    with pytest.raises(MmiError) as exc:
        inspect_archive(zip_bytes({"inner.zip": zip_bytes({"a": b"x"})}), MmiLimits())
    assert exc.value.code == "BAD_ARCHIVE"


def test_xml_gate_rejects_dtd_and_depth() -> None:
    with pytest.raises(MmiError) as exc:
        parse_xml(b'<!DOCTYPE x [<!ENTITY e "boom">]><root>&e;</root>', MmiLimits())
    assert exc.value.code == "BAD_XML"
    with pytest.raises(MmiError) as exc:
        parse_xml(b"<a><b/></a>", MmiLimits(max_xml_depth=1))
    assert exc.value.code == "EXPANSION_LIMIT"
    assert parse_xml(b"<a><b/></a>", MmiLimits()).local == "a"


def test_csv_quoted_newline_offsets_and_missing() -> None:
    data = b'name,note,price\nA,"line1\nline2",100\nB,,200\n'
    inp = make_input("csv", data)
    doc = CsvParser().parse(inp)
    table = doc.tables()[0]
    assert table.cell(row=2, column=2).text == "line1\nline2"
    assert table.cell(row=3, column=2).text == ""
    assert table.cell(row=3, column=2).value_state != "unknown"
    quoted = table.cell(row=2, column=2)
    assert quoted.locator.byte_start is not None
    assert data[int(quoted.locator.byte_start) : int(quoted.locator.byte_end)].decode() == "line1\nline2"
    assert "CSV_INJECTION_AS_DATA" not in doc.warnings


def test_csv_injection_and_unclosed_quote() -> None:
    doc = CsvParser().parse(make_input("csv", b"a,b\n=1+1,2\n"))
    assert "CSV_INJECTION_AS_DATA" in doc.warnings
    doc = CsvParser().parse(make_input("csv", b'a,b\n"unterminated,2\n'))
    assert "UNCLOSED_QUOTE" in doc.warnings


def test_xlsx_formula_cached_error_is_not_zero() -> None:
    data = xlsx_bytes([["Item", "Value"], ["A", "ok"]], formulas={"B2": ("1/0", "#DIV/0!")})
    doc = XlsxParser().parse(make_input("xlsx", data))
    cell = doc.tables()[0].cell(a1="B2")
    assert cell is not None
    assert cell.text is None
    assert cell.cached_error == "#DIV/0!"
    assert cell.value_state == "unknown"
    assert cell.locator.kind == "sheet"
    assert "FORMULA_CACHED_ERROR" in doc.warnings


def test_xlsx_rejects_external_relationship() -> None:
    data = xlsx_bytes([["a"]], external_link=True)
    with pytest.raises(MmiError) as exc:
        XlsxParser().parse(make_input("xlsx", data))
    assert exc.value.code == "EXTERNAL_REFERENCE"


def test_docx_heading_table_and_header() -> None:
    data = docx_bytes(["見積書", "本文"], [["品目", "金額"], ["A", "100"]], header="Header")
    doc = DocxParser().parse(make_input("docx", data))
    blocks = doc.blocks()
    assert blocks[0].kind == "heading"
    assert blocks[0].level == 1
    assert any(b.kind == "header" and b.text == "Header" for b in blocks)
    table = doc.tables()[0]
    cell = table.cell(row=2, column=2)
    assert cell is not None and cell.text == "100"
    assert cell.locator.physical_page == "unknown"
    assert cell.locator.part_name == "word/document.xml"


def test_pptx_slide_order_and_table() -> None:
    data = pptx_bytes(
        [
            {"title": "First", "body": "one", "table": [["k", "v"], ["a", "1"]]},
            {"title": "Second", "body": "two"},
        ]
    )
    doc = PptxParser().parse(make_input("pptx", data))
    assert [s.label for s in doc.sections] == ["Slide 1", "Slide 2"]
    assert doc.sections[0].blocks[0].kind == "slide_title"
    cell = doc.tables()[0].cell(row=2, column=2)
    assert cell is not None and cell.text == "1"
    assert cell.locator.slide1 == 1
    assert cell.locator.physical_page == "unknown"


def test_pdf_preflight_rejects_active_content() -> None:
    with pytest.raises(MmiError) as exc:
        preflight_pdf(pdf_bytes(extra="/JavaScript"), MmiLimits())
    assert exc.value.code == "UNSUPPORTED_ACTIVE_CONTENT"
    with pytest.raises(MmiError) as exc:
        preflight_pdf(pdf_bytes(extra="/Encrypt"), MmiLimits())
    assert exc.value.code == "PASSWORD_REQUIRED"
    info = preflight_pdf(pdf_bytes(), MmiLimits())
    assert info.page_count == 1
    doc = PdfParser().parse(make_input("pdf", pdf_bytes()))
    assert doc.metadata["pageCount"] == 1


def test_pdf_native_text_and_page_locator() -> None:
    pytest.importorskip("pdfplumber")
    doc = PdfParser().parse(make_input("pdf", pdf_bytes("Price 100 yen", pages=2)))
    assert doc.partial is False
    assert doc.warnings == ()
    assert [b.text for b in doc.blocks()] == ["Price 100 yen", "Price 100 yen"]
    assert [b.locator.page1 for b in doc.blocks()] == [1, 2]
    assert all(b.locator.kind == "pdf" for b in doc.blocks())


def test_png_parse_records_container_without_inventing_text() -> None:
    doc = ImageParser().parse(make_input("png", png_bytes(3, 4)))
    image = doc.metadata["image"]
    assert image["container"] == "png"
    assert (image["widthPx"], image["heightPx"]) == (3, 4)
    assert doc.blocks() == []
    assert "LOCAL_OCR_DISABLED" in doc.warnings
    assert doc.sections[0].images[0].digest == hashlib.sha256(png_bytes(3, 4)).hexdigest()


def test_png_dimensions_and_pixel_limit() -> None:
    doc = ImageParser().parse(make_input("png", png_bytes(3, 4)))
    assert doc.metadata["image"]["widthPx"] == 3
    assert doc.metadata["image"]["heightPx"] == 4
    assert "LOCAL_OCR_DISABLED" in doc.warnings
    huge = bytearray(png_bytes(1, 1))
    huge[16:20] = (20_000).to_bytes(4, "big")
    with pytest.raises(MmiError) as exc:
        ImageParser().parse(make_input("png", bytes(huge)))
    assert exc.value.code == "PIXEL_LIMIT"


def test_money_tax_unknown_and_verification() -> None:
    data = 'item,price\nA,"¥ 12,800"\n'.encode()
    doc = CsvParser().parse(make_input("csv", data))
    extraction = extract_candidates(doc, ["amount", "effective_price", "tax"], locale="ja-JP")
    amount = next(c for c in extraction.candidates if c.field_key == "amount")
    assert amount.value_state == "observed"
    assert amount.normalized["amount"] == "12800"
    assert amount.normalized["currency"] == "JPY"
    assert amount.normalized["tax"] == "unknown"
    effective = next(c for c in extraction.candidates if c.field_key == "effective_price")
    assert effective.value_state == "unknown"
    assert effective.missing_reason == "UNKNOWN_TAX_TREATMENT"
    result = verify_candidates(doc, extraction.candidates, data, locale="ja-JP")
    verified_amount = next(c for c in result.candidates if c.field_key == "amount")
    assert verified_amount.verification == "anchored"
    assert result.contradicted == 0


def test_tax_included_allows_derived_effective_price() -> None:
    data = 'item,price\nA,"¥ 12,800 税込"\n'.encode()
    doc = CsvParser().parse(make_input("csv", data))
    extraction = extract_candidates(doc, ["amount", "effective_price"], locale="ja-JP")
    effective = next(c for c in extraction.candidates if c.field_key == "effective_price")
    assert effective.value_state == "derived"
    assert effective.normalized["amount"] == "12800"
    result = verify_candidates(doc, extraction.candidates, data, locale="ja-JP")
    assert all(c.verification == "anchored" for c in result.candidates)


def test_verification_detects_tampered_source() -> None:
    data = b"a,b\n1,2\n"
    doc = CsvParser().parse(make_input("csv", data))
    extraction = extract_candidates(doc, ["amount"])
    with pytest.raises(MmiError) as exc:
        verify_candidates(doc, extraction.candidates, b"a,b\n1,3\n")
    assert exc.value.code == "SOURCE_MISMATCH"


def test_contract_locator_and_canonical_key() -> None:
    locator = MmiSourceLocator(
        kind="office",
        artifact_id="a",
        source_sha256="0" * 64,
        format="docx",
        part_name="word/document.xml",
        paragraph1=1,
        physical_page="unknown",
    )
    assert locator.to_dict()["physicalPage"] == "unknown"
    with pytest.raises(ValueError):
        MmiSourceLocator(
            kind="office",
            artifact_id="a",
            source_sha256="0" * 64,
            format="docx",
            part_name="word/document.xml",
            physical_page="1",
        )
    from aiagent_multimodal.contracts import MmiRequestV1, MmiSource

    request = MmiRequestV1(
        source_artifact_id="a",
        source=MmiSource(kind="user_upload", acquired_at="2026-01-01T00:00:00Z"),
        requested_fields=("amount",),
        idempotency_key="k",
        deadline_at="2026-01-01T01:00:00Z",
    )
    base = canonical_parse_key(
        tenant_id="t",
        owner_id="u",
        source_artifact_id="a",
        source_sha256="0" * 64,
        request=request,
        policy_revision="p1",
        parser_build_digest="b1",
    )
    changed = canonical_parse_key(
        tenant_id="t",
        owner_id="u",
        source_artifact_id="a",
        source_sha256="0" * 64,
        request=request,
        policy_revision="p2",
        parser_build_digest="b1",
    )
    assert base != changed
    assert len(base) == 64


def test_ocr_candidate_is_never_anchored_by_self_reparse() -> None:
    data = b"not-a-real-pdf"
    sha = hashlib.sha256(data).hexdigest()
    locator = MmiSourceLocator(
        kind="pdf",
        artifact_id="art_ocr",
        source_sha256=sha,
        page1=1,
        layer="ocr",
        rotation_deg=0,
        normalized_bbox=(0.1, 0.1, 0.9, 0.2),
    )
    block = TextBlock(
        block_id="ocr:1",
        kind="paragraph",
        text="100\u5186",
        locator=locator,
        recognition_confidence_raw=99.0,
        extraction_method="ocr",
    )
    document = Document(
        source_artifact_id="art_ocr",
        source_sha256=sha,
        format="pdf",
        sections=[Section(section_id="page:1", kind="page", ordinal1=1, label="Page 1", locator=None, blocks=[block])],
        parser_revision="test",
        build_digest="test-build",
    )
    extraction = extract_candidates(document, ("amount",))
    result = verify_candidates(document, extraction.candidates, data, locale="ja-JP")
    assert result.candidates
    assert all(candidate.verification == "unverified" for candidate in result.candidates)
    assert result.anchored == 0


def test_pipeline_ignores_worker_candidate_text_and_reparses_source() -> None:
    from aiagent_multimodal.contracts import MmiRequestV1, MmiSource
    from aiagent_multimodal.pipeline import Pipeline

    data = b"amount,currency\n100,JPY\n"
    sha = hashlib.sha256(data).hexdigest()

    class PoisonedExecutor:
        def execute(self, inp: ParseInput, deadline: float) -> Document:
            return Document(
                source_artifact_id=inp.source_artifact_id,
                source_sha256=inp.source_sha256,
                format="csv",
                sections=[],
                parser_revision="poisoned",
                build_digest="poisoned",
            )

    pipeline = Pipeline(MmiLimits(), PoisonedExecutor())
    detection = pipeline.detect(data, file_name="amount.csv", declared_mime="text/csv")
    request = MmiRequestV1(
        source_artifact_id="art_worker",
        source=MmiSource(kind="user_upload", acquired_at="2026-01-01T00:00:00Z"),
        requested_fields=("amount",),
        idempotency_key="worker-trust",
        deadline_at="2026-01-01T00:05:00Z",
    )
    result = pipeline.process(
        source_artifact_id="art_worker",
        source_sha256=sha,
        raw=data,
        request=request,
        detection=detection,
    )
    # The poisoned worker document has no sections. A non-empty candidate set can only
    # come from the pipeline re-reading the immutable source bytes itself.
    amount = result.outcome["candidates"][0]
    assert amount["rawText"] == "100"
    assert amount["locator"]["kind"] == "csv"


def test_xlsx_cached_formula_value_is_estimated_and_unverified() -> None:
    data = xlsx_bytes(
        [["Item", "Price"], ["A", None]],
        formulas={"B2": ("1+1", "100\u5186")},
        formula_types={"B2": "str"},
    )
    doc = XlsxParser().parse(make_input("xlsx", data))
    extraction = extract_candidates(doc, ["amount"])
    candidate = next(c for c in extraction.candidates if c.raw_text == "100\u5186")
    assert candidate.value_state == "estimated"
    assert "formula_cached_value" in candidate.conditions
    result = verify_candidates(doc, [candidate], data, locale="ja-JP")
    assert result.candidates[0].verification == "unverified"


def test_normalized_value_without_replay_is_not_anchored() -> None:
    data = "price\n100\u5186\n".encode()
    doc = CsvParser().parse(make_input("csv", data))
    extraction = extract_candidates(doc, ["amount"])
    candidate = next(c for c in extraction.candidates if c.raw_text == "100\u5186")
    changed = dataclasses.replace(
        candidate,
        normalized={"schema": "mmi.money/1", "amount": "999", "currency": "JPY", "tax": "unknown"},
    )
    assert verify_candidates(doc, [changed], data).candidates[0].verification == "contradicted"
    no_replay = dataclasses.replace(candidate, field_key="quantity")
    assert verify_candidates(doc, [no_replay], data).candidates[0].verification == "unverified"


def test_date_replay_accepts_slash_form() -> None:
    data = b"date\n2026/11/27\n"
    doc = CsvParser().parse(make_input("csv", data))
    extraction = extract_candidates(doc, ["date"])
    result = verify_candidates(doc, extraction.candidates, data)
    assert result.anchored == 1


def test_concurrent_chunk_publish_does_not_overwrite(tmp_path) -> None:
    store = QuarantineBlobStore(tmp_path, max_file_bytes=1024)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def writer(payload: bytes) -> None:
        barrier.wait()
        try:
            digest = store.put_chunk(
                "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", 0, payload
            )
        except MmiError as exc:
            outcomes.append(("error", exc.code))
        else:
            outcomes.append(("ok", digest))

    threads = [threading.Thread(target=writer, args=(b"first",)), threading.Thread(target=writer, args=(b"second",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([o for o in outcomes if o[0] == "ok"]) == 1
    assert len([o for o in outcomes if o[0] == "error"]) == 1
    winner = next(o[1] for o in outcomes if o[0] == "ok")
    part = (
        tmp_path
        / "staging"
        / "11111111-1111-4111-8111-111111111111"
        / "22222222-2222-4222-8222-222222222222"
        / "00000.part"
    )
    assert hashlib.sha256(part.read_bytes()).hexdigest() == winner


def test_pdf_verification_is_limited_to_the_candidate_bbox() -> None:
    data = "page\n100\u5186\n".encode()
    sha = hashlib.sha256(data).hexdigest()
    page_locator = MmiSourceLocator(
        kind="pdf", artifact_id="art_bbox", source_sha256=sha, page1=1, layer="native", rotation_deg=0
    )
    block = TextBlock(
        block_id="p1:b1",
        kind="paragraph",
        text="100\u5186",
        locator=MmiSourceLocator(
            kind="pdf",
            artifact_id="art_bbox",
            source_sha256=sha,
            page1=1,
            layer="native",
            rotation_deg=0,
            normalized_bbox=(0.1, 0.1, 0.3, 0.2),
        ),
    )
    document = Document(
        source_artifact_id="art_bbox",
        source_sha256=sha,
        format="pdf",
        sections=[
            Section(section_id="page:1", kind="page", ordinal1=1, label="Page 1", locator=page_locator, blocks=[block])
        ],
        parser_revision="test",
        build_digest="test-build",
    )
    extraction = extract_candidates(document, ("amount",), locale="ja-JP")
    candidate = extraction.candidates[0]
    assert candidate.locator is not None
    outside = dataclasses.replace(
        candidate, locator=dataclasses.replace(candidate.locator, normalized_bbox=(0.7, 0.7, 0.9, 0.9))
    )
    result = verify_candidates(document, [outside], data)
    assert result.candidates[0].verification == "unverified"
    assert "bounding box" in result.candidates[0].provenance["verificationReason"]
