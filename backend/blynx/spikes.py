"""Fixed, nonoverlapping Twitch chat windows."""
from collections import deque

from .detector import Bucket, Event


class WindowDetector:
    def __init__(self) -> None:
        self.recent: deque[int] = deque(maxlen=30)
        self.active: Event | None = None
        self.metrics: dict = {}
        self.seconds = 0
        self.total = 0
        self.previous: float | None = None

    def tick(self, when: float, bucket: Bucket | None) -> tuple[str, Event] | None:
        completed = self.active
        self.active = None
        if bucket is None or (self.previous is not None and when - self.previous != 1):
            self.seconds = self.total = 0
            self.metrics.clear()
        self.previous = when
        if bucket is not None:
            self.seconds += 1
            self.total += bucket.count
            if self.seconds == 10:
                self.close_window(when)
        if completed is not None:
            return "end", completed
        if self.active is not None:
            return "start", self.active
        return None

    def close_window(self, when: float) -> None:
        count = self.total
        windows = len(self.recent)
        average = sum(self.recent) / windows if windows else 0
        score = round(100 * count / (count + 3 * average), 1) if count else 0
        self.metrics = {"raw10": count, "baseline": round(average, 2), "score": score,
                        "provisional": windows < 3, "comparedWindows": windows,
                        "similarWindows": sum(prior >= count for prior in self.recent)}
        if windows >= 3 and count > 3 * average:
            middle = when - 5
            self.active = Event(middle, score, False, settled=middle,
                                reason=f"10-second Twitch chat spike: {count} messages vs {average:.1f} average")
        self.recent.append(count)
        self.seconds = self.total = 0
