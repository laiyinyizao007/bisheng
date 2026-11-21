"""F005 workflow-as-code: the local half of ``bisheng flow``.

``spec`` loads a declaration, ``overlay`` applies the constrained Python overlay,
``compiler`` turns the declaration into a platform workflow definition and
``validate`` runs the compile-time structural checks. None of these modules
talks to the platform; node templates and the tool list are passed in.
"""
