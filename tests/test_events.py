import unittest

from gazevoice.events import EventBus


class EventBusTest(unittest.TestCase):
    def test_subscriber_receives_events_in_order(self):
        bus = EventBus()
        sub = bus.subscribe()
        bus.publish("a", {"n": 1})
        bus.publish("b", {"n": 2})
        first, second = sub.get(timeout=1), sub.get(timeout=1)
        self.assertEqual((first.kind, second.kind), ("a", "b"))
        self.assertLess(first.seq, second.seq)

    def test_full_subscriber_drops_oldest_not_blocks(self):
        bus = EventBus()
        sub = bus.subscribe(maxsize=2)
        for kind in ("one", "two", "three"):
            bus.publish(kind, {})
        self.assertEqual(sub.get(timeout=1).kind, "two")
        self.assertEqual(sub.get(timeout=1).kind, "three")

    def test_get_times_out_with_none(self):
        sub = EventBus().subscribe()
        self.assertIsNone(sub.get(timeout=0.05))

    def test_closed_subscription_stops_receiving(self):
        bus = EventBus()
        sub = bus.subscribe()
        sub.close()
        bus.publish("a", {})
        self.assertIsNone(sub.get(timeout=0.05))


if __name__ == "__main__":
    unittest.main()
