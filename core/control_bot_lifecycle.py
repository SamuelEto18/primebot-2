import asyncio
from contextlib import suppress
from threading import Event, RLock, Thread, current_thread

DEFAULT_HEARTBEAT_INTERVAL = 10


class ControlBotLifecycle:

    def __init__(
        self,
        application_factory,
        logger,
        on_started=None,
        on_stopped=None,
        on_error=None,
        on_heartbeat=None,
        heartbeat_interval=DEFAULT_HEARTBEAT_INTERVAL,
        name="Telegram Control Bot"
    ):
        self.application_factory = application_factory
        self.logger = logger
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.on_error = on_error
        self.on_heartbeat = on_heartbeat
        self.heartbeat_interval = heartbeat_interval
        self.name = name

        self._lock = RLock()
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._started_event = None
        self._application = None
        self._running = False
        self._starting = False
        self._last_error = None
        self._startup_error = None

    @property
    def last_error(self):

        with self._lock:
            return self._last_error

    def is_running(self):

        with self._lock:
            return (
                self._running
                and self._thread is not None
                and self._thread.is_alive()
            )

    def get_status(self):

        with self._lock:
            return {
                "running": self.is_running(),
                "starting": self._starting,
                "last_error": self._last_error,
                "thread_alive": (
                    self._thread is not None
                    and self._thread.is_alive()
                ),
            }

    def start(self, timeout=30):

        with self._lock:

            if self.is_running() or self._starting:
                return True

            self._started_event = Event()
            self._startup_error = None
            self._last_error = None
            self._starting = True

            self._thread = Thread(
                target=self._thread_main,
                daemon=True,
                name="PrimeBot-ControlBot"
            )
            self._thread.start()

            started_event = self._started_event

        if not started_event.wait(timeout):
            message = f"{self.name} startup timed out"
            self._set_error(message)
            return False

        with self._lock:
            return self._startup_error is None and self._running

    def stop(self, timeout=30):

        with self._lock:
            thread = self._thread
            loop = self._loop
            stop_event = self._stop_event

            if thread is None:
                self._running = False
                self._starting = False
                return True

            if not thread.is_alive():
                self._running = False
                self._starting = False
                return True

            if loop is not None and stop_event is not None:
                loop.call_soon_threadsafe(stop_event.set)

        if thread is current_thread():
            return True

        thread.join(timeout)

        with self._lock:
            stopped = not thread.is_alive()

            if stopped:
                self._running = False
                self._starting = False

            return stopped

    def restart(self, timeout=30):

        if not self.stop(timeout=timeout):
            self._set_error(f"{self.name} failed to stop during restart")
            return False

        return self.start(timeout=timeout)

    def _thread_main(self):

        loop = asyncio.new_event_loop()

        with self._lock:
            self._loop = loop

        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._run())
        except Exception:
            self.logger.exception(f"{self.name} lifecycle crashed")
        finally:
            with self._lock:
                self._running = False
                self._starting = False
                self._loop = None
                self._stop_event = None
                self._application = None

            try:
                loop.close()
            except Exception as exc:
                self.logger.error(f"{self.name} event loop close failed: {exc}")

    async def _run(self):

        application = None
        polling_started = False
        heartbeat_task = None

        try:
            application = self.application_factory()
            stop_event = asyncio.Event()

            with self._lock:
                self._application = application
                self._stop_event = stop_event

            await application.initialize()
            await application.start()

            if getattr(application, "updater", None) is None:
                raise RuntimeError("Control Bot updater is unavailable")

            await application.updater.start_polling()
            polling_started = True

            with self._lock:
                self._running = True
                self._starting = False
                self._last_error = None
                self._startup_error = None

            if self.on_started:
                self.on_started()

            if self.on_heartbeat:
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(stop_event)
                )

            self._started_event.set()

            await stop_event.wait()

        except Exception as exc:
            with self._lock:
                self._running = False
                self._starting = False
                self._last_error = str(exc)
                self._startup_error = exc

            if self.on_error:
                self.on_error(str(exc))

            if self._started_event is not None:
                self._started_event.set()

            raise

        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()

                with suppress(asyncio.CancelledError):
                    await heartbeat_task

            if application is not None:
                await self._shutdown_application(application, polling_started)

            with self._lock:
                self._running = False
                self._starting = False

            if self.on_stopped:
                self.on_stopped()

            if self._started_event is not None:
                self._started_event.set()

    async def _shutdown_application(self, application, polling_started):

        if polling_started and getattr(application, "updater", None) is not None:
            try:
                await application.updater.stop()
            except Exception as exc:
                self.logger.error(f"{self.name} polling stop failed: {exc}")

        try:
            await application.stop()
        except Exception as exc:
            self.logger.error(f"{self.name} application stop failed: {exc}")

        try:
            await application.shutdown()
        except Exception as exc:
            self.logger.error(f"{self.name} application shutdown failed: {exc}")

    async def _heartbeat_loop(self, stop_event):
        while not stop_event.is_set():
            try:
                self.on_heartbeat()
            except Exception as exc:
                self.logger.error(f"{self.name} heartbeat failed: {exc}")

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.heartbeat_interval,
                )
            except asyncio.TimeoutError:
                pass

    def _set_error(self, message):

        with self._lock:
            self._last_error = message
            self._startup_error = RuntimeError(message)
            self._running = False
            self._starting = False

        if self.on_error:
            self.on_error(message)

        if self._started_event is not None:
            self._started_event.set()
