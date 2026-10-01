"""Independent regression: ownership revocation must precede primary integration."""

import asyncio

import pytest
from test_item_graph import make_pilot

from backlog_harness.item_graph import run_item_graph
from backlog_harness.provider import TransitionBlocked, git


@pytest.mark.parametrize("change", ["owner", "revision"])
def test_graph_delivery_revalidates_authority_before_merge(provider, tmp_path, monkeypatch, change):
    app = make_pilot(provider, tmp_path, monkeypatch)
    app.question = False
    checks = app.checks
    changed = False

    def revoke(*args):
        nonlocal changed
        result = checks(*args)
        if not changed:
            changed = True
            item = provider.item("item-one")
            source = provider.repository / item.path
            content = source.read_text()
            source.write_text(
                content.replace("Owner: owner", "Owner: replacement")
                if change == "owner"
                else content + "\nChanged authoritative requirement.\n"
            )
            git(provider.repository, "add", item.path)
            git(provider.repository, "commit", "-m", "Revoke reviewed delivery authority")
        return result

    app.checks = revoke
    with pytest.raises(TransitionBlocked, match="ownership or revision"):
        asyncio.run(run_item_graph(app, "item-one"))
    assert not (provider.repository / "answer.py").exists()
    assert not app._stage_path("item-one", "delivery").exists()
    assert provider.item("item-one").state == "Running"
