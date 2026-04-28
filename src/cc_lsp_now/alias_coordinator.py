"""Client-aware alias coordination over render memory.

``RenderMemory`` owns canonical identity -> alias allocation.  This module
adds the missing broker-era layer: every workspace session has one master
alias book, while each agent/client has its own frontier of aliases already
introduced in output.  That lets multiple agents converge on the same handles
without compressing a response to an alias the receiving agent has not seen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from cc_lsp_now.render_memory import AliasIdentity, AliasKind, AliasRecord, AliasResolution, RenderMemory


@dataclass(frozen=True, slots=True)
class AliasDecision:
    """One touched identity and whether this client needed a legend intro."""

    record: AliasRecord
    introduced: bool


@dataclass(frozen=True, slots=True)
class AliasTouchResult:
    """Result of touching a batch of identities for a particular client."""

    decisions: tuple[AliasDecision, ...]
    legend: str = ""

    @property
    def records(self) -> tuple[AliasRecord, ...]:
        return tuple(decision.record for decision in self.decisions)


class AliasCoordinator:
    """Master alias book plus per-client introduction frontiers."""

    def __init__(self, memory: RenderMemory | None = None) -> None:
        self.memory = memory or RenderMemory()
        self._introduced_by_client: dict[str, set[str]] = {}

    def touch(self, client_id: str, identities: list[AliasIdentity]) -> AliasTouchResult:
        """Touch identities and return only newly-introduced aliases in legend.

        The same identity always receives the same alias from the master book.
        ``introduced`` is tracked per ``client_id``: a second agent can receive
        the same canonical alias with its own first-use legend, while a warmed
        agent gets the compressed handle without another legend wall.
        """
        client_key = client_id.strip() or "default"
        frontier = self._introduced_by_client.setdefault(client_key, set())
        decisions: list[AliasDecision] = []
        introduced_records: list[AliasRecord] = []
        for identity in identities:
            record = self.memory.touch(identity)
            introduced = record.alias not in frontier
            if introduced:
                frontier.add(record.alias)
                introduced_records.append(record)
            decisions.append(AliasDecision(record=record, introduced=introduced))
        return AliasTouchResult(
            decisions=tuple(decisions),
            legend=self.memory.aliases_for_response(introduced_records, delta=True),
        )

    def lookup(self, token: str) -> AliasResolution:
        return self.memory.lookup(token)

    def clear_client(self, client_id: str) -> bool:
        client_key = client_id.strip() or "default"
        return self._introduced_by_client.pop(client_key, None) is not None

    def clear_epoch(self, reason: str = "") -> None:
        self.memory.clear_epoch(reason)
        self._introduced_by_client.clear()

    def status(self) -> dict[str, object]:
        snapshot = self.memory.snapshot()
        return {
            "epoch": snapshot.epoch_id,
            "generation": snapshot.generation,
            "aliases": len(snapshot.records),
            "clients": {
                client: len(frontier)
                for client, frontier in sorted(self._introduced_by_client.items())
            },
        }


def alias_identity_to_wire(identity: AliasIdentity) -> dict[str, object]:
    return {
        "kind": identity.kind.value,
        "name": identity.name,
        "path": identity.path,
        "line": identity.line,
        "character": identity.character,
        "symbol_kind": identity.symbol_kind,
        "workspace_root": identity.workspace_root,
        "server_label": identity.server_label,
        "bucket_key": identity.bucket_key,
        "bucket_label": identity.bucket_label,
    }


def alias_identity_from_wire(value: object) -> AliasIdentity:
    if not isinstance(value, dict):
        raise ValueError("alias identity must be an object")
    row = cast(dict[str, object], value)
    kind_value = row.get("kind", AliasKind.SYMBOL.value)
    if not isinstance(kind_value, str):
        raise ValueError("alias identity kind must be a string")
    try:
        kind = AliasKind(kind_value)
    except ValueError:
        raise ValueError(f"unknown alias identity kind: {kind_value!r}") from None
    return AliasIdentity(
        kind=kind,
        name=_wire_str(row, "name"),
        path=_wire_str(row, "path"),
        line=_wire_int(row, "line"),
        character=_wire_int(row, "character"),
        symbol_kind=_wire_str(row, "symbol_kind"),
        workspace_root=_wire_str(row, "workspace_root"),
        server_label=_wire_str(row, "server_label"),
        bucket_key=_wire_str(row, "bucket_key"),
        bucket_label=_wire_str(row, "bucket_label"),
    )


def alias_record_to_wire(record: AliasRecord) -> dict[str, object]:
    return {
        "alias": record.alias,
        "bucket": record.bucket,
        "member_index": record.member_index,
        "kind": record.kind.value,
        "identity": alias_identity_to_wire(record.identity),
        "generation": record.generation,
        "epoch_id": record.epoch_id,
    }


def alias_record_from_wire(value: object) -> AliasRecord:
    if not isinstance(value, dict):
        raise ValueError("alias record must be an object")
    row = cast(dict[str, object], value)
    kind_value = row.get("kind", AliasKind.SYMBOL.value)
    if not isinstance(kind_value, str):
        raise ValueError("alias record kind must be a string")
    try:
        kind = AliasKind(kind_value)
    except ValueError:
        raise ValueError(f"unknown alias record kind: {kind_value!r}") from None
    return AliasRecord(
        alias=_wire_str(row, "alias"),
        bucket=_wire_str(row, "bucket"),
        member_index=_wire_int(row, "member_index"),
        kind=kind,
        identity=alias_identity_from_wire(row.get("identity", {})),
        generation=_wire_int(row, "generation"),
        epoch_id=_wire_int(row, "epoch_id"),
    )


def alias_touch_result_to_wire(result: AliasTouchResult) -> dict[str, object]:
    return {
        "decisions": [
            {
                "introduced": decision.introduced,
                "record": alias_record_to_wire(decision.record),
            }
            for decision in result.decisions
        ],
        "legend": result.legend,
    }


def alias_touch_result_from_wire(value: object) -> AliasTouchResult:
    if not isinstance(value, dict):
        raise ValueError("alias touch result must be an object")
    row = cast(dict[str, object], value)
    decisions_value = row.get("decisions", [])
    if not isinstance(decisions_value, list):
        raise ValueError("alias touch decisions must be a list")
    decisions: list[AliasDecision] = []
    for item in decisions_value:
        if not isinstance(item, dict):
            raise ValueError("alias touch decision must be an object")
        decision = cast(dict[str, object], item)
        decisions.append(
            AliasDecision(
                record=alias_record_from_wire(decision.get("record", {})),
                introduced=bool(decision.get("introduced", False)),
            )
        )
    legend = row.get("legend", "")
    return AliasTouchResult(
        decisions=tuple(decisions),
        legend=legend if isinstance(legend, str) else "",
    )


def _wire_str(row: dict[str, Any], key: str) -> str:
    value = row.get(key, "")
    return value if isinstance(value, str) else ""


def _wire_int(row: dict[str, Any], key: str) -> int:
    value = row.get(key, 0)
    return value if isinstance(value, int) else 0
