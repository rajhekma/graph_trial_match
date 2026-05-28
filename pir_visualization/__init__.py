"""
PIR (Patient Inclusion Results) visualization layer.

Reads match results from MySQL and enriches graph nodes from Neo4j.
"""

from .pir_router import router

__all__ = ["router"]
