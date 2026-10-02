"""Vantage - a permissioned, multi-agent personal AI operating system.

This package implements the *Core* described in section 60 of the plan:

    1. Orchestrator
    2. Tool Registry
    3. Permission Engine
    4. Laptop Agent
    5. Memory
    6. Audit Log

The proof loop is:

    Intent -> Plan -> Permission -> Tool -> Execution -> Verification
           -> Memory -> Response
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
