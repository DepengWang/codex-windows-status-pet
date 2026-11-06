import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api import bluetooth_battery_api as battery


NODE_GPT = r"BTHENUM\DEV_9C5A8A6F775D"
NODE_CC = r"BTHENUM\DEV_9C5A8A6F3291"
ENDPOINT = r"SWD\MMDEVAPI\ENDPOINT1"


def fake_windows(endpoint_names, node_names, levels):
    """Patch the CfgMgr32 layer with an in-memory device table."""
    names = {ENDPOINT: endpoint_names[0] if endpoint_names else ""}
    names.update(node_names)

    def ids(class_guid):
        if class_guid == battery._CLASS_AUDIO_ENDPOINT:
            return [ENDPOINT] if endpoint_names else []
        return list(node_names)

    def prop(instance_id, key, expected_type):
        return bytes([levels[instance_id]]) if instance_id in levels else None

    return (
        mock.patch.object(battery, "_present_instance_ids", ids),
        mock.patch.object(battery, "_friendly_name", lambda instance_id: names.get(instance_id, "")),
        mock.patch.object(battery, "_property", prop),
    )


def read(endpoint_names, node_names, levels):
    patches = fake_windows(endpoint_names, node_names, levels)
    with patches[0], patches[1], patches[2]:
        return battery.read_connected_dji_mic_mini_2_battery()


class MicBatteryTests(unittest.TestCase):
    NODES = {
        NODE_GPT: "DJI Mic Mini 2-6F775D Hands-Free AG",
        NODE_CC: "DJI Mic Mini 2-6F3291 Hands-Free AG",
    }

    def by_label(self, reading):
        return {item["label"]: item for item in reading["devices"]}

    def test_only_transmitter_with_live_endpoint_is_connected(self):
        reading = read(
            ["Headset (DJI Mic Mini 2-6F775D Hands-Free)"], self.NODES, {NODE_GPT: 40, NODE_CC: 90}
        )
        devices = self.by_label(reading)
        self.assertEqual(devices["GPT"], {"label": "GPT", "connected": True, "level": 40})
        self.assertEqual(devices["CC"], {"label": "CC", "connected": False, "level": None})
        self.assertEqual((reading["label"], reading["level"]), ("GPT", 40))

    def test_two_connected_transmitters_report_the_lowest_level(self):
        names = [
            "Headset (DJI Mic Mini 2-6F775D Hands-Free)",
            "Headset (DJI Mic Mini 2-6F3291 Hands-Free)",
        ]
        patches = fake_windows(names, self.NODES, {NODE_GPT: 40, NODE_CC: 90})
        with patches[0], patches[1], patches[2], mock.patch.object(
            battery, "_present_instance_ids",
            lambda guid: [ENDPOINT, ENDPOINT + "B"] if guid == battery._CLASS_AUDIO_ENDPOINT else list(self.NODES),
        ), mock.patch.object(
            battery, "_friendly_name",
            lambda i: {ENDPOINT: names[0], ENDPOINT + "B": names[1]}.get(i, self.NODES.get(i, "")),
        ):
            reading = battery.read_connected_dji_mic_mini_2_battery()
        self.assertEqual((reading["label"], reading["level"], reading["selected"]), ("MIC", 40, "GPT"))

    def test_no_audio_endpoint_means_everything_is_offline(self):
        reading = read([], self.NODES, {NODE_GPT: 40, NODE_CC: 90})
        self.assertFalse(reading["connected"])
        self.assertTrue(all(not item["connected"] and item["level"] is None for item in reading["devices"]))

    def test_missing_or_out_of_range_level_is_unknown_not_stale(self):
        reading = read(["Headset (DJI Mic Mini 2-6F775D Hands-Free)"], self.NODES, {NODE_GPT: 250})
        self.assertEqual(self.by_label(reading)["GPT"], {"label": "GPT", "connected": True, "level": None})

    def test_unknown_transmitters_are_ignored(self):
        nodes = {r"BTHENUM\DEV_OTHER": "DJI Mic Mini 2-AAAAAA Hands-Free AG"}
        reading = read(["Headset (DJI Mic Mini 2-AAAAAA Hands-Free)"], nodes, {r"BTHENUM\DEV_OTHER": 77})
        self.assertFalse(reading["connected"])

    @unittest.skipUnless(sys.platform == "win32", "CfgMgr32 is Windows-only")
    def test_live_windows_call_returns_both_known_rows_without_error(self):
        reading = battery.read_connected_dji_mic_mini_2_battery()
        self.assertEqual({item["label"] for item in reading["devices"]}, {"GPT", "CC"})


if __name__ == "__main__":
    unittest.main()
