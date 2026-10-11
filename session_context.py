import contextvars

current_session = contextvars.ContextVar("current_session", default=None)
