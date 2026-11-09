from pathlib import Path

import pytest
from flask.testing import FlaskClient
from test_picture import LEDGER

from tally import advice, web


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "ledger"
    (folder / "imported").mkdir(parents=True)
    head, body = LEDGER.split("\n2026-02-14", 1)
    (folder / "main.beancount").write_text(
        'option "title" "My Money"\n'
        + head
        + 'include "imported/*.beancount"\ninclude "pending.beancount"\n'
    )
    (folder / "imported" / "2026.beancount").write_text("2026-02-14" + body)
    return tmp_path


def client(data_dir: Path, fava_port: int | None = 8443) -> FlaskClient:
    app = web.create_app(data_dir, fava_port)
    app.testing = True
    return app.test_client()


def test_the_page_links_to_fava_on_the_same_host(data_dir: Path) -> None:
    page = client(data_dir).get("/", base_url="https://box.example.ts.net")
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'href="//box.example.ts.net:8443/my-money/account/Expenses:Groceries/"' in text
    assert "Needs a category (1)" in text
    # Without a port, Fava's pages are linked on the app's own address.
    text = client(data_dir, None).get("/").get_data(as_text=True)
    assert 'href="/my-money/income_statement/"' in text


def test_picking_a_category_and_asking(data_dir: Path) -> None:
    c = client(data_dir)
    assert c.post("/categorize", data={"tally_id": "a5", "category": "Nope"}).status_code == 400
    done = c.post("/categorize", data={"tally_id": "a5", "category": "Groceries"})
    assert done.status_code == 303
    assert (
        "  Expenses:Groceries  40.00 USD"
        in (data_dir / "ledger" / "imported" / "2026.beancount").read_text()
    )
    # The page loads the changed file again.
    assert "Every purchase has a category." in c.get("/").get_data(as_text=True)

    assert c.post("/ask", data={"question": " "}).status_code == 400
    assert c.post("/ask", data={"question": "Am I on track?"}).status_code == 303
    assert [q.text for q in advice.questions(data_dir)] == ["Am I on track?"]


def test_ledger_errors_are_shown(data_dir: Path) -> None:
    path = data_dir / "ledger" / "imported" / "2026.beancount"
    path.write_text(path.read_text() + "\n2026-03-07 balance Assets:Checking  1.00 USD\n")
    text = client(data_dir).get("/").get_data(as_text=True)
    assert "The ledger has 1 error" in text
