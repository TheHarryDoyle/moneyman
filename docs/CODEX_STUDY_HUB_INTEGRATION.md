# Codex Study Hub Integration

MoneyMan should not become another local web app with another visible port. If it needs a web GUI, it should plug into the existing Codex Word Game / Codex Study Hub experience.

## User Rule

The user should open one local hub URL, normally the Codex Study Hub server on port `8000`. MoneyMan should appear as a section inside that hub, not as a separate app the user has to remember.

Good future routes:

```text
/moneyman
/moneyman/data
/moneyman/gridbots
/moneyman/reports
/moneyman/risk
```

Avoid:

```text
http://127.0.0.1:8001
http://127.0.0.1:5173
http://127.0.0.1:<some-new-money-port>
```

## Integration Shape

Preferred approach:

- keep MoneyMan's ingestion, normalization, backtest, and reporting code in this repository;
- expose stable command-line or Python-callable outputs;
- let Codex Study Hub read generated catalog/report artifacts or call a small local library boundary;
- add the UI routes, navigation item, and dark visual treatment inside Codex Study Hub.

This keeps MoneyMan's research pipeline separate while preserving one visible user interface.

## Visual Direction

Use the same dark grimdark/gothic sci-fi language as Codex Word Game, adapted for finance:

- dense market tables;
- order-book heatmaps;
- dark status panels;
- warning colors for stale data, invalid book windows, and risk limits;
- no bright white dashboard shell;
- no marketing landing page.

## First UI Slice

Do not start with trading controls. Start with read-only visibility:

1. data inventory summary;
2. raw/derived coverage by product and date;
3. data-quality warnings;
4. level 2 reconstruction validity;
5. gridbot backtest summaries after the backtester exists.

Live trading controls should not exist in the UI until the user explicitly asks after research, risk, and paper trading are proven.

## Agent Notes

If an agent is asked to build a MoneyMan web UI, it should inspect the Codex Word Game / Codex Study Hub repo first and follow its existing server, navigation, styling, and persistence patterns.
