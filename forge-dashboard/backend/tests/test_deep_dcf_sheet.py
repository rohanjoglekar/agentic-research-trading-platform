"""The deep-DCF sheet reader: values out of a recalculated .xlsx, stdlib only."""
import json
import zipfile

import pytest

from adapters import deep_dcf

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
RNS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _xlsx(path, cells_by_sheet, shared=("Revenue", "WACC (active)")):
    sheets = "".join(f'<sheet name="{n}" sheetId="{i+1}" r:id="rId{i+1}"/>' for i, n in enumerate(cells_by_sheet))
    rels = "".join(f'<Relationship Id="rId{i+1}" Type="x" Target="worksheets/sheet{i+1}.xml"/>' for i, _ in enumerate(cells_by_sheet))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {NS} {RNS}><sheets>{sheets}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {NS}>' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        for i, cells in enumerate(cells_by_sheet.values()):
            z.writestr(f"xl/worksheets/sheet{i+1}.xml", f'<worksheet {NS}><sheetData>{cells}</sheetData></worksheet>')


def test_reads_shared_strings_numbers_inline_and_formula_strings(tmp_path):
    p = tmp_path / "m.xlsx"
    _xlsx(p, {"DCF": (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>=1+1</f><v>346445</v></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Base case</t></is></c><c r="B2" t="str"><v>Base</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>1</v></c><c r="B3"><v>0.1061</v></c><c r="C3"/></row>'
        '<row r="500"><c r="A500"><v>9</v></c></row>'          # beyond MAX_ROWS: dropped
        '<row r="4"><c r="AB4"><v>1</v></c></row>'             # beyond column Z: dropped
    ), "WACC": '<row r="1"><c r="A1" t="b"><v>1</v></c></row>'})
    out = deep_dcf.read_workbook_values(p)
    assert [s["name"] for s in out] == ["DCF", "WACC"]
    assert out[0]["rows"] == [
        {"r": 1, "c": {"A": "Revenue", "B": 346445.0}},
        {"r": 2, "c": {"A": "Base case", "B": "Base"}},
        {"r": 3, "c": {"A": "WACC (active)", "B": 0.1061}},
    ]
    assert out[1]["rows"] == [{"r": 1, "c": {"A": True}}]
    json.dumps(out)                                           # serialisable as-is


def test_sheet_serves_only_indexed_models_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(deep_dcf.config, "DEEP_DCF_DIR", tmp_path)
    deep_dcf._sheet_cache.clear()
    (tmp_path / "models" / "AAA" / "r1").mkdir(parents=True)
    x = tmp_path / "models" / "AAA" / "r1" / "AAA_DCF_2026-11-15.xlsx"
    _xlsx(x, {"DCF": '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'})
    (tmp_path / "latest.json").write_text(json.dumps({"models": {
        "AAA": {"symbol": "AAA", "as_of": "2026-11-15", "file": "models/AAA/r1/AAA_DCF_2026-11-15.xlsx"},
        "EVIL": {"symbol": "EVIL", "file": "../../etc/passwd.xlsx"},
    }}))
    got = deep_dcf.sheet("aaa")
    assert got["symbol"] == "AAA" and got["as_of"] == "2026-11-15"
    assert got["sheets"][0]["rows"][0]["c"]["A"] == "Revenue"
    assert deep_dcf.sheet("AAA") is got                       # cached by file + mtime
    assert deep_dcf.sheet("EVIL") is None                     # outside the models dir
    assert deep_dcf.sheet("NOPE") is None
    assert deep_dcf.sheet("../x") is None


def test_the_public_sheet_comes_from_the_public_index(tmp_path, monkeypatch):
    """OWNA was built for the account and AAA was rebuilt for it later. The
    public reads neither of those: no sheet for OWNA, the earlier one for AAA."""
    monkeypatch.setattr(deep_dcf.config, "DEEP_DCF_DIR", tmp_path)
    deep_dcf._sheet_cache.clear()
    files = {"models/AAA/r1/AAA_DCF_2026-10-15.xlsx": ("Earlier model",),
             "models/AAA/r2/AAA_DCF_2026-11-26.xlsx": ("Model built for the account",),
             "models/OWNA/r1/OWNA_DCF_2026-11-15.xlsx": ("Model built for the account",)}
    for rel, shared in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        _xlsx(tmp_path / rel, {"DCF": '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'}, shared=shared)
    (tmp_path / "latest.json").write_text(json.dumps({"models": {
        "AAA": {"symbol": "AAA", "as_of": "2026-11-26", "file": "models/AAA/r2/AAA_DCF_2026-11-26.xlsx"},
        "OWNA": {"symbol": "OWNA", "as_of": "2026-11-15", "file": "models/OWNA/r1/OWNA_DCF_2026-11-15.xlsx"}}}))
    (tmp_path / "public.json").write_text(json.dumps({"models": {
        "AAA": {"symbol": "AAA", "as_of": "2026-10-15", "file": "models/AAA/r1/AAA_DCF_2026-10-15.xlsx"}}}))
    own = deep_dcf.sheet("AAA")
    assert own["as_of"] == "2026-11-26" and own["sheets"][0]["rows"][0]["c"]["A"] == "Model built for the account"
    pub = deep_dcf.sheet("AAA", view_only=True)                # asked after the owner: no shared cache entry
    assert pub["as_of"] == "2026-10-15" and pub["sheets"][0]["rows"][0]["c"]["A"] == "Earlier model"
    assert deep_dcf.sheet("OWNA") is not None
    assert deep_dcf.sheet("OWNA", view_only=True) is None


def test_corrupt_workbook_is_absent_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(deep_dcf.config, "DEEP_DCF_DIR", tmp_path)
    deep_dcf._sheet_cache.clear()
    (tmp_path / "models" / "AAA").mkdir(parents=True)
    (tmp_path / "models" / "AAA" / "AAA_DCF.xlsx").write_bytes(b"not a zip")
    (tmp_path / "latest.json").write_text(json.dumps({"models": {"AAA": {"file": "models/AAA/AAA_DCF.xlsx"}}}))
    assert deep_dcf.sheet("AAA") is None


def test_oversized_part_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "big.xlsx"
    _xlsx(p, {"DCF": '<row r="1"><c r="A1"><v>1</v></c></row>'})
    monkeypatch.setattr(deep_dcf, "MAX_XML_BYTES", 10)
    with pytest.raises(ValueError):
        deep_dcf.read_workbook_values(p)


def test_model_names_and_private_rows_never_leave_the_server(tmp_path, monkeypatch):
    monkeypatch.setattr(deep_dcf.config, "DEEP_DCF_DIR", tmp_path)
    deep_dcf._sheet_cache.clear()
    (tmp_path / "models" / "ACME" / "r1").mkdir(parents=True)
    x = tmp_path / "models" / "ACME" / "r1" / "ACME_DCF.xlsx"
    _xlsx(x, {
        "DCF": '<row r="2"><c r="A2" t="inlineStr"><is><t>Built 2026-11-16; assumptions by claude-opus-5-5. Blue = input.</t></is></c></row>'
               '<row r="3"><c r="A3" t="s"><v>0</v></c><c r="C3"><v>1</v></c></row>'
               '<row r="4"><c r="A4" t="inlineStr"><is><t>Cost of equity (CAPM)</t></is></c><c r="C4"><v>0.12</v></c></row>',
        "Notes": '<row r="1"><c r="A1" t="inlineStr"><is><t>Assumptions by</t></is></c>'
                 '<c r="B1" t="inlineStr"><is><t>claude-opus-5-5 via the Message Batches API</t></is></c></row>'
                 '<row r="2"><c r="A2" t="inlineStr"><is><t>Method</t></is></c>'
                 '<c r="B2" t="inlineStr"><is><t>Ten-year DCF; reviewed by Opus 5.5 and Sonnet</t></is></c></row>',
    })
    (tmp_path / "latest.json").write_text(json.dumps({"models": {"ACME": {"file": "models/ACME/r1/ACME_DCF.xlsx"}}}))
    got = deep_dcf.sheet("ACME")
    text = json.dumps(got).lower()
    for word in ("claude", "opus", "sonnet", "anthropic"):
        assert word not in text
    notes = got["sheets"][1]["rows"]
    assert [r["c"]["A"] for r in notes] == ["Method"]
    assert got["sheets"][0]["rows"][1]["c"] == {"A": "Revenue", "C": 1.0}
    assert got["sheets"][0]["rows"][2]["c"] == {"A": "Cost of equity (CAPM)", "C": 0.12}   # finance rows survive
