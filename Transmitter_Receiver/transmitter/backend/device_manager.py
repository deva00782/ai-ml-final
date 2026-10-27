"""Device registry and state manager: holds references to mobile/desktop backends and tracks active views."""


class DeviceManager:
    def __init__(self):
        self._mobile = {"mqtt_service": None, "pipeline_scheduler": None}
        self._desktop = {"controller": None}

    def register_mobile(self, mqtt_service, pipeline_scheduler=None):
        self._mobile["mqtt_service"] = mqtt_service
        self._mobile["pipeline_scheduler"] = pipeline_scheduler

    def register_desktop(self, controller):
        self._desktop["controller"] = controller

    def get_device(self, target):
        if target == "mobile":
            return self._mobile
        if target == "desktop":
            return self._desktop
        raise ValueError(f"Unknown target device: {target}")

    def get_mobile(self):
        return self._mobile

    def get_desktop(self):
        return self._desktop

    def is_ready(self, target):
        device = self.get_device(target)
        if target == "mobile":
            return device["mqtt_service"] is not None and device["mqtt_service"].client is not None
        if target == "desktop":
            return device["controller"] is not None
        return False


class StateManager:
    """Tracks which dashboard view is active and per-device connection state."""

    def __init__(self):
        self._active_dashboard = "master"
        self._devices = {
            "mobile": {"registered": False, "connected": False},
            "desktop": {"registered": False, "connected": False},
        }

    def set_active_dashboard(self, name):
        self._active_dashboard = name

    def get_active_dashboard(self):
        return self._active_dashboard

    def register_device(self, name, registered=True):
        if name in self._devices:
            self._devices[name]["registered"] = registered

    def set_connected(self, name, connected):
        if name in self._devices:
            self._devices[name]["connected"] = connected

    def get_state(self):
        return {
            "active_dashboard": self._active_dashboard,
            "devices": {k: dict(v) for k, v in self._devices.items()},
        }


device_manager = DeviceManager()
state_manager = StateManager()
