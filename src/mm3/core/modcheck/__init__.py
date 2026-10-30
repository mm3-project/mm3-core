"""``mm3 module check``: conformance checks for a module's source tree.

``catalog`` lists the Module Contract section 11 rows, ``report`` holds the
result model and its JSON, JUnit and text forms, ``pyscan`` and ``jsscan``
are the static scanners, ``static`` runs every static row, ``build`` makes
the wheel and the temporary venv, ``kitrun`` runs the test kit inside that
venv and ``runner`` ties the stages together.

Implements:
    CORE-281: Core spec 0.18, §10.2 Packaging and deployment.
    -: Module Contract 1.0 rev 10, §12.1 mm3 module check.
"""
