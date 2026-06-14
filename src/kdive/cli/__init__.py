"""``kdivectl`` — the operator CLI as an authenticated MCP client (ADR-0089).

A self-contained package: it imports no ``kdive.services.*`` and reads no
database/object-store credentials. The operator host holds only the bearer token and
the server URL. The boundary is enforced structurally by
``tests/cli/test_no_service_import.py``.
"""
