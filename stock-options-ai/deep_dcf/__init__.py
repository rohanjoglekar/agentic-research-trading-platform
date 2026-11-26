"""Deep-DCF models: a full three-scenario valuation per company, on a budget.

The model is asked only for its assumptions. Python builds the workbook,
LibreOffice recalculates it, and an independent check decides whether it may
be published. selection.py and the runner job decide which companies are
modelled and when; they read the account, so they are not published with the
rest.

Layout
  config.py      every knob (env-overridable), paths
  budget.py      spend ledger + monthly ceiling + pacing (the no-overspend rule)
  edgar.py       SEC filing dates + XBRL statement extraction
  inputs.py      inputs.json for one symbol (the model's only data source)
  batch.py       the Message Batches request: prompt, schema, reading the answer
  prompt.py      the prompt for the original headless-agent engine
  sandbox.py     run the model, the builder and LibreOffice as an unprivileged user
  store.py       runner state, the index of accepted models, the public index
  tools/build_model.py      writes the workbook from the assumptions
  tools/check_delivery.py   independent re-verification of what was delivered
  tools/recalc.py           LibreOffice recalculation (runs inside the sandbox)
"""
