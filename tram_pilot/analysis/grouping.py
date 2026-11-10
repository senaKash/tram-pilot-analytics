"""Построение сегментов и эпизодов без обращений к PostgreSQL."""
from .models import Episode, Observation, Segment


class SegmentBuilder:
    """Держит только текущий сегмент одного сигнала."""

    def __init__(self, gap_seconds: float = 30):
        self.gap_seconds = gap_seconds
        self.current: Segment | None = None
        self.pending_nonbad: Observation | None = None

    def _close_with_pending(self) -> Segment | None:
        pending = self.pending_nonbad
        self.pending_nonbad = None
        current, self.current = self.current, None
        if current and pending:
            current.close_reason = ("observed_clearance" if pending.state == "clear"
                                    else "unknown_state")
            current.clear_track_id = pending.track_id if pending.state == "clear" else None
            current.closing_observation = pending
        return current

    def feed(self, observation: Observation) -> tuple[Segment | None, Segment | None, bool]:
        """Возвращает закрытый, активный сегмент и признак его создания."""
        closed = None
        pending = self.pending_nonbad
        if pending:
            if observation.at == pending.at:
                if observation.state == "bad":
                    # Противоречивый снимок того же времени не доказывает снятие.
                    self.pending_nonbad = None
                    return self._feed_bad(observation)
                if observation.state == "unknown":
                    self.pending_nonbad = observation
                return None, None, False
            closed = self._close_with_pending()
        current = self.current
        if observation.state != "bad":
            if current and current.last_bad_at == observation.at:
                # Error и снятие на одной метке времени образуют конфликт,
                # а не последовательность «восстановление → новая ошибка».
                return closed, None, False
            self.pending_nonbad = observation
            return closed, None, False
        gap_closed, active, created = self._feed_bad(observation)
        return closed or gap_closed, active, created

    def _feed_bad(self, observation: Observation):
        current = self.current
        if current and (observation.at - current.last_bad_at).total_seconds() <= self.gap_seconds:
            current.last_bad_at = observation.at
            current.last_track_id = observation.track_id
            current.bad_count += 1
            return None, current, False
        if current:
            current.close_reason = "telemetry_gap"
        self.current = Segment(observation.at, observation.at,
                               observation.track_id, observation.track_id)
        return current, self.current, True

    def finish(self) -> Segment | None:
        """Закрывает незавершённый сегмент концом доступной выборки."""
        if self.pending_nonbad:
            return self._close_with_pending()
        result, self.current = self.current, None
        return result


class EpisodeBuilder:
    """Соединяет соседние сегменты без наблюдавшегося снятия признака."""

    def __init__(self, bridge_seconds: float = 90):
        self.bridge_seconds = bridge_seconds
        self.current: Episode | None = None
        self.previous_segment: Segment | None = None

    def feed(self, segment: Segment) -> tuple[Episode | None, Episode, bool]:
        """Возвращает закрытый эпизод, активный эпизод и признак создания."""
        previous = self.previous_segment
        gap = ((segment.first_bad_at - previous.last_bad_at).total_seconds()
               if previous else 0)
        bridge = (previous is not None and previous.close_reason in
                  {"telemetry_gap", "unknown_state"}
                  and 0 < gap <= self.bridge_seconds)
        closed = None
        if not bridge:
            closed = self.current
            self.current = Episode(segment.first_bad_at, segment.last_bad_at,
                                   segment.first_track_id, segment.last_track_id,
                                   segment.bad_count)
        else:
            self.current.last_bad_at = segment.last_bad_at
            self.current.last_track_id = segment.last_track_id
            self.current.bad_count += segment.bad_count
            self.current.segment_count += 1
            # Пробел не выдаётся за подтверждённую непрерывную неисправность.
            self.current.bridged_gap_seconds += gap
        self.current.close_reason = segment.close_reason
        self.previous_segment = segment
        return closed, self.current, not bridge

    def finish(self) -> Episode | None:
        """Возвращает последний эпизод сигнала."""
        result, self.current = self.current, None
        return result
