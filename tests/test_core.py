from outreach.security import unsubscribe_token, valid_unsubscribe_token
from outreach.service import contact_role, normalize_email, render


def test_email_normalization():
    assert normalize_email("Contato <Contato@Empresa.com.br>") == "contato@empresa.com.br"
    assert normalize_email("inválido") is None


def test_contact_roles():
    assert contact_role("vendas@empresa.com.br") == "sales"
    assert contact_role("fiscal@empresa.com.br") == "finance"


def test_render():
    assert render("Olá, {empresa}", {"trade_name": "Loja X"}) == "Olá, Loja X"


def test_unsubscribe_signature():
    token = unsubscribe_token(42, "a@b.com", "secret")
    assert valid_unsubscribe_token(42, "a@b.com", token, "secret")
    assert not valid_unsubscribe_token(43, "a@b.com", token, "secret")
