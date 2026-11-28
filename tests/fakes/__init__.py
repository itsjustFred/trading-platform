"""Shared broker doubles for the live adapters.

The live adapters are the money path: a bug in them moves real money. Their
tests therefore have to drive the same seam the real broker sits behind -
Alpaca's REST client and cTrader's OpenAPI request/response - rather than a
double that answers whatever the test happens to ask.

Every module in this package is a *stateful broker*: it keeps an order book
with working orders, positions that net, a fill pipeline that acknowledges an
order before it fills, a rejection path, and listings that page. A double that
fills on submit and lists everything at once lets exactly the classes of bug
this repo has already paid for pass unnoticed:

* an adapter that reads the submit response as a fill (``_register_bracket_legs``
  and the order-id persistence both run on it),
* an adapter that assumes one page of ``status=all`` is the whole book (#174/#350),
* a close that races a resting protective leg and shorts the account.

Each fake ships with contract tests (``tests/fakes/test_*.py``) that pin the
broker behaviour itself, so a migration can assert on the fake and trust it.
"""
