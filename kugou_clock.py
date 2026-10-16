"""Continuous animation time corrected by coarse observations, never packet resets."""


class PlaybackClock:
    def __init__(self):
        self.initialized = False
        self.anchor = self.at = self.last_good = 0.0
        self.speed = 0.0
        self.playing = False
        self.duration = 0
        self.raw = None
        self.raw_at = 0.0
        self.pending = None

    def position(self, now):
        if not self.initialized:
            return 0.0
        # A lost reader may coast briefly, but cannot advance indefinitely.
        end = min(now, self.last_good + 2.5)
        elapsed = max(0, end - self.at) * 1000 if self.playing else 0
        return min(self.duration, self.anchor + elapsed * self.speed)

    def observe(self, raw, now, duration, playing, rate, coarse, reset=False):
        if reset or not self.initialized:
            self.initialized = True
            self.anchor = min(duration, raw + (500 if coarse and playing else 0))
            self.at = self.last_good = self.raw_at = now
            self.duration, self.playing, self.speed = duration, playing, rate if playing else 0
            self.raw, self.pending = raw, None
            return True, True

        if coarse and playing and self.playing and raw == self.raw and now - self.raw_at > 2:
            # An unchanged screen value is no longer a fresh observation after
            # several seconds. Do not reinterpret its eventual drift as a seek.
            self.anchor, self.at, self.speed = self.position(now), now, 0.0
            return False, False
        current = self.position(now)
        lower, upper = raw, raw + (1000 if coarse else 0)
        error = lower - current if current < lower else upper - current if current > upper else 0
        paused_seek = coarse and not playing and not self.playing and raw != self.raw
        suspicious = abs(error) > 1500 or paused_seek
        if coarse and suspicious:
            pending = self.pending
            expected = (pending[0] + max(0, now - pending[1]) * 1000 * rate
                        if pending and pending[2] and playing else pending[0] if pending else 0)
            confirmed = pending is not None and 0 < now - pending[1] <= 2 and abs(raw - expected) <= 1000
            if not confirmed:
                self.pending = (raw, now, playing)
                # Honor a pause immediately even when the OCR position is bad.
                self.anchor, self.at = current, now
                self.playing = playing
                self.speed = rate if playing else 0
                return False, False
        if suspicious:
            self.anchor = min(duration, raw + (500 if coarse and playing else 0))
            self.at = self.last_good = self.raw_at = now
            self.duration, self.playing, self.speed = duration, playing, rate if playing else 0
            self.raw, self.pending = raw, None
            return True, True

        self.pending = None
        if raw != self.raw or playing != self.playing:
            self.raw_at = now
        self.raw = raw
        self.anchor, self.at, self.duration = current, now, duration
        self.playing = playing
        self.last_good = now
        if playing:
            # A displayed second describes an interval, not its exact beginning.
            # Correct phase gradually; even negative corrections keep time forward.
            adjustment = max(-rate * .15, min(rate * .15, error / 2500))
            self.speed = rate + adjustment
        else:
            self.speed = 0
            if not coarse:
                self.anchor = raw
        return True, False
