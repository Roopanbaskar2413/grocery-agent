"""Time-driven work. run_once() is what the background loop calls every minute."""
import asyncio
import logging

log = logging.getLogger("agent.scheduler")


class Scheduler:
    def __init__(self, agent, clock, lock):
        self.agent, self.clock, self.lock = agent, clock, lock

    def run_once(self):
        now = self.clock()
        with self.lock:                                   # the same lock the webhooks use
            self.agent.service(now)                       # held messages, reminders, expiry, app payment
            self.agent.fridge_silence_check(now)
            self.agent.evening_check(now)                 # the 6 PM check, once a day
            self.agent.watch_check(now)                   # hourly, read-only: watched items back in stock

    async def loop(self, interval_seconds=60):
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:                             # one bad minute must not stop the scheduler
                log.exception("scheduler pass failed")
            await asyncio.sleep(interval_seconds)
