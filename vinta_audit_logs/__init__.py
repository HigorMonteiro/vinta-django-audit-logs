"""A swappable, scope-agnostic audit log for Django projects.

Install the app, point ``AUDIT_SCOPE_MODEL`` and ``AUDIT_IDENTITY_MODEL`` at
models that match your project's idea of a tenant and an actor, subclass
``AuditService`` and ``DjangoORMAuditRepository`` to fill whatever columns those
models add, and record away.
"""
