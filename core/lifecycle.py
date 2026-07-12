from threading import Event, RLock


_LOCK = RLock()
_SHUTDOWN_REQUESTED = Event()
_SHUTDOWN_COMPLETED = Event()
_RUNNING = False


def reset_lifecycle():
    global _RUNNING

    with _LOCK:
        _RUNNING = False
        _SHUTDOWN_REQUESTED.clear()
        _SHUTDOWN_COMPLETED.clear()


def mark_running():
    global _RUNNING

    with _LOCK:
        _RUNNING = True
        _SHUTDOWN_REQUESTED.clear()
        _SHUTDOWN_COMPLETED.clear()


def request_shutdown():
    global _RUNNING

    with _LOCK:
        _RUNNING = False
        _SHUTDOWN_REQUESTED.set()


def mark_shutdown_completed():
    global _RUNNING

    with _LOCK:
        _RUNNING = False
        _SHUTDOWN_COMPLETED.set()


def is_running():
    with _LOCK:
        return _RUNNING and not _SHUTDOWN_REQUESTED.is_set()


def is_shutdown_requested():
    return _SHUTDOWN_REQUESTED.is_set()


def is_shutdown_completed():
    return _SHUTDOWN_COMPLETED.is_set()


def wait_for_shutdown(timeout=None):
    return _SHUTDOWN_REQUESTED.wait(timeout)
