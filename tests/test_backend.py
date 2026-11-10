"""Unit and live-hardware integration tests for HDHomeRun Ubuntu Desktop."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from hdhomerun_app.backend.client import HDHomeRunClient
from hdhomerun_app.backend.epg import EPGManager
from hdhomerun_app.backend.protocol import (
    _build_packet,
    _encode_tlv,
    _parse_packet,
    TAG_GETSET_NAME,
    TYPE_GETSET_REQ,
    parse_tuner_status_string,
    udp_discover_devices,
)
from hdhomerun_app.backend.recorder import DVRManager
from hdhomerun_app.config import AppConfig, CustomChannel


class TestHDHomeRunBackend(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp_dir.name)
        self.config = AppConfig(config_dir=self.cfg_path)
        self.config.recordings_dir = str(self.cfg_path / "recordings")
        self.config.save()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_config_custom_and_block_channels(self) -> None:
        ch = CustomChannel(
            guide_number="99.1",
            guide_name="Test Custom HD",
            url="http://127.0.0.1:5004/auto/v99.1",
        )
        self.config.add_or_update_custom_channel(ch)
        self.config.set_channel_blocked("99.1", True)
        self.config.set_channel_favorite("3.1", True)

        reloaded = AppConfig(config_dir=self.cfg_path)
        self.assertTrue(reloaded.is_channel_blocked("99.1"))
        self.assertTrue(reloaded.is_channel_favorite("3.1"))
        self.assertEqual(len(reloaded.custom_channels), 1)
        self.assertEqual(reloaded.custom_channels[0].guide_name, "Test Custom HD")

    def test_protocol_packet_roundtrip_and_status_parser(self) -> None:
        payload = _encode_tlv(TAG_GETSET_NAME, b"/tuner0/status\x00")
        pkt = _build_packet(TYPE_GETSET_REQ, payload)
        parsed = _parse_packet(pkt)
        self.assertIsNotNone(parsed)
        pkt_type, tlvs = parsed  # type: ignore[misc]
        self.assertEqual(pkt_type, TYPE_GETSET_REQ)
        self.assertEqual(tlvs[TAG_GETSET_NAME], b"/tuner0/status\x00")

        st = parse_tuner_status_string(
            0,
            "ch=8vsb:599000000 lock=8vsb ss=87 snq=65 seq=100 bps=19392712 pps=1285",
            "vch=3.1 name=KCRA-TV auth=subscribed",
            "tsid=0x01\n3: 3.1 KCRA-TV\n4: 3.2 Me-TV\n",
        )
        self.assertEqual(st.frequency_hz, 599000000)
        self.assertEqual(st.lock, "8vsb")
        self.assertEqual(st.signal_strength, 87)
        self.assertEqual(st.signal_to_noise, 65)
        self.assertEqual(st.symbol_error_quality, 100)
        self.assertEqual(st.vchannel, "3.1")
        self.assertEqual(st.vname, "KCRA-TV")
        self.assertEqual(len(st.programs), 2)

    def test_xmltv_parsing(self) -> None:
        xml_file = self.cfg_path / "sample_xmltv.xml"
        xml_file.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
            <tv>
              <channel id="ch3.1">
                <display-name>3.1</display-name>
                <display-name>KCRA-TV</display-name>
              </channel>
              <programme start="20261110130000 -0800" stop="20261110140000 -0800" channel="ch3.1">
                <title>Afternoon News</title>
                <sub-title>Local Headlines</sub-title>
                <desc>Live coverage of Sacramento news.</desc>
                <category>News</category>
              </programme>
            </tv>
            """,
            encoding="utf-8",
        )
        self.config.epg_source = "xmltv"
        self.config.xmltv_path_or_url = str(xml_file)
        epg = EPGManager(self.config)
        ok, msg = epg.refresh_epg()
        self.assertTrue(ok, msg)
        self.assertIn("3.1", epg.channels)
        self.assertEqual(epg.channels["3.1"].programs[0].title, "Afternoon News")

    def test_live_hardware_discovery_and_lineup(self) -> None:
        client = HDHomeRunClient(self.config)
        devices = client.discover_devices()
        if not devices:
            self.skipTest("No live HDHomeRun hardware reachable on LAN")
        dev = devices[0]
        self.assertTrue(dev.local_ip)
        lineup = client.get_lineup(dev, show_all=True)
        self.assertGreater(len(lineup), 0)

        tuners = client.get_tuners_telemetry(dev)
        self.assertGreaterEqual(len(tuners), 2)

        # Verify live 2-second DVR stream capture
        dvr = DVRManager(self.config)
        rec = dvr.start_instant_recording(
            channel_number="3.1",
            channel_name="KCRA-TV",
            stream_url=f"http://{dev.local_ip}:5004/auto/v3.1",
            duration_minutes=1,
            title="Live Test Capture",
        )
        time.sleep(2.2)
        dvr.stop_or_cancel_recording(rec.id)
        self.assertTrue(Path(rec.output_path).exists())
        self.assertGreater(rec.bytes_written, 10000)


if __name__ == "__main__":
    unittest.main()
