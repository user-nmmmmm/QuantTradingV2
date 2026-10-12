"""Foundation modules: domain types, runtime wiring, logging, clock, alerting, safety, redaction, identity.

These are the most widely imported modules in the project. They may import other ``core`` modules but nothing
outside ``core``; the package ``__init__`` stays empty on purpose (a facade that imports its members is how an
import cycle starts). See ADR 0016.
"""
