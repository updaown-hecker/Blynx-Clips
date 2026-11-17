from collections import Counter, deque
from dataclasses import dataclass
from math import ceil, sqrt
from statistics import median


@dataclass(frozen=True)
class Detection:
    rise: float = 2.5
    deviations: float = 5
    min_messages: int = 12
    min_users: int = 5
    warmup: int = 25
    baseline: int = 120
    onset: int = 2
    merge: int = 6
    cooldown: int = 35
    per_hour: int = 12
    max_active: int = 80


@dataclass
class Bucket:
    count: int
    authors: set[str]
    repeats: int = 0
    emotes: int = 0


class Aggregator:
    def __init__(self) -> None:
        self.authors: Counter[str] = Counter()
        self.signatures: Counter[str] = Counter()
        self.count = 0
        self.repeats = 0
        self.emotes = 0
        self.delay_total = 0.0
        self.delay_samples = 0

    def add(self, author: str, signature: str, emote: bool, delay: float | None) -> None:
        self.count += 1
        if len(self.authors) < 50000 or author in self.authors:
            self.authors[author] += 1
        if signature in self.signatures:
            self.repeats += 1
        if signature and len(self.signatures) < 10000:
            self.signatures[signature] += 1
        self.emotes += emote
        if delay is not None:
            self.delay_total += delay
            self.delay_samples += 1

    def bucket(self) -> Bucket:
        return Bucket(sum(min(3, count) for count in self.authors.values()),
                      set(self.authors), self.repeats, self.emotes)


@dataclass
class Event:
    onset: float
    peak: float
    provisional: bool
    settled: float | None = None
    reason: str = ""


class Detector:
    def __init__(self, config: Detection) -> None:
        self.config = config
        self.history: deque[float] = deque(maxlen=config.baseline)
        self.recent: deque[Bucket] = deque(maxlen=5)
        # Non-overlapping windows give the dashboard an observed frequency.
        self.comparisons: deque[int] = deque(maxlen=max(60, config.baseline // 5))
        self.last_tick: float | None = None
        self.last_comparison: float | None = None
        self.starts: deque[float] = deque()
        self.active: Event | None = None
        self.rising: float | None = None
        self.recovery: float | None = None
        self.cooldown_until = 0.0
        self.learn_on_cooldown = False
        self.metrics: dict = {}

    def _end(self, when: float, reason: str) -> tuple[str, Event]:
        event = self.active
        assert event is not None
        event.settled = self.recovery if self.recovery is not None else when
        event.reason = reason
        self.active = None
        self.recovery = None
        self.cooldown_until = when + self.config.cooldown
        self.learn_on_cooldown = reason == "duration cap"
        if self.learn_on_cooldown:
            new = min(self.metrics["count5"], max(self.config.min_messages, self.metrics["baseline"] * 2))
            self.history.clear()
            self.history.extend([new] * self.config.warmup)
            self.cooldown_until = when + max(self.config.cooldown, self.config.warmup + 5)
        return "end", event

    def tick(self, when: float, bucket: Bucket | None) -> tuple[str, Event] | None:
        config = self.config
        if bucket is None or (self.last_tick is not None and when - self.last_tick > 1.5):
            self.recent.clear()
            self.recovery = self.rising = None
            self.metrics = {}
        self.last_tick = when
        if bucket is None:
            if self.active and when - self.active.onset >= config.max_active:
                return self._end(when, "chat coverage lost")
            return None

        self.recent.append(bucket)
        if len(self.recent) < 5:
            return None
        volume = sum(b.count for b in self.recent)
        authors: set[str] = set()
        for item in self.recent:
            authors.update(item.authors)
        baseline = median(self.history) if self.history else volume
        mad = median(abs(sample - baseline) for sample in self.history) if self.history else 0
        spread = max(sqrt(max(1, baseline)), 1.4826 * mad)
        provisional = len(self.history) < config.warmup
        excess = max(0.0, volume - baseline)
        # A stable, busy chat can have an unusual spike without doubling its traffic.
        lift_target = (config.rise - 1) / (1 + sqrt(baseline / 100))
        deviation_target = config.deviations * (1.6 if provisional else 1)
        lift_target *= 1.5 if provisional else 1
        threshold = max(config.min_messages, baseline * (1 + lift_target),
                        baseline + spread * deviation_target)
        audience = max(config.min_users, min(12, ceil(sqrt(baseline) / 4)))
        high = volume >= threshold and len(authors) >= audience
        lift = excess / max(config.min_messages, baseline)
        deviation = excess / spread
        breadth = len(authors) / (len(authors) + audience)
        strength = (0.55 * deviation / (deviation + deviation_target)
                    + 0.30 * lift / (lift + lift_target) + 0.15 * breadth)
        score = round(100 * strength * min(1, len(authors) / audience), 1) if excess else 0.0

        self.metrics = {"count5": volume, "baseline": baseline, "score": score,
                        "provisional": provisional, "unique5": len(authors),
                        "comparedWindows": len(self.comparisons),
                        "similarWindows": sum(sample >= volume for sample in self.comparisons)}
        if self.last_comparison is None or when - self.last_comparison >= 5:
            self.comparisons.append(volume)
            self.last_comparison = when

        if self.active is not None:
            self.active.peak = max(self.active.peak, score)
            below_exit = volume < baseline + (threshold - baseline) * 0.45
            self.recovery = (self.recovery if self.recovery is not None else when) if below_exit else None
            if when - self.active.onset >= config.max_active:
                return self._end(when, "duration cap")
            if self.recovery is not None and when - self.recovery >= config.merge:
                return self._end(when, "activity settled")
            return None

        while self.starts and self.starts[0] <= when - 3600:
            self.starts.popleft()
        if high and when >= self.cooldown_until and len(self.history) >= 8 and len(self.starts) < config.per_hour:
            self.rising = self.rising if self.rising is not None else when
            if when - self.rising + 1 >= config.onset:
                self.active = Event(self.rising, score, provisional)
                self.rising = None
                self.starts.append(when)
                return "start", self.active
            return None
        self.rising = None
        if when >= self.cooldown_until or self.learn_on_cooldown:
            self.history.append(volume)
        return None

