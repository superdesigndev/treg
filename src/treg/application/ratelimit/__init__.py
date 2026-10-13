"""Request rate limiting: the per-process runtime and the audited policy writes.

`limiter` and `config_state` are the serving side and never touch the database; `admin` is the
write side and the only module that touches `RateLimitConfigVersion`.
"""
