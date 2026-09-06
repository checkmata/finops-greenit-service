"""
Pydantic schemas for FastAPI request validation and response serialization.

Schemas are the HTTP boundary — they translate between domain dataclasses and JSON.
Domain never imports from this package (hexagonal rule).
"""