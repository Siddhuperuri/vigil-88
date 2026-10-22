"""One router per resource (02 §2). Each reads through a service boundary, never the database.

The console is a client of a documented API and never a privileged peer (D-002), so these
routers are the *only* way incident data leaves the process -- and they hold no business logic
of their own: they translate, they do not decide.
"""
