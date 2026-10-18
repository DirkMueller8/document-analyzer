from pathlib import Path

import pytest

from ingest.odf_crypto import WrongPassword, decrypt_odt, is_encrypted
from ingest.odt_parser import extract_blocks

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
PASSWORD = "Synthetic-Test-Key-42"  # synthetic fixtures only
# Both fixtures were produced by LibreOffice 26.2 (samples/tools/lo_encrypt.py)
FIXTURES = ["sample_entries_encrypted.odt", "sample_entries_encrypted_odf12.odt"]


@pytest.mark.parametrize("name", FIXTURES)
def test_decrypts_to_same_blocks_as_plain(name):
    path = SAMPLES / name
    assert is_encrypted(path)
    plain = [(b.text, b.kind) for b in extract_blocks(SAMPLES / "sample_entries.odt")]
    assert [(b.text, b.kind) for b in extract_blocks(decrypt_odt(path, PASSWORD))] == plain


@pytest.mark.parametrize("name", FIXTURES)
def test_wrong_password(name):
    with pytest.raises(WrongPassword):
        decrypt_odt(SAMPLES / name, "falsch")


def test_plain_file_passes_through():
    assert not is_encrypted(SAMPLES / "sample_entries.odt")
    assert extract_blocks(decrypt_odt(SAMPLES / "sample_entries.odt", ""))


def test_open_document_and_full_pipeline(tmp_path):
    from ingest.run import open_document
    from ingest.segment import segment
    from ingest.store import connect, sync_entries

    blocks = extract_blocks(open_document(SAMPLES / FIXTURES[0], PASSWORD))
    stats = sync_entries(connect(tmp_path / "t.db"), segment(blocks))
    assert stats.added == 15
    with pytest.raises(WrongPassword):
        open_document(SAMPLES / FIXTURES[0], "falsch")
