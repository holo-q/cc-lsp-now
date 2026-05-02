## C# Dapper Fixture

`tests/test_csharp_dapper_integration.py` is the real-project C# QA suite for
the agent-first LSP surface. It uses Dapper as an external fixture because it has
the shape we need: partial classes, nested interfaces, generic type handlers,
overrides, and enough real Roslyn metadata to exercise semantic navigation.

The fixture is intentionally not vendored. Keep it under repo-local `tmp/`:

```bash
git clone --depth 1 https://github.com/DapperLib/Dapper.git tmp/csharp-fixtures/Dapper
```

The tests do not load the full Dapper solution. Full upstream solutions often
encode a CI matrix with framework targeting packs that are absent on local agent
machines. Instead, the suite generates `tmp/csharp-fixtures/dapper-smoke/`, a
small `net10.0` project built from Dapper's real `SqlMapper.ITypeHandler.cs` and
`SqlMapper.TypeHandler.cs` plus one local derived handler.

Run the suite explicitly:

```bash
HSP_RUN_CSHARP_FIXTURE=1 uv run --frozen python -m unittest tests.test_csharp_dapper_integration
```

Optional override:

```bash
HSP_CSHARP_FIXTURE_ROOT=/path/to/Dapper \
HSP_RUN_CSHARP_FIXTURE=1 \
uv run --frozen python -m unittest tests.test_csharp_dapper_integration
```

Coverage target:

- `lsp_outline`: real `documentSymbol` output on Dapper's
  `SqlMapper.TypeHandler.cs`. The test uses the smoke file's absolute path
  because the upstream clone and copied smoke file intentionally share a
  basename under repo-local `tmp/`.
- `lsp_grep` and `lsp_symbols_at`: semantic grouping and line bounce on a
  concrete derived handler method with an argument.
- `lsp_types`: type hierarchy expansion from the derived handler to Dapper's
  `StringTypeHandler<T>`.
- `lsp_rename`: workspace rename preview staging without applying edits to the
  fixture.

This suite is not a replacement for the unit tests. It is the slower verifier
that tells us the public surface still works when a real C# language server has
to load real project state.
