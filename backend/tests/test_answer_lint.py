from app.agent.answer_lint import apply_lint


def _claim(kind, value, label="x"):
    return {
        "kind": kind,
        "value": value,
        "label": label,
        "tool": "t",
        "target": "A",
    }


def test_no_append_when_present():
    ledger = [{"ok": True, "claims": [_claim("id", "ABC123")]}]
    text = "Done: ABC123"
    new, appended, missing = apply_lint(text, ledger)
    assert appended == ""
    assert missing == []
    assert new == text


def test_append_missing():
    ledger = [{"ok": True, "claims": [_claim("id", "ABC123")]}]
    new, appended, missing = apply_lint("Done.", ledger)
    assert "ABC123" in appended
    assert new.startswith("Done.")
    assert len(missing) == 1


def test_amount_accepts_currency_format():
    ledger = [{"ok": True, "claims": [_claim("amount", "1288.65", "total")]}]
    _, appended, missing = apply_lint("Total is $1,288.65.", ledger)
    assert appended == ""
    assert missing == []


def test_amount_accepts_integer_form():
    ledger = [{"ok": True, "claims": [_claim("amount", "1628.00", "total")]}]
    _, appended, missing = apply_lint("Total 1628", ledger)
    assert appended == ""
    assert missing == []


def test_empty_ledger_is_noop():
    text = "hello"
    assert apply_lint(text, []) == (text, "", [])


def test_summary_is_capped():
    text = "Done."
    claims = [_claim("id", f"ID{i:03d}") for i in range(200)]
    _, appended, _ = apply_lint(text, [{"ok": True, "claims": claims}])
    assert len(appended) <= 700


def test_payment_claim_needs_full_id_and_amount():
    ledger = [
        {
            "ok": True,
            "claims": [
                _claim("payment", "gift_card_8516878 $128.00", "payment")
            ],
        }
    ]
    _, appended, missing = apply_lint(
        "Charged $128 to card ending 8516878.", ledger
    )
    assert "gift_card_8516878 $128.00" in appended
    assert len(missing) == 1


def test_payment_claim_passes_when_both_present():
    ledger = [
        {
            "ok": True,
            "claims": [
                _claim("payment", "gift_card_8516878 $128.00", "payment")
            ],
        }
    ]
    _, appended, missing = apply_lint(
        "Charged $128 to gift_card_8516878.", ledger
    )
    assert appended == ""
    assert missing == []
