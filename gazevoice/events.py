"""In-process publish/subscribe that feeds live updates to the dashboard."""

from __future__ import annotations

import itertools
import queue
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict
    seq: int


class Subscription:
    def __init__(self, bus: "EventBus", maxsize: int):
        self._bus = bus
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
        self._closed = False

    def _offer(self, event: Event) -> None:
        # A slow reader loses its oldest events; publishers never block.
        while not self._closed:
            try:
                self._queue.put_nowait(event)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

    def get(self, timeout: float | None = None) -> Event | None:
        if self._closed:
            return None
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed = True
        self._bus._remove(self)


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: list[Subscription] = []
        self._seq = itertools.count(1)

    def publish(self, kind: str, data: dict) -> Event:
        with self._lock:
            event = Event(kind, data, next(self._seq))
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            subscription._offer(event)
        return event

    def subscribe(self, maxsize: int = 1000) -> Subscription:
        subscription = Subscription(self, maxsize)
        with self._lock:
            self._subscriptions.append(subscription)
        return subscription

    def _remove(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)
