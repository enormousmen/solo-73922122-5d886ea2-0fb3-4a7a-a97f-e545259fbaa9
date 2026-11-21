"""Tests for the staging store: seeding, tickets, rejection rules, concurrency."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.staging import (
    BeforeMismatch,
    RequiredDomainsBroken,
    StagingStore,
    reset_store,
)

SEED = {
    "revision": 0,
    "zones": [
        {
            "name": "a.example.com",
            "records": [
                {
                    "name": "www.a.example.com",
                    "type": "CNAME",
                    "value": "svc.b.example.com",
                    "ttl": 60,
                }
            ],
        },
        {
            "name": "b.example.com",
            "records": [
                {"name": "svc.b.example.com", "type": "A", "value": "10.20.1.10", "ttl": 60}
            ],
        },
    ],
    "required_domains": ["www.a.example.com"],
}


def rec(name: str, rtype: str, value: str, ttl: int = 60) -> dict:
    return {"name": name, "type": rtype, "value": value, "ttl": ttl}


def change(name: str, before: list[dict], after: list[dict]) -> dict:
    return {"name": name, "before": before, "after": after}


def ticket(*changes: dict, ticket_id: str | None = None) -> dict:
    out: dict = {"changes": list(changes)}
    if ticket_id is not None:
        out["ticket_id"] = ticket_id
    return out


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(SEED), encoding="utf-8")
    snapshot = tmp_path / "data" / "staging-snapshot.json"
    monkeypatch.setenv("STAGING_SEED_PATH", str(seed))
    monkeypatch.setenv("STAGING_SNAPSHOT_PATH", str(snapshot))
    reset_store()
    yield {"seed": seed, "snapshot": snapshot}
    reset_store()


@pytest.fixture()
def client(paths):
    with TestClient(app) as c:
        yield c


def store_from(paths) -> StagingStore:
    return StagingStore(snapshot_path=paths["snapshot"], seed_path=paths["seed"])


def snapshot_on_disk(paths) -> dict:
    return json.loads(paths["snapshot"].read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Seeding and restart behaviour
# --------------------------------------------------------------------------- #


def test_first_boot_seeds_from_sample_and_persists(paths, client) -> None:
    assert not paths["snapshot"].exists()
    out = client.get("/staging/snapshot")
    assert out.status_code == 200
    body = out.json()
    assert body["revision"] == 0
    assert [z["name"] for z in body["zones"]] == ["a.example.com", "b.example.com"]
    assert body["required_domains"] == ["www.a.example.com"]
    assert body["all_required_resolvable"] is True
    assert body["required_resolution"] == {"www.a.example.com": ["10.20.1.10"]}
    assert body["unresolved"] == []
    # First boot persisted the seed as the saved snapshot.
    assert snapshot_on_disk(paths)["revision"] == 0


def test_restart_reuses_saved_snapshot_not_seed(paths, client) -> None:
    add = change("svc2.b.example.com", [], [rec("svc2.b.example.com", "A", "10.20.2.10")])
    assert client.post("/staging/tickets", json=ticket(add)).status_code == 200

    # Simulate a restart: drop the in-process store, even delete the seed.
    reset_store()
    paths["seed"].unlink()
    with TestClient(app) as restarted:
        body = restarted.get("/staging/snapshot").json()
    assert body["revision"] == 1
    names = {r["name"] for z in body["zones"] for r in z["records"]}
    assert "svc2.b.example.com" in names


def test_store_level_restart_keeps_applied_tickets(paths) -> None:
    store = store_from(paths)
    store.apply_ticket(
        _ticket_model(change("svc2.b.example.com", [], [rec("svc2.b.example.com", "A", "10.20.2.10")]))
    )
    reloaded = store_from(paths)
    snap = reloaded.get_snapshot()
    assert snap.revision == 1
    names = {r.name for z in snap.zones for r in z.records}
    assert "svc2.b.example.com" in names


def _ticket_model(*changes: dict):
    from app.models import ChangeTicket

    return ChangeTicket.model_validate(ticket(*changes))


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_add_record_then_flip_cname(paths, client) -> None:
    add = change("svc2.b.example.com", [], [rec("svc2.b.example.com", "A", "10.20.2.10")])
    out = client.post("/staging/tickets", json=ticket(add, ticket_id="CHG-1"))
    assert out.status_code == 200
    body = out.json()
    assert body["status"] == "applied"
    assert body["ticket_id"] == "CHG-1"
    assert body["revision"] == 1
    assert body["applied_changes"] == ["svc2.b.example.com"]
    assert body["snapshot"]["all_required_resolvable"] is True

    flip = change(
        "www.a.example.com",
        [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
        [rec("www.a.example.com", "CNAME", "svc2.b.example.com")],
    )
    out = client.post("/staging/tickets", json=ticket(flip, ticket_id="CHG-2"))
    assert out.status_code == 200
    body = out.json()
    assert body["revision"] == 2
    assert body["snapshot"]["required_resolution"] == {
        "www.a.example.com": ["10.20.2.10"]
    }
    assert snapshot_on_disk(paths)["revision"] == 2


def test_before_comparison_ignores_order(paths, client) -> None:
    # Stage two A records at one name.
    add = change(
        "pool.b.example.com",
        [],
        [
            rec("pool.b.example.com", "A", "10.20.3.1"),
            rec("pool.b.example.com", "A", "10.20.3.2"),
        ],
    )
    assert client.post("/staging/tickets", json=ticket(add)).status_code == 200
    # Submit the before set in the opposite order: still a match.
    update = change(
        "pool.b.example.com",
        [
            rec("pool.b.example.com", "A", "10.20.3.2"),
            rec("pool.b.example.com", "A", "10.20.3.1"),
        ],
        [rec("pool.b.example.com", "A", "10.20.3.3")],
    )
    out = client.post("/staging/tickets", json=ticket(update))
    assert out.status_code == 200


def test_multi_change_ticket_applies_atomically(paths, client) -> None:
    both = ticket(
        change("svc2.b.example.com", [], [rec("svc2.b.example.com", "A", "10.20.2.10")]),
        change(
            "www.a.example.com",
            [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
            [rec("www.a.example.com", "CNAME", "svc2.b.example.com")],
        ),
    )
    out = client.post("/staging/tickets", json=both)
    assert out.status_code == 200
    body = out.json()
    assert body["revision"] == 1
    assert body["snapshot"]["required_resolution"] == {
        "www.a.example.com": ["10.20.2.10"]
    }


# --------------------------------------------------------------------------- #
# Rejections: nothing is persisted
# --------------------------------------------------------------------------- #


def test_stale_before_rejected_with_conflicting_name(paths, client) -> None:
    stale = change(
        "www.a.example.com",
        [rec("www.a.example.com", "CNAME", "old-svc.b.example.com")],
        [rec("www.a.example.com", "CNAME", "svc2.b.example.com")],
    )
    out = client.post("/staging/tickets", json=ticket(stale))
    assert out.status_code == 409
    detail = out.json()["detail"]
    assert detail["status"] == "rejected"
    assert detail["reason"] == "before_mismatch"
    assert detail["conflicting_names"] == ["www.a.example.com"]
    conflict = detail["conflicts"][0]
    assert conflict["current"] == [
        rec("www.a.example.com", "CNAME", "svc.b.example.com")
    ]
    # Nothing was persisted.
    assert snapshot_on_disk(paths)["revision"] == 0


def test_two_tickets_same_chain_second_conflicts(paths, client) -> None:
    """The motivating case: two tickets touching the same CNAME chain."""
    first = change(
        "www.a.example.com",
        [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
        [rec("www.a.example.com", "A", "10.20.9.9")],
    )
    second = change(
        "www.a.example.com",
        [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
        [rec("www.a.example.com", "A", "10.20.9.10")],
    )
    assert client.post("/staging/tickets", json=ticket(first)).status_code == 200
    out = client.post("/staging/tickets", json=ticket(second))
    assert out.status_code == 409
    assert out.json()["detail"]["conflicting_names"] == ["www.a.example.com"]
    assert snapshot_on_disk(paths)["revision"] == 1


def test_ticket_is_all_or_nothing(paths, client) -> None:
    mixed = ticket(
        change("svc2.b.example.com", [], [rec("svc2.b.example.com", "A", "10.20.2.10")]),
        change(
            "www.a.example.com",
            [rec("www.a.example.com", "CNAME", "stale.b.example.com")],  # stale
            [rec("www.a.example.com", "CNAME", "svc2.b.example.com")],
        ),
    )
    out = client.post("/staging/tickets", json=mixed)
    assert out.status_code == 409
    # The valid half was not applied either.
    snap = client.get("/staging/snapshot").json()
    names = {r["name"] for z in snap["zones"] for r in z["records"]}
    assert "svc2.b.example.com" not in names
    assert snapshot_on_disk(paths)["revision"] == 0


def test_zone_validation_reused_for_tickets(paths, client) -> None:
    bad = change(
        "www.a.example.com",
        [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
        [
            rec("www.a.example.com", "A", "10.20.9.9"),
            rec("www.a.example.com", "CNAME", "svc.b.example.com"),
        ],
    )
    out = client.post("/staging/tickets", json=ticket(bad))
    assert out.status_code == 422
    detail = out.json()["detail"]
    assert detail["reason"] == "zone_validation_failed"
    assert any("both A and CNAME" in e for e in detail["errors"])
    assert snapshot_on_disk(paths)["revision"] == 0


def test_apex_cname_rejected_via_shared_validation(paths, client) -> None:
    bad = change(
        "a.example.com",
        [],
        [rec("a.example.com", "CNAME", "svc.b.example.com")],
    )
    out = client.post("/staging/tickets", json=ticket(bad))
    assert out.status_code == 422
    assert any("apex" in e for e in out.json()["detail"]["errors"])


def test_breaking_required_domain_rejected_with_fault_chain(paths, client) -> None:
    remove = change("svc.b.example.com", [rec("svc.b.example.com", "A", "10.20.1.10")], [])
    out = client.post("/staging/tickets", json=ticket(remove))
    assert out.status_code == 409
    detail = out.json()["detail"]
    assert detail["reason"] == "required_domains_unresolvable"
    chain = detail["fault_chains"][0]
    assert chain["domain"] == "www.a.example.com"
    assert chain["category"] == "missing_record"
    assert "svc.b.example.com" in chain["chain"]
    assert snapshot_on_disk(paths)["revision"] == 0


def test_flip_to_missing_target_rejected(paths, client) -> None:
    flip = change(
        "www.a.example.com",
        [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
        [rec("www.a.example.com", "CNAME", "svc2.b.example.com")],
    )
    out = client.post("/staging/tickets", json=ticket(flip))
    assert out.status_code == 409
    assert out.json()["detail"]["fault_chains"][0]["category"] == "missing_record"


def test_change_outside_staged_zones_rejected(paths, client) -> None:
    out = client.post(
        "/staging/tickets",
        json=ticket(change("www.other.example.org", [], [rec("www.other.example.org", "A", "10.0.0.1")])),
    )
    assert out.status_code == 422
    assert out.json()["detail"]["reason"] == "name_outside_zones"


def test_malformed_tickets_are_422(client) -> None:
    # Inner record name differs from the change name.
    out = client.post(
        "/staging/tickets",
        json={
            "changes": [
                {
                    "name": "www.a.example.com",
                    "before": [],
                    "after": [rec("other.a.example.com", "A", "10.0.0.1")],
                }
            ]
        },
    )
    assert out.status_code == 422
    # Duplicate change names in one ticket.
    dup = {
        "changes": [
            {"name": "www.a.example.com", "before": [], "after": []},
            {"name": "www.a.example.com", "before": [], "after": []},
        ]
    }
    assert client.post("/staging/tickets", json=dup).status_code == 422
    # Empty ticket.
    assert client.post("/staging/tickets", json={"changes": []}).status_code == 422


# --------------------------------------------------------------------------- #
# Concurrency: the required-domain rule holds under racing submissions
# --------------------------------------------------------------------------- #


def test_concurrent_identical_tickets_exactly_one_applies(paths, client) -> None:
    payload = ticket(
        change(
            "www.a.example.com",
            [rec("www.a.example.com", "CNAME", "svc.b.example.com")],
            [rec("www.a.example.com", "A", "10.20.9.9")],
        )
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.post("/staging/tickets", json=payload), range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1
    assert codes.count(409) == 7
    assert snapshot_on_disk(paths)["revision"] == 1


def test_concurrent_distinct_tickets_all_apply(paths, client) -> None:
    def submit(i: int):
        payload = ticket(
            change(f"h{i}.b.example.com", [], [rec(f"h{i}.b.example.com", "A", f"10.20.4.{i}")])
        )
        return client.post("/staging/tickets", json=payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in results)
    final = client.get("/staging/snapshot").json()
    assert final["revision"] == 8
    names = {r["name"] for z in final["zones"] for r in z["records"]}
    assert {f"h{i}.b.example.com" for i in range(8)} <= names


def test_concurrent_unsafe_ticket_never_lands(paths, client) -> None:
    """A ticket that would break the chain must lose every race it runs."""
    unsafe = ticket(
        change("svc.b.example.com", [rec("svc.b.example.com", "A", "10.20.1.10")], [])
    )
    safe = ticket(
        change("svc2.b.example.com", [], [rec("svc2.b.example.com", "A", "10.20.2.10")])
    )

    def submit(i: int):
        return client.post("/staging/tickets", json=unsafe if i % 2 else safe)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert all(r.status_code in (200, 409) for r in results)
    # Every unsafe submission was rejected; only safe ones persisted.
    snap = client.get("/staging/snapshot").json()
    names = {r["name"] for z in snap["zones"] for r in z["records"]}
    assert "svc.b.example.com" in names
    assert snap["all_required_resolvable"] is True


# --------------------------------------------------------------------------- #
# Store-level guards
# --------------------------------------------------------------------------- #


def test_corrupt_snapshot_fails_loudly(paths) -> None:
    paths["snapshot"].parent.mkdir(parents=True, exist_ok=True)
    paths["snapshot"].write_text("{not json", encoding="utf-8")
    store = store_from(paths)
    with pytest.raises(Exception):
        store.get_snapshot()


def test_store_rejection_types_carry_details(paths) -> None:
    store = store_from(paths)
    with pytest.raises(BeforeMismatch) as exc:
        store.apply_ticket(
            _ticket_model(
                change(
                    "www.a.example.com",
                    [rec("www.a.example.com", "CNAME", "stale.b.example.com")],
                    [rec("www.a.example.com", "A", "10.0.0.1")],
                )
            )
        )
    assert exc.value.conflicts[0]["name"] == "www.a.example.com"

    with pytest.raises(RequiredDomainsBroken) as exc2:
        store.apply_ticket(
            _ticket_model(
                change("svc.b.example.com", [rec("svc.b.example.com", "A", "10.20.1.10")], [])
            )
        )
    assert exc2.value.failures[0].domain == "www.a.example.com"


def test_openapi_documents_staging_endpoints(client) -> None:
    spec = client.get("/openapi.json").json()
    assert "/staging/snapshot" in spec["paths"]
    assert "/staging/tickets" in spec["paths"]
