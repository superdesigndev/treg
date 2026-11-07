"""Views: what the CLI prints, built from a route's JSON and what the registry knows about the caller.

Pure functions over stdlib and toon_format, imported by the server (routers/view.py) and by the
light Python CLI, so both print the same text.
"""
