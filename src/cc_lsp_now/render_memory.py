"""Render Memory — durable, reversible alias book over LSP semantic results.

See ``docs/render-memory.md`` for the full contract. This module is the pure
primitive: deterministic alias minting keyed off canonical semantic identity,
bracketed/unbracketed lookup, generation/epoch metadata, unknown/stale error
distinctions, and legend rendering. It performs no IO, no LSP calls, and no
filesystem observation — staleness is signalled by the caller (``server.py``
for direct mode, the broker session for shared mode).

Design constraints carried from the doc:

- Aliases are issued by the server. The resolver never invents them.
- Within an epoch, aliases are monotonic. ``A3`` never silently rebinds to a
  different identity; once retired it stays retired (member counters do not
  decrement).
- Three handle families coexist: symbol aliases ``A3`` / ``[A3]`` (bucket+
  member), file aliases ``[F1]``, and type aliases ``[T1]``. ``F`` and ``T``
  are reserved single-letter prefixes — symbol bucket allocation skips them.
- Numeric-only tokens (``3``, ``[3]``) are graph-handle space, not aliases;
  they are refused at lookup with an ``INVALID`` error so the server-side
  resolver can fall through to graph-index resolution.
- Unicode lookalikes are rejected at parse time — there is no fuzzy matching
  and no "did you mean" path for aliases.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final


class AliasKind(Enum):
    SYMBOL = "symbol"
    FILE = "file"
    TYPE = "type"


class AliasError(Enum):
    UNKNOWN = "unknown"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class AliasIdentity:
    """Canonical identity of an entity that may receive an alias.

    Two ``AliasIdentity`` values that compare equal map to the same alias
    within an epoch — equality drives reuse. ``bucket_key`` and
    ``bucket_label`` apply only to ``SYMBOL`` kind; ``FILE`` and ``TYPE``
    share single well-known buckets (``F`` and ``T``) and ignore those
    fields.

    ``bucket_key`` is the caller-supplied grouping key — the nearest stable
    semantic container per the doc (containing class for methods/fields,
    module/file for free functions, containing method for locals,
    type identity for type aliases). When omitted for ``SYMBOL`` kind, it
    defaults to ``path`` so the natural bucket is one-per-file.

    ``bucket_label`` is the human-readable string printed in the legend's
    bucket header line, e.g. ``ComfyNodeRenderer.cs::ComfyNodeRenderer``.
    """

    kind: AliasKind
    name: str = ""
    path: str = ""
    line: int = 0
    character: int = 0
    symbol_kind: str = ""
    workspace_root: str = ""
    server_label: str = ""
    bucket_key: str = ""
    bucket_label: str = ""


@dataclass(frozen=True, slots=True)
class AliasRecord:
    """A minted alias and the identity it points to.

    ``alias`` is the canonical printable form (``A3``, ``F1``, ``T1``).
    Bracketed forms are display sugar handled by the renderer/resolver.
    ``generation`` is the render-memory generation when the alias was minted;
    ``epoch_id`` is the epoch active at mint time, so cross-epoch comparison
    is deterministic.
    """

    alias: str
    bucket: str
    member_index: int
    kind: AliasKind
    identity: AliasIdentity
    generation: int
    epoch_id: int


@dataclass(frozen=True, slots=True)
class AliasResolution:
    """Outcome of ``RenderMemory.lookup``.

    Exactly one of ``record`` or ``error`` is populated. ``message`` carries
    the agent-readable refusal text per the doc's "not active" / "stale" /
    "ambiguous" distinction, with a generation stamp so the model knows which
    snapshot the answer pertains to.
    """

    record: AliasRecord | None = None
    error: AliasError | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.record is not None


@dataclass(frozen=True, slots=True)
class RenderMemorySnapshot:
    """Frozen, restorable view of a ``RenderMemory``.

    Used by the broker (and by save/restore tests) to migrate state without
    touching the renderer grammar. All sequences are tuples so the snapshot
    is hashable and never aliases live state.
    """

    epoch_id: int
    generation: int
    records: tuple[AliasRecord, ...]
    bucket_for_key: tuple[tuple[str, str], ...]
    bucket_label: tuple[tuple[str, str], ...]
    bucket_member_count: tuple[tuple[str, int], ...]
    next_bucket_index: int
    stale_aliases: tuple[tuple[str, str], ...]


_FILE_PREFIX: Final[str] = "F"
_TYPE_PREFIX: Final[str] = "T"
_RESERVED_SYMBOL_PREFIXES: Final[frozenset[str]] = frozenset({_FILE_PREFIX, _TYPE_PREFIX})
_ALIAS_TOKEN: Final[re.Pattern[str]] = re.compile(r"([A-Za-z]+)(\d+)")


def _index_to_alpha(n: int) -> str:
    """Map a 0-based index to a base-26 alpha label.

    ``0 -> A, 1 -> B, ..., 25 -> Z, 26 -> AA, 27 -> AB, ...``. Used to mint
    symbol bucket prefixes deterministically.
    """
    s = ""
    m = n
    while True:
        s = chr(ord("A") + m % 26) + s
        m //= 26
        if m == 0:
            break
        m -= 1
    return s


def _default_bucket_key(identity: AliasIdentity) -> str:
    return identity.bucket_key or identity.path or identity.name


def _member_chip(record: AliasRecord) -> str:
    """Render the per-member legend chip.

    Files print just the path (``F1=src/...``) because the path is the
    identity. Symbols and types print ``name@Lline`` so the agent can verify
    the alias against the source row.
    """
    ident = record.identity
    if record.kind is AliasKind.FILE:
        return f"{record.alias}={ident.path}"
    return f"{record.alias}={ident.name}@L{ident.line}"


@dataclass(slots=True)
class RenderMemory:
    """Pure, in-process alias book.

    The instance is mutable; the records it issues are frozen. Direct mode
    holds one of these as a process global beside ``_last_semantic_groups``;
    broker mode will hold one per workspace session. The API is intentionally
    small so the broker can own the same shape without changing the output
    grammar.
    """

    epoch_id: int = 0
    generation: int = 0
    _records_by_identity: dict[AliasIdentity, AliasRecord] = field(default_factory=dict)
    _records_by_alias: dict[str, AliasRecord] = field(default_factory=dict)
    _bucket_for_key: dict[str, str] = field(default_factory=dict)
    _bucket_label: dict[str, str] = field(default_factory=dict)
    _bucket_member_count: dict[str, int] = field(default_factory=dict)
    _next_bucket_index: int = 0
    _stale_aliases: dict[str, str] = field(default_factory=dict)

    def touch(self, identity: AliasIdentity) -> AliasRecord:
        """Mint or refresh an alias for ``identity``.

        Same identity returns the same record — alias reuse for the same
        identity is the core compression promise. New identity allocates a
        fresh member in the right bucket; bucket member counters are
        monotonic so retired aliases are never recycled within an epoch.
        """
        existing = self._records_by_identity.get(identity)
        if existing is not None:
            return existing
        prefix = self._allocate_bucket_prefix(identity)
        member = self._bucket_member_count.get(prefix, 0) + 1
        self._bucket_member_count[prefix] = member
        alias = f"{prefix}{member}"
        self.generation += 1
        record = AliasRecord(
            alias=alias,
            bucket=prefix,
            member_index=member,
            kind=identity.kind,
            identity=identity,
            generation=self.generation,
            epoch_id=self.epoch_id,
        )
        self._records_by_identity[identity] = record
        self._records_by_alias[alias] = record
        return record

    def get(self, alias: str) -> AliasRecord | None:
        """Direct alias-string accessor. Skips parsing — pass canonical strings."""
        return self._records_by_alias.get(alias)

    def lookup(self, token: str) -> AliasResolution:
        """Resolve a render-memory token to its record.

        Accepts ``A3``, ``[A3]``, ``F1``, ``[F1]``, ``T1``, ``[T1]`` (case is
        canonicalized to upper). Numeric-only and malformed tokens return
        ``INVALID`` so the server-side resolver can fall through to graph
        indices and line targets without consulting LSP.
        """
        raw = token.strip()
        if not raw:
            return AliasResolution(error=AliasError.INVALID, message="empty alias token")
        if not raw.isascii():
            return AliasResolution(
                error=AliasError.INVALID,
                message=f"alias token {token!r} contains non-ASCII characters",
            )
        inner = raw
        if inner.startswith("[") and inner.endswith("]"):
            inner = inner[1:-1].strip()
        if not inner:
            return AliasResolution(error=AliasError.INVALID, message="empty alias token")
        if inner.isdigit():
            return AliasResolution(
                error=AliasError.INVALID,
                message=f"{token!r} is a graph handle, not a render-memory alias",
            )
        match = _ALIAS_TOKEN.fullmatch(inner)
        if match is None:
            return AliasResolution(
                error=AliasError.INVALID,
                message=f"alias token {token!r} does not match [A-Za-z]+\\d+",
            )
        bucket = match.group(1).upper()
        member = int(match.group(2))
        if member <= 0:
            return AliasResolution(
                error=AliasError.INVALID,
                message=f"alias member index in {token!r} must be positive",
            )
        alias = f"{bucket}{member}"
        record = self._records_by_alias.get(alias)
        if record is not None:
            return AliasResolution(record=record)
        stale_reason = self._stale_aliases.get(alias)
        if stale_reason is not None:
            return AliasResolution(
                error=AliasError.STALE,
                message=f"Alias {alias} is stale: {stale_reason}",
            )
        return AliasResolution(
            error=AliasError.UNKNOWN,
            message=(
                f"Alias {alias} is not active in render memory gen={self.generation}. "
                "Run lsp_legend or re-anchor with lsp_grep."
            ),
        )

    def mark_stale(
        self,
        alias_or_identity: str | AliasIdentity,
        reason: str,
    ) -> AliasRecord | None:
        """Retire an alias. Future lookups return ``STALE``.

        Member counters are not rewound, so the alias string can never be
        reissued in this epoch. Returns the retired record (or ``None`` when
        the input did not match an active alias).
        """
        if isinstance(alias_or_identity, AliasIdentity):
            record = self._records_by_identity.get(alias_or_identity)
        else:
            record = self._records_by_alias.get(alias_or_identity)
        if record is None:
            return None
        self._records_by_identity.pop(record.identity, None)
        self._records_by_alias.pop(record.alias, None)
        self._stale_aliases[record.alias] = reason or "alias retired"
        self.generation += 1
        return record

    def clear_epoch(self, reason: str = "") -> None:
        """End the current epoch.

        All records (active and stale) are dropped, bucket allocation rewinds
        to ``A``/1, and ``epoch_id`` increments. Cross-epoch alias recycling
        is allowed because the epoch boundary is the contract — agents must
        treat fresh epochs as fresh slates.
        """
        del reason
        self._records_by_identity.clear()
        self._records_by_alias.clear()
        self._stale_aliases.clear()
        self._bucket_for_key.clear()
        self._bucket_label.clear()
        self._bucket_member_count.clear()
        self._next_bucket_index = 0
        self.epoch_id += 1
        self.generation += 1

    def aliases_for_response(
        self,
        records: Iterable[AliasRecord],
        delta: bool = False,
    ) -> str:
        """Render a legend block for ``records``.

        Empty input returns ``""`` per the doc's "Empty results emit no
        legend" rule. ``delta=True`` produces a ``legend+`` header; otherwise
        the full ``legend`` header is used. Symbol buckets group on one line
        with a bucket header (``A=Class  A3=Render@L44  A7=Update@L88``);
        file and type buckets emit one chip per line because the prefix
        itself names the family.
        """
        grouped: dict[str, dict[str, AliasRecord]] = {}
        for record in records:
            grouped.setdefault(record.bucket, {}).setdefault(record.alias, record)
        if not grouped:
            return ""
        header = f"legend{'+' if delta else ''} gen={self.generation}:"
        lines = [header]
        for bucket in sorted(grouped):
            members = sorted(grouped[bucket].values(), key=lambda r: r.member_index)
            if bucket in _RESERVED_SYMBOL_PREFIXES:
                for record in members:
                    lines.append(f"  {_member_chip(record)}")
                continue
            chips = "  ".join(_member_chip(record) for record in members)
            label = self._bucket_label.get(bucket, "")
            if label:
                lines.append(f"  {bucket}={label}  {chips}")
            else:
                lines.append(f"  {chips}")
        return "\n".join(lines)

    def snapshot(self) -> RenderMemorySnapshot:
        """Freeze the current state for migration or test inspection."""
        return RenderMemorySnapshot(
            epoch_id=self.epoch_id,
            generation=self.generation,
            records=tuple(self._records_by_alias.values()),
            bucket_for_key=tuple(self._bucket_for_key.items()),
            bucket_label=tuple(self._bucket_label.items()),
            bucket_member_count=tuple(self._bucket_member_count.items()),
            next_bucket_index=self._next_bucket_index,
            stale_aliases=tuple(self._stale_aliases.items()),
        )

    def restore(self, snapshot: RenderMemorySnapshot) -> None:
        """Replace state with ``snapshot``. Inverse of ``snapshot()``."""
        self.epoch_id = snapshot.epoch_id
        self.generation = snapshot.generation
        self._records_by_alias = {r.alias: r for r in snapshot.records}
        self._records_by_identity = {r.identity: r for r in snapshot.records}
        self._bucket_for_key = dict(snapshot.bucket_for_key)
        self._bucket_label = dict(snapshot.bucket_label)
        self._bucket_member_count = dict(snapshot.bucket_member_count)
        self._next_bucket_index = snapshot.next_bucket_index
        self._stale_aliases = dict(snapshot.stale_aliases)

    def _allocate_bucket_prefix(self, identity: AliasIdentity) -> str:
        if identity.kind is AliasKind.FILE:
            return _FILE_PREFIX
        if identity.kind is AliasKind.TYPE:
            return _TYPE_PREFIX
        key = _default_bucket_key(identity)
        existing = self._bucket_for_key.get(key)
        if existing is not None:
            if identity.bucket_label and not self._bucket_label.get(existing):
                self._bucket_label[existing] = identity.bucket_label
            return existing
        prefix = self._next_symbol_prefix()
        self._bucket_for_key[key] = prefix
        self._bucket_label[prefix] = identity.bucket_label or key
        return prefix

    def _next_symbol_prefix(self) -> str:
        while True:
            candidate = _index_to_alpha(self._next_bucket_index)
            self._next_bucket_index += 1
            if candidate not in _RESERVED_SYMBOL_PREFIXES:
                return candidate
