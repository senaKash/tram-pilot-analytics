"""Два явно зарегистрированных анализатора исходных полей Track."""
from .models import Observation


class SubsystemErrorAnalyzer:
    """Выделяет Error каждой подсистемы без смешения их состояний."""
    identifier = "subsystem_error"
    version = "1"
    signals = ("m_danger_obj_status", "m_trafficlight_status", "m_speed_limit_status")

    def observe(self, track: dict) -> list[Observation]:
        """Возвращает по одному наблюдению на сигнал, включая неизвестные состояния."""
        values = track["attributes"]
        result = []
        for signal in self.signals:
            raw = values.get(signal)
            state = "bad" if raw == "Error" else "clear" if raw == "ProperlyWork" else "unknown"
            prefix = signal.removesuffix("_status")
            context = {key: values[key] for key in
                       (f"{prefix}_req", f"{prefix}_act", "is_adas_button_on") if key in values}
            result.append(_observation(track, self.identifier, signal, state,
                                       signal in values, raw, context))
        return result


class SensorFreshnessAnalyzer:
    """Различает неактуальные, актуальные и неизвестные данные устройств."""
    identifier = "sensor_freshness"
    version = "1"
    signals = ("ubloxGps", "leftImage", "multiImages", "minsEth", "dbwFbTram",
               "roadModel", "tramPlannerService")

    def observe(self, track: dict) -> list[Observation]:
        """Считает только явное false неактуальностью, а true снятием признака."""
        values = track["attributes"]
        result = []
        for signal in self.signals:
            raw = values.get(signal)
            state = "bad" if raw is False else "clear" if raw is True else "unknown"
            result.append(_observation(track, self.identifier, signal, state,
                                       signal in values, raw, {}))
        return result


def _observation(track, category, signal, state, present, raw, context):
    return Observation(track["id"], track["observed_at"], track["loco_number"],
                       track["device_id"], track["cabin_type"], category, signal,
                       state, present, raw, context)


# Явный реестр фиксирует набор и версии правил каждого запуска.
ANALYZERS = (SubsystemErrorAnalyzer(), SensorFreshnessAnalyzer())
