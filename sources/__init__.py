"""sources — one module per external data source.

Each module exports a single top-level `fetch_*()` function that returns:
    list of dicts with keys:
      url       (required) — canonical URL to the announcement/tender/lead
      title     (required) — human-readable title
      snippet   (optional) — short description
      date      (optional) — ISO or RFC-822 date string
      company   (optional) — extracted organization name

The adapters (adapters/tenders.py, filings.py, news.py, expansion.py,
company.py) import these modules with ImportError guards, so deploying
a subset works correctly — missing modules are silently skipped.
"""
__version__ = "1.0"
