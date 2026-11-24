"""The life of a Fund Request after the money went out (migration 144): the pure rules, no database."""
from datetime import date
from decimal import Decimal

import pytest

from app.fund_lifecycle import after_paid, difference, missing_proof, vendor_problems

TODAY = date(2026, 11, 24)


def test_paid_is_done_at_once_without_proof_after():
    assert after_paid({"proofAfter": False}, TODAY) == ("DONE", None)


def test_paid_waits_for_the_proof_within_the_days_set():
    assert after_paid({"proofAfter": True, "proofDays": 7}, TODAY) == ("PAID", date(2026, 12, 1))


def test_difference_of_the_accepted_amount():
    assert difference(1_000_000, None, 850_000) == Decimal(-150_000)  # money to come back
    assert difference(1_000_000, None, 1_200_000) == Decimal(200_000)  # Finance still owes
    assert difference(1_000_000, 200_000, 1_200_000) == Decimal(0)  # shortfall paid: even


def test_proof_needs_a_file_each_document_asked_and_the_amount():
    rule = {"proofDocs": ["INVOICE", "FOTO"]}
    assert len(missing_proof(rule, [], None)) == 2
    files = [{"kind": "PROOF", "docType": "INVOICE"}]
    assert missing_proof(rule, files, 100) == ["Lampirkan foto barang diterima"]
    files.append({"kind": "PROOF", "docType": "FOTO"})
    assert missing_proof(rule, files, 100) == []
    assert missing_proof(rule, [{"kind": "OTHER", "docType": "INVOICE"}], 100)[0].startswith("Lampirkan bukti")


@pytest.mark.parametrize("due, errors, warns", [
    (None, 1, 0), (date(2026, 11, 23), 1, 0), (date(2026, 11, 25), 0, 1), (date(2026, 12, 10), 0, 0)])
def test_vendor_due_date(due, errors, warns):
    files = [{"kind": "OTHER", "docType": "INVOICE"}]
    e, w = vendor_problems({"vendor": True}, due, "INV-1", files, TODAY, 3)
    assert (len(e), len(w)) == (errors, warns)


def test_vendor_needs_invoice_number_and_file():
    e, _ = vendor_problems({"vendor": True}, date(2026, 12, 10), "", [], TODAY, 3)
    assert e == ["Isi nomor invoice vendor", "Lampirkan file invoice vendor"]
    assert vendor_problems({"vendor": False}, None, None, [], TODAY, 3) == ([], [])


def test_payee_bank_or_virtual_account():
    from app.de_funds import payee_of
    from app.damaged_equipment import DeError
    assert payee_of({"type": "VA", "bank": "Shopee", "account": "8808123"}) == ("Shopee", "8808123", "Shopee", "VA")
    assert payee_of({"bank": "BCA", "account": "123", "name": "Toko A"})[3] == "BANK"
    with pytest.raises(DeError):
        payee_of({"bank": "BCA", "account": "123"})
    with pytest.raises(DeError):
        payee_of({"type": "VA", "account": "8808123"})
