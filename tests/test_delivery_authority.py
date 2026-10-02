"""Ownership revocation must prevent integration in the standard workflow."""

from types import SimpleNamespace

import pytest

from backlog_harness.delivery import integrate
from backlog_harness.provider import TransitionBlocked, git


@pytest.mark.parametrize("change", ["owner", "revision"])
def test_delivery_revalidates_authority_before_merge(provider, tmp_path, change):
    item = provider.item("item-one")
    source = provider.repository / item.path
    source.write_text(
        source.read_text()
        .replace("Status: Ready", "Status: Running")
        .replace("Owner: Unowned", "Owner: owner")
    )
    git(provider.repository, "add", item.path)
    git(provider.repository, "commit", "-m", "Running item")
    running = provider.item("item-one")
    source.write_text(
        source.read_text().replace("Owner: owner", "Owner: replacement")
        if change == "owner"
        else source.read_text() + "\nChanged authoritative requirement.\n"
    )
    git(provider.repository, "add", item.path)
    git(provider.repository, "commit", "-m", "Revoke delivery authority")
    before = git(provider.repository, "rev-parse", "HEAD")
    app = SimpleNamespace(
        provider=provider,
        config=SimpleNamespace(repository=provider.repository),
        _stage_path=lambda item_id, stage: tmp_path / "evidence" / (stage + ".json"),
    )
    with pytest.raises(TransitionBlocked, match="ownership or revision"):
        integrate(
            app,
            "item-one",
            tmp_path / "candidate",
            "candidate",
            "base",
            {},
            [],
            expected_owner=running.owner,
            expected_revision=running.revision,
        )
    assert git(provider.repository, "rev-parse", "HEAD") == before
    assert not app._stage_path("item-one", "delivery").exists()
    assert provider.item("item-one").state == "Running"
