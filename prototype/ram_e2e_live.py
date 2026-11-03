"""Explicit OPEN-FIXTURE nine-point scan/upload/reboot diagnostic.

Compile/upload this helper separately, then run from FRIENDLY REPL on a fresh
interpreter: import ram_e2e_live; ram_e2e_live.run(). It uses production main.run,
UI, engine, network and RestartRequested cleanup. No settings/scan-file writes.
Only a successful exact nine-point ACK is a pass. Never run with a DUT fitted.
The diagnostic drain grid is 0.1/0.6/1.1 V: the gate-dependent low-end drain
droop remains unresolved, so this upload test explicitly excludes VDS=0.
Production voltage guards, saved settings and calibration are unchanged.
"""

import main  # Reserve production buffers before constructing diagnostic state.

BaseApp = main.App
BaseCloud = main.RamCloudManager
TIMEOUT_MS = 5 * 60 * 1000
GRID = {"drain_start_uv": 100000, "drain_stop_uv": 1100000, "drain_step_uv": 500000,
        "gate_start_uv": -1000000, "gate_stop_uv": 1000000, "gate_step_uv": 1000000}


class DiagnosticStop(BaseException):
    """Unwind production finally without requesting a reboot or retry."""


def emit(marker, value):
    print(marker, main.storage.json.dumps(value))


class ObservedCloud(BaseCloud):
    def _ack_matches(self, result):
        matched = BaseCloud._ack_matches(self, result)
        if matched:
            # Retain only already-validated ACK fields, never arbitrary server
            # text, URLs, headers or credentials. Production owns acceptance.
            self.e2e_ack = (result["scan_id"], result["sha256"],
                            result["point_count"], result["sheet_id"])
        return matched


class ObservedApp(BaseApp):
    def __init__(self, *args, **kwargs):
        BaseApp.__init__(self, *args, **kwargs)
        if (self.settings["version"] != 3 or self.settings["source_voltage_uv"] != 3300000 or
                self.settings["ina_average_count"] not in main.model.INA_AVERAGE_COUNTS):
            raise DiagnosticStop("unexpected_saved_settings")
        self.e2e_ina_count = self.settings["ina_average_count"]
        # BaseApp owns a normalized RAM copy; Settings persistence is not used.
        self.settings.update(GRID)
        main.model.validate_scan_calibration(self.calibration, self.settings)
        self._reset_plot_bounds()
        self.e2e_started = main.time.ticks_ms()
        self.e2e_queued = False
        self.e2e_points = 0
        self.e2e_state = None

    def _request_restart(self):
        if self._restart_pending or self._restart_called:
            return BaseApp._request_restart(self)
        cloud = self.cloud
        if cloud is not None and cloud.state == "complete":
            scan = cloud.scan
            ack = getattr(cloud, "e2e_ack", None)
            if (scan is None or scan.outcome != "complete" or scan.point_count != 9
                    or len(self._scan_buffer) != 9 or ack is None
                    or ack[:3] != (scan.scan_id, scan.sha256, 9)
                    or scan.settings["ina_average_count"] != self.e2e_ina_count):
                raise DiagnosticStop("invalid_final_ack")
            emit("RAM_E2E_ACK", {"exact_match": True, "point_count": ack[2],
                 "scan_id": ack[0], "sha256": ack[1], "sheet_id": ack[3],
                 "cloud_state": cloud.state})
        else:
            print("RAM_E2E_ABORT cancelled")
        # Keep actual discard -> RestartRequested -> main finally -> soft_reset.
        return BaseApp._request_restart(self)

    def tick(self, now_ms):
        if main.time.ticks_diff(now_ms, self.e2e_started) >= TIMEOUT_MS:
            raise DiagnosticStop("timeout")
        BaseApp.tick(self, now_ms)
        state = (self.screen, self.engine.state,
                 self.cloud.state, self.wifi.state)
        if state != self.e2e_state:
            self.e2e_state = state
            emit("RAM_E2E_STATE", {"screen": state[0], "engine": state[1],
                 "cloud": state[2], "wifi": state[3], "free_heap": main.gc.mem_free()})
        while self.e2e_points < len(self._scan_buffer):
            row = self._scan_buffer.get(self.e2e_points)
            emit("RAM_E2E_POINT", {"index": self.e2e_points, "vds": row[0],
                 "vgs": row[1], "id_a": row[2], "ig_a": row[3], "flags": row[4]})
            self.e2e_points += 1
        if self.screen in ("scan_error", "upload_error"):
            raise DiagnosticStop(self.screen)
        if self.engine.state == "partial":
            raise DiagnosticStop("partial_scan")
        if not self.e2e_queued:
            if self.screen != "home" or self.dirty:
                raise DiagnosticStop("home_not_ready")
            self.e2e_queued = True
            emit("RAM_E2E_BEGIN", {"point_count": 9, "settings_version": 3,
                 "ina_average_count": self.e2e_ina_count,
                 "acquisition": main.model.acquisition_metadata(self.settings),
                 "drain_start_uv": self.settings["drain_start_uv"],
                 "drain_stop_uv": self.settings["drain_stop_uv"],
                 "drain_step_uv": self.settings["drain_step_uv"],
                 "gate_start_uv": self.settings["gate_start_uv"],
                 "gate_stop_uv": self.settings["gate_stop_uv"],
                 "gate_step_uv": self.settings["gate_step_uv"],
                 "low_boundary_excluded": True,
                 "source_voltage_uv": self.settings["source_voltage_uv"],
                 "driver_supply_uv": self.settings["driver_supply_uv"],
                 "free_heap": main.gc.mem_free()})
            self.action("right")  # Home has been painted by the actual UI tick.


def run():
    main.App, main.RamCloudManager = ObservedApp, ObservedCloud
    try:
        main.run()
    except DiagnosticStop as error:
        # Only fixed local diagnostic codes reach this path.
        print("RAM_E2E_ABORT", str(error))
    finally:
        main.App, main.RamCloudManager = BaseApp, BaseCloud
    # Success resets the VM inside main.run and can never reach this marker.
    print("RAM_E2E_RETURNED")


if __name__ == "__main__":
    run()
