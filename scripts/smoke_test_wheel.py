"""Smoke test: native writers render text in a minimal Linux runtime.

Intended to run inside a slim container (e.g., python:*-slim) against an
installed ggsql wheel, after provisioning the documented runtime
dependencies (libfontconfig1 + fonts-dejavu-core — see README "Native
Writers"). Catches packaging omissions and missing system dependencies:
without fontconfig, native renders silently lose all text.
"""

import ggsql

reader = ggsql.DuckDBReader("duckdb://memory")
spec = reader.execute(
    "SELECT * FROM ggsql:penguins "
    "VISUALISE DRAW point MAPPING bill_len AS x, bill_dep AS y"
)

svg = ggsql.SVGWriter().render(spec)
assert "<svg" in svg, "SVG output is malformed"
assert "bill_len" in svg, (
    "SVG lost all text — are libfontconfig1 and fonts installed?"
)

pdf = ggsql.PDFWriter().render(spec)
assert pdf.startswith(b"%PDF"), "PDF output is malformed"

hep = ggsql.HepWriter().render(spec)
assert len(hep) > 0, "HEP output is empty"

print(f"smoke test OK: svg={len(svg)}B pdf={len(pdf)}B hep={len(hep)}B")
