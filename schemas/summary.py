"""
schemas/summary.py
------------------
Request models for the document-summary endpoints.

The *response* for the notes themselves is deliberately plain JSON: it is stored
as JSON text and rendered by the browser, so pinning it to a pydantic model would
only add a place for the stored payload and the schema to drift apart.
"""

from typing import Literal

from pydantic import BaseModel, Field

Depth = Literal["brief", "standard", "full"]


class SummariseRequest(BaseModel):
    depth: Depth = Field(
        "standard",
        description=(
            "brief = study map only (cheapest); "
            "standard = expand the sections that matter; "
            "full = expand every section."
        ),
    )
