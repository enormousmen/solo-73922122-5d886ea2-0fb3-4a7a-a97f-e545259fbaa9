"""Persistent staging store for DNS change tickets.

The store holds one *snapshot*: the current zone records plus the required
domains that must stay resolvable.  On first boot the snapshot is seeded from
a local sample file and immediately persisted; later boots reuse the saved
snapshot, so restarts never lose applied tickets.

A change ticket replaces, per record name, the whole A/CNAME RRset
(``before`` -> ``after``).  Applying a ticket is a single critical section:

1. every ``before`` must equal the currently staged RRset at that name
   (compared order-insensitively) — otherwise the ticket is based on a stale
   view and is rejected with the conflicting names;
2. the merged zones must pass the same record validation as /plan;
3. every required domain must still resolve to an IPv4 address under the
   cache-less authoritative resolver;
4. only then is the whole ticket persisted (atomically) as one snapshot.

A process-wide lock serialises concurrent submissions, so rule 3 holds even
when tickets race: each of them is checked against the latest snapshot.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .models import (
    ChangeTicket,
    Record,
    Snapshot,
    Zone,
    check_zone_records,
    zone_covering,
)
from .resolver import Resolution, Resolver

# The seed ships inside the package so both local runs and the container image
# find it without configuration; the live snapshot defaults to ./data and is
# overridden via STAGING_SNAPSHOT_PATH (the compose file points it at a
# persistent volume).
DEFAULT_SEED_PATH = Path(__file__).resolve().parent / "seed" / "staging-seed.json"
DEFAULT_SNAPSHOT_PATH = Path("data/staging-snapshot.json")

SEED_PATH_ENV = "STAGING_SEED_PATH"
SNAPSHOT_PATH_ENV = "STAGING_SNAPSHOT_PATH"


def record_key(record: Record) -> tuple[str, str, str, int]:
    """Canonical identity of one record for order-insensitive comparison."""
    return (record.name, record.type, record.value, record.ttl)


def _sorted_keys(records: list[Record]) -> list[tuple[str, str, str, int]]:
    return sorted(record_key(r) for r in records)


class TicketRejection(Exception):
    """Base class for ticket rejections; carries HTTP status and reason."""

    status_code: int = 400
    reason: str = "rejected"


class BeforeMismatch(TicketRejection):
    """A ``before`` block does not match the currently staged records."""

    status_code = 409
    reason = "before_mismatch"

    def __init__(self, conflicts: list[dict]) -> None:
        self.conflicts = conflicts
        names = [c["name"] for c in conflicts]
        super().__init__(
            "改前记录与暂存库当前记录不一致，冲突记录名: " + ", ".join(names)
        )


class NameOutsideZones(TicketRejection):
    """A change targets a name that no staged zone is authoritative for."""

    status_code = 422
    reason = "name_outside_zones"

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__(
            "记录名不属于任何暂存区: " + ", ".join(names)
        )


class CandidateZoneInvalid(TicketRejection):
    """The merged snapshot violates the shared zone-record rules."""

    status_code = 422
    reason = "zone_validation_failed"

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("合入后的区记录校验失败: " + "; ".join(errors))


class RequiredDomainsBroken(TicketRejection):
    """The merged snapshot leaves a required domain unresolvable."""

    status_code = 409
    reason = "required_domains_unresolvable"

    def __init__(self, failures: list[Resolution]) -> None:
        self.failures = failures
        super().__init__(
            "合入后必保域名无法解析到 IPv4: "
            + ", ".join(f.domain for f in failures)
        )


class StagingStore:
    """Thread-safe, file-backed store for the staged DNS state."""

    def __init__(self, snapshot_path: Path | str, seed_path: Path | str) -> None:
        self._snapshot_path = Path(snapshot_path)
        self._seed_path = Path(seed_path)
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    # ------------------------------------------------------------------ #
    # Loading and persistence
    # ------------------------------------------------------------------ #

    def _ensure_loaded(self) -> None:
        """Load the snapshot once; seed and persist it on first boot."""
        if self._snapshot is not None:
            return
        if self._snapshot_path.exists():
            self._snapshot = Snapshot.model_validate(
                json.loads(self._snapshot_path.read_text(encoding="utf-8"))
            )
            return
        seed = Snapshot.model_validate(
            json.loads(self._seed_path.read_text(encoding="utf-8"))
        )
        self._persist(seed)
        self._snapshot = seed

    def _persist(self, snapshot: Snapshot) -> None:
        """Write the snapshot atomically (temp file + rename + fsync)."""
        path = self._snapshot_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:  # flush the directory entry as well, best effort
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Public API (each takes the lock: one critical section per call)
    # ------------------------------------------------------------------ #

    def get_snapshot(self) -> Snapshot:
        with self._lock:
            self._ensure_loaded()
            assert self._snapshot is not None
            return self._snapshot.model_copy(deep=True)

    def apply_ticket(self, ticket: ChangeTicket) -> Snapshot:
        """Merge a ticket into the latest snapshot and persist it.

        The whole check-merge-validate-resolve-persist sequence runs under the
        store lock, so a concurrent ticket always sees the newest snapshot and
        the required-domain guarantee holds for every persisted state.
        Raises a ``TicketRejection`` subclass (and persists nothing) when any
        rule fails.
        """
        with self._lock:
            self._ensure_loaded()
            assert self._snapshot is not None
            current = self._snapshot

            zone_names = [z.name for z in current.zones]
            changes_per_zone: dict[int, dict[str, list[Record]]] = {}
            outside: list[str] = []
            conflicts: list[dict] = []

            for change in ticket.changes:
                owner = zone_covering(change.name, zone_names)
                if owner is None:
                    outside.append(change.name)
                    continue
                _zone_name, idx = owner
                staged = [
                    r for r in current.zones[idx].records if r.name == change.name
                ]
                if _sorted_keys(staged) != _sorted_keys(change.before):
                    conflicts.append(
                        {
                            "name": change.name,
                            "zone": current.zones[idx].name,
                            "current": [
                                r.model_dump(mode="json") for r in staged
                            ],
                            "submitted_before": [
                                r.model_dump(mode="json") for r in change.before
                            ],
                        }
                    )
                    continue
                changes_per_zone.setdefault(idx, {})[change.name] = change.after

            if outside:
                raise NameOutsideZones(outside)
            if conflicts:
                raise BeforeMismatch(conflicts)

            # Merge the whole ticket into a candidate snapshot.
            candidate_zones: list[tuple[str, list[Record]]] = []
            for idx, zone in enumerate(current.zones):
                zone_changes = changes_per_zone.get(idx, {})
                merged = [r for r in zone.records if r.name not in zone_changes]
                for name, after in zone_changes.items():
                    merged.extend(after)
                candidate_zones.append((zone.name, merged))

            # Same zone-record validation as /plan.
            errors: list[str] = []
            for zone_name, records in candidate_zones:
                try:
                    check_zone_records(zone_name, "records", records)
                except ValueError as exc:
                    errors.append(str(exc))
            if errors:
                raise CandidateZoneInvalid(errors)

            # Same cache-less authoritative resolution as /plan.
            resolver = Resolver(
                [
                    Zone(name=name, records=records, new_records=records)
                    for name, records in candidate_zones
                ]
            )
            failures = [
                r
                for r in resolver.resolve_all(
                    current.required_domains, frozenset()
                )
                if not r.ok
            ]
            if failures:
                raise RequiredDomainsBroken(failures)

            new_snapshot = Snapshot(
                revision=current.revision + 1,
                zones=[
                    {"name": name, "records": [r.model_dump(mode="json") for r in records]}
                    for name, records in candidate_zones
                ],
                required_domains=list(current.required_domains),
            )
            self._persist(new_snapshot)
            self._snapshot = new_snapshot
            return new_snapshot.model_copy(deep=True)


# --------------------------------------------------------------------------- #
# Process-wide store, configured by environment
# --------------------------------------------------------------------------- #

_store: StagingStore | None = None
_store_lock = threading.Lock()


def get_store() -> StagingStore:
    """Return the process-wide store, building it from the environment."""
    global _store
    with _store_lock:
        if _store is None:
            _store = StagingStore(
                snapshot_path=os.environ.get(
                    SNAPSHOT_PATH_ENV, str(DEFAULT_SNAPSHOT_PATH)
                ),
                seed_path=os.environ.get(SEED_PATH_ENV, str(DEFAULT_SEED_PATH)),
            )
        return _store


def reset_store() -> None:
    """Drop the cached store (used by tests and after config changes)."""
    global _store
    with _store_lock:
        _store = None
