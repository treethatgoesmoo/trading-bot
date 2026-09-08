# Documentation map

Use one current operational source: **[root README](../README.md)**. Actual registration remains `bot/core.py`; do not infer deployment from documentation or passing historical tests.

- [Product scope](product-scope.md): owner-approved direction, research and design boundaries.
- [State rules](market-state.md): exact `spx-state-v1` classification, timing, delivery and migration contract.
- [Research tools](../research/README.md): explicit entry points, side effects, dependencies and historical-reader validation.
- [Strategy log](strategy-log.md): historical experiments and evidence, not a feature registry.
- [Research notes](research/): dated hypotheses/protocols; not instructions to enable old streams.

The old BB-event, SPX-context deployment guides, state inventory and session roadmaps have been retired to short pointers. Their full prior contents remain in Git history (for example the merged BB revision `338036e`). Old Python commands in historical notes must be translated to `python -m research.<tool>` when the tool is retained. Removed cogs/UI have no compatibility wrappers or legacy mode.
