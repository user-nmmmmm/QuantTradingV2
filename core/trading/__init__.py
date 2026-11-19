"""Orders, protective orders, lots, accounts, position/state persistence and SQLite helpers.

Modules here may import other ``core`` modules but nothing outside ``core``; the package ``__init__`` stays
empty on purpose (a facade that imports its members is how an import cycle starts). See ADR 0013.
"""
