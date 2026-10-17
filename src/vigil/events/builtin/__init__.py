"""Built-in detection modules. One directory each; nothing outside it changes (06 §4).

A package here is discovered only if it exports a `MODULE` descriptor and a `create()` factory.
Discovery is not enablement: a module listed here runs only if `modules.enabled` names it.
"""
