"""Bringing a dead worker thread back, without looping on one that cannot live.

The assistant, audio and speech workers each run on a thread of their own, and
a thread that raises just stops. Left alone, the device would carry on with a
face and no voice and nothing to say why. The thread posts `WorkerDied` from a
`finally`, and the conductor hands that to `Supervisor.died`.

It restarts after 1s, then 5s, then 30s. A worker that has died four times
without getting anywhere is left off and reported (`status()` is what the web
page shows as "voice is down"). It is not a reason to roll back a release: a
missing voice is a smaller problem than a device that restarts itself in a
loop, so `Device.healthy` does not look at this.

The restart itself is a callable the owner of the worker supplies, which makes
this the same mechanism for every service and testable with no thread in it.
"""

BACKOFF = (1.0, 5.0, 30.0)

# A worker that ran this long before dying was working. Its next death starts
# the count again instead of spending one of its three restarts.
STABLE_AFTER = 120.0

RUNNING = "running"
WAITING = "waiting"
DOWN = "down"


class _Worker:
    def __init__(self, name, start):
        self.name = name
        self.start = start
        self.state = RUNNING
        self.deaths = 0
        self.restart_at = None
        self.started_at = None


class Supervisor:
    def __init__(self, log=None):
        self.log = log or (lambda line: None)
        self._workers = {}

    def add(self, name, start, now=0.0):
        """Register a worker. `start` begins it and returns nothing; it is
        called now, and again after each death."""
        worker = _Worker(name, start)
        self._workers[name] = worker
        worker.started_at = now
        start()

    def died(self, name, now):
        worker = self._workers.get(name)
        if worker is None or worker.state != RUNNING:
            return
        if worker.started_at is not None \
                and now - worker.started_at >= STABLE_AFTER:
            worker.deaths = 0
        worker.deaths += 1
        if worker.deaths > len(BACKOFF):
            worker.state = DOWN
            worker.restart_at = None
            self.log("%s keeps dying; leaving it off" % name)
            return
        delay = BACKOFF[worker.deaths - 1]
        worker.state = WAITING
        worker.restart_at = now + delay
        self.log("%s died; restarting in %gs" % (name, delay))

    def tick(self, now):
        for worker in self._workers.values():
            if worker.state == WAITING and now >= worker.restart_at:
                worker.state = RUNNING
                worker.restart_at = None
                worker.started_at = now
                try:
                    worker.start()
                except Exception as exc:
                    # Failing to start is a death like any other.
                    self.log("%s did not start: %s" % (worker.name, exc))
                    self.died(worker.name, now)

    def status(self):
        return {name: {"state": w.state, "deaths": w.deaths}
                for name, w in self._workers.items()}

    def down(self):
        return [name for name, w in self._workers.items()
                if w.state == DOWN]
