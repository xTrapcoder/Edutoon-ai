"""Pure domain logic (rule 1).

Modules here perform no I/O and never import from ``repositories``,
``providers`` or ``services``. They hold value objects and the rules that
operate on them; the layers above orchestrate persistence and external
calls around them.
"""
