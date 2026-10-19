import math
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from manager import resources


class TemperaturesTest(unittest.TestCase):
    def setUp(self):
        self.cache = patch.object(resources, "_temperature_cache", (0.0, {}))
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_identifies_components_without_guessing_thermal_zones(self):
        for source, label, expected in [
            ("coretemp", "Package id 0", "cpu"),
            ("k10temp", "Tctl", "cpu"),
            ("cpu_thermal", "", "cpu"),
            ("/amdcpu/0", "Core #1", "cpu"),
            ("/intelcpu/0", "CPU Package", "cpu"),
            ("spd5118-i2c-0-50", "", "ram"),
            ("jc42", "", "ram"),
            ("/lpc/0", "DIMM A1", "ram"),
            ("acpitz", "", None),
            ("nvme", "Composite", None),
            ("/gpu-nvidia/0", "GPU Memory", None),
        ]:
            with self.subTest(source=source, label=label):
                self.assertEqual(resources._temperature_kind(source, label), expected)

    def test_hottest_valid_readings_and_cache(self):
        rows = [("coretemp", "", value) for value in (51, 69, None, "bad", math.nan, math.inf, -1, 200)]
        rows += [("jc42", "", 43), ("jc42", "", 48), ("nvme", "", 95)]
        with patch.object(resources.sys, "platform", "win32"), \
                patch.object(resources, "_windows_temperatures", return_value=rows) as read:
            result = resources.temperatures()
            self.assertEqual(result, {"cpu_temp_c": 69, "ram_temp_c": 48})
            result["cpu_temp_c"] = 999
            self.assertEqual(resources.temperatures()["cpu_temp_c"], 69)
            read.assert_called_once()

    def test_psutil_sensor_groups(self):
        sensor = types.SimpleNamespace
        groups = {"coretemp": [sensor(label="Core 0", current=62)],
                  "spd5118": [sensor(label="", current=42)]}
        with patch.object(resources.sys, "platform", "linux"), \
                patch.object(resources.psutil, "sensors_temperatures", return_value=groups, create=True):
            self.assertEqual(resources.temperatures(), {"cpu_temp_c": 62, "ram_temp_c": 42})

    def test_unavailable_psutil_sensors(self):
        for error in (OSError, NotImplementedError):
            with self.subTest(error=error), patch.object(resources, "_temperature_cache", (0.0, {})), \
                    patch.object(resources.sys, "platform", "linux"), \
                    patch.object(resources.psutil, "sensors_temperatures", side_effect=error, create=True):
                self.assertEqual(resources.temperatures(), {"cpu_temp_c": None, "ram_temp_c": None})

    def test_windows_fallback_and_com_cleanup(self):
        class ComError(Exception):
            pass

        com = types.SimpleNamespace(CoInitialize=MagicMock(), CoUninitialize=MagicMock())
        provider = MagicMock()
        provider.ExecQuery.return_value = [types.SimpleNamespace(Parent="/intelcpu/0", Name="CPU Package", Value=63)]
        get_object = MagicMock(side_effect=[ComError(), provider])
        modules = {"pythoncom": com, "pywintypes": types.SimpleNamespace(com_error=ComError),
                   "win32com.client": types.SimpleNamespace(GetObject=get_object)}
        with patch.dict(sys.modules, modules):
            self.assertEqual(resources._windows_temperatures(), [("/intelcpu/0", "CPU Package", 63)])
        self.assertEqual(get_object.call_count, 2)
        com.CoInitialize.assert_called_once()
        com.CoUninitialize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
