import socket
import unittest
import tempfile
import asyncio
import csv
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from pythonosc.osc_message import OscMessage
from bridge import (Output, Publisher, START, STOP, OPEN_SESSION, START_ACK, VIBRATION_ACK, FitProFrames, JumpCheck, measure, stream, fitpro_record, standard_bpm, board_name, possible_brookstone,
                    FITPRO_AD, DutyCycle, effective_interval, IDLE_MODES, find_bands, receiver_open)
from history import History, discharge
from wristlink import next_alarm


class ProtocolTests(unittest.TestCase):
    def test_persistent_band_recovers_in_place_and_reuses_link_for_alert(self):
        clock=[100.]
        writes=[]
        events=[]
        readings=[]
        cycle=DutyCycle()
        class Band:
            services=SimpleNamespace(get_service=lambda _:None)
            is_connected=True
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def pair(self): pass
            async def start_notify(self,channel,callback): self.notify=callback
            async def write_gatt_char(self,channel,data,response):
                writes.append(data)
                if data==START:
                    n=writes.count(START)
                    clock[0]+=24 if n==1 else 8
                    if n>1:
                        frame=bytes.fromhex("cd000f150104000a2421000100000e")+bytes((n,80+n,1))
                        self.notify(None,START_ACK+frame)
                elif data==bytes.fromhex("cd000612010b000101"):
                    self.notify(None,VIBRATION_ACK)
        band=Band()
        def connect(device,timeout,disconnected_callback):
            band.close=disconnected_callback
            return band
        def deliver(bpm,*args):
            readings.append(bpm)
            if len(readings)==1: cycle.queue_alert("Timer")
            if len(readings)==3:
                band.is_connected=False
                band.close(band)
        timer=SimpleNamespace(time=lambda:clock[0],monotonic=lambda:clock[0])
        with patch("bridge.BleakClient",side_effect=connect) as clients,patch("bridge.time",timer), \
             patch("bridge.band_channels",return_value=("tx","rx")),patch("bridge.read_optional",AsyncMock(return_value=b"P")):
            from bleak.exc import BleakError
            with self.assertRaisesRegex(BleakError,"link closed"):
                asyncio.run(measure(SimpleNamespace(name="Band"),True,1,True,cycle,cycle.seen,
                                    lambda k,v:events.append((k,v)),deliver))
        self.assertEqual(readings,[82,83,84])
        self.assertEqual(clients.call_count,1)
        self.assertEqual(writes.count(OPEN_SESSION),1)
        self.assertEqual(writes.count(STOP),1)  # Only the timed-out optical request.
        self.assertEqual([v[1] for k,v in events if k=="link"],["connected","sample_timeout","link_lost"])
        self.assertTrue(any(k=="notice" and "acknowledged" in v for k,v in events))
        self.assertIsNone(cycle.queue_alert)

    def test_split_joined_and_out_of_order_notifications(self):
        result = bytes.fromhex("cd000f150104000a2421000100000e107b01")
        split = FitProFrames()
        self.assertEqual(split.feed(b"\x00"+START_ACK[:2]), [])
        self.assertEqual(split.feed(START_ACK[2:]+result[:4]), [START_ACK])
        self.assertEqual(split.feed(result[4:]), [result])
        for payload in (START_ACK+result, result+START_ACK):
            emitted = []
            class Band:
                is_connected = True
                services = SimpleNamespace(get_service=lambda _: None)
                async def __aenter__(self): return self
                async def __aexit__(self,*args): pass
                async def start_notify(self, channel, callback): self.notify = callback
                async def write_gatt_char(self, channel, data, response):
                    if data == START:
                        self.notify(None,payload[:5])
                        self.notify(None,payload[5:])
                        self.is_connected = False
                        self.disconnect(self)
            fake=Band()
            def connect(device, timeout, disconnected_callback):
                fake.disconnect=disconnected_callback
                return fake
            cycle=DutyCycle()
            with self.subTest(payload=payload), patch("bridge.BleakClient",connect), \
                 patch("bridge.band_channels",return_value=("tx","rx")), \
                 patch("bridge.read_optional",AsyncMock(return_value=b"P")):
                got=asyncio.run(measure(SimpleNamespace(name="Band"),True,15,True,cycle,cycle.seen,
                                        lambda *_:None,lambda bpm,*_:emitted.append(bpm)))
            self.assertTrue(got)
            self.assertEqual(emitted,[123])
            self.assertEqual(list(cycle.seen),[(bytes.fromhex("2421"),3600)])

    def test_osc_replay_and_expiration(self):
        with patch("bridge.SimpleUDPClient") as udp:
            wire=udp.return_value.send_message
            pub=Publisher(Output(),aliases=False)
            pub.send(94)
            wire.reset_mock()
            pub.repeat(pub.repeat_at-1)
            wire.assert_not_called()
            pub.repeat(pub.repeat_at)
            self.assertEqual([c.args[1] for c in wire.call_args_list],[94,True])
            wire.reset_mock()
            pub.stale()
            pub.repeat(float("inf"))
            self.assertEqual([c.args for c in wire.call_args_list],[("/avatar/parameters/HeartRateActive",False)])
            wire.reset_mock()
            pub.close()
            self.assertEqual([c.args[1] for c in wire.call_args_list],[0,False])

    def test_responsive_recovery_keeps_optical_budget(self):
        cycle=DutyCycle(last_start=11)
        for _ in range(9): cycle.missed(19,"Band disconnected",15)
        self.assertEqual(cycle.due(15),34)
        self.assertEqual(cycle.due(900),911)
        self.assertEqual(cycle.reason,"Band disconnected")
        cycle.success()
        self.assertEqual(cycle.reason,"")

    def test_osc_failure_does_not_stop_bluetooth_recording(self):
        async def run():
            stop=asyncio.Event()
            events=[]
            async def measuring(device,verified,interval,live,cycle,seen,emit,deliver):
                deliver(89,interval,7,True)
                deliver(91,interval,6,True)
                stop.set()
                return True
            with patch("bridge.BleakScanner.find_device_by_address",AsyncMock(return_value=object())), \
                 patch("bridge.SimpleUDPClient",side_effect=socket.gaierror("Host unavailable")), \
                 patch("bridge.measure",measuring):
                async with asyncio.timeout(3):
                    await stream("test",Output(host="invalid.example"),lambda k,v:events.append((k,v)),stop)
            self.assertEqual([v[1] for k,v in events if k=="reading"],[89,91])
            self.assertTrue(any(k=="notice" and "OSC unavailable" in v for k,v in events))
        asyncio.run(run())

    def test_continuous_reconnect_uses_pairing_without_an_advertisement(self):
        from bleak.exc import BleakError
        async def run():
            stop = asyncio.Event()
            events, attempts = [], []
            first, refreshed = object(), object()
            async def session(device, verified, interval, live, cycle, seen, emit, deliver):
                self.assertEqual((interval, live), (1, True))
                attempts.append(device)
                if len(attempts) == 1:
                    emit("link", (0, "connected", "Brookstone FitPro"))
                    deliver(82, interval, 8, True)
                    emit("link", (1, "link_lost", "Bluetooth link closed"))
                    raise BleakError("Bluetooth link closed")
                if len(attempts) == 2:
                    raise BleakError("Device unavailable")
                emit("link", (5, "connected", "Brookstone FitPro"))
                deliver(84, interval, 8, True)
                stop.set()
                return True
            with patch("bridge.paired_device", AsyncMock(side_effect=[first, refreshed])), \
                 patch("bridge.BleakScanner.find_device_by_address", AsyncMock()) as scan, \
                 patch("bridge.receiver_open", return_value=False), \
                 patch("bridge.SimpleUDPClient"), patch("bridge.measure", session):
                async with asyncio.timeout(7):
                    await stream("test", Output(), lambda k,v: events.append((k,v)), stop)
                scan.assert_not_awaited()
            self.assertEqual(attempts, [first, first, refreshed])
            self.assertEqual([v[1] for k,v in events if k == "link"],
                             ["connected", "link_lost", "retry", "retry", "connected"])
            self.assertEqual([v[1] for k,v in events if k == "reading"], [82,84])
            self.assertIn(("signal", False), events)
        asyncio.run(run())

    def test_brookstone_single_record(self):
        frame = bytes.fromhex("cd000f150104000a2421000100000e107b01")
        self.assertEqual(fitpro_record(frame), ((bytes.fromhex("2421"), 3600), 123))
        for invalid in [frame[:-1], START_ACK, frame.replace(b"\x15\x01\x04", b"\x15\x01\x03"),
                        frame[:10] + b"\x00\x02" + frame[12:]]:
            self.assertIsNone(fitpro_record(invalid))

    def test_standard_measurement(self):
        self.assertEqual(standard_bpm(bytes([0,82])), 82)
        self.assertEqual(standard_bpm(bytes([1,4,1])), 260)
        self.assertEqual(standard_bpm(bytes([4,82])), 0)
        self.assertEqual(standard_bpm(bytes([30,82,1,0,0,4])), 82)
        for invalid in [b"", b"\x01\x05", b"\x10\x51", b"\x08\x51", b"\x00\x51\x51"]:
            self.assertIsNone(standard_bpm(invalid))

    def test_osc_types_and_clear(self):
        for mode, value in [("Int", 128), ("Float", 128/255)]:
            with self.subTest(mode=mode), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
                receiver.bind(("127.0.0.1", 0))
                receiver.settimeout(1)
                pub = Publisher(Output(port=receiver.getsockname()[1], mode=mode), aliases=False)
                try:
                    pub.send(128)
                    msg = OscMessage(receiver.recv(2048))
                    self.assertEqual(msg.address, "/avatar/parameters/HeartRate")
                    self.assertIs(type(msg.params[0]), type(value))
                    self.assertAlmostEqual(msg.params[0], value, places=6)
                    self.assertEqual(OscMessage(receiver.recv(2048)).params, [True])
                    pub.clear()
                    self.assertEqual(OscMessage(receiver.recv(2048)).params, [0])
                    self.assertEqual(OscMessage(receiver.recv(2048)).params, [False])
                finally:
                    pub.close()

    def test_osc_bpm_aliases_and_custom_precedence(self):
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1",0))
            sock.settimeout(1)
            self.assertTrue(receiver_open("127.0.0.1",sock.getsockname()[1]))
            pub=Publisher(Output(port=sock.getsockname()[1],parameter="HR",mode="Float"))
            try:
                pub.send(123)
                replies=[OscMessage(sock.recv(2048)) for _ in range(7)]
                values={m.address.split("/")[-1]:m.params[0] for m in replies}
                self.assertAlmostEqual(values["HR"],123/255)
                self.assertEqual([values[k] for k in ("Heartrate3","Value","hundredsHR","tensHR","onesHR")],[123,123,1,2,3])
            finally: pub.close()

    def test_discovery_confirms_model_across_addresses(self):
        frame=bytes.fromhex("cd00152001020010000000000000024142054c4133383000")
        self.assertEqual(board_name(frame),"LA380")
        self.assertIsNone(board_name(frame[:-1]))
        ad=SimpleNamespace(local_name=None,service_uuids=[FITPRO_AD],manufacturer_data={0xaa01:b""})
        devices=[(SimpleNamespace(name=None,address=addr),ad) for addr in ("01:11:22:33:44:55","02:11:22:33:44:55")]
        self.assertTrue(possible_brookstone(*devices[0]))
        with patch("bridge.scan",AsyncMock(return_value=devices)), patch("bridge.paired_bands",AsyncMock(return_value=[])), patch("bridge.confirm_band",AsyncMock(side_effect=[True,True])):
            _,yes,pending=asyncio.run(find_bands())
        self.assertEqual(yes,[d.address for d,_ in devices])
        self.assertEqual(pending,[])

    def test_duty_cycle_does_not_restart_on_reconnect(self):
        c=DutyCycle(last_start=10)
        c.missed(15)
        self.assertEqual(c.due(60),70)
        self.assertEqual(c.due(900),910)
        for i in range(10): c.missed(35)
        self.assertEqual(c.due(15),335)
        c.success()
        self.assertEqual(c.due(15),25)
        self.assertEqual(effective_interval(60,False,IDLE_MODES[0]),900)
        self.assertIsNone(effective_interval(60,False,IDLE_MODES[1]))
        self.assertEqual(effective_interval(60,True,IDLE_MODES[1]),60)

    def test_history_isolation_export_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"history.sqlite3"
            h=History(path)
            for address,bpm in (("a",105),("b",60)):
                h.write(address,"reading",(1700000001,bpm,60,7.5))
            h.write("a","identity",("Band","FitPro",80,60,1700000000))
            h.write("a","request",(1700000000,60))
            h.write("a","link",(1700000000,"connected","Brookstone FitPro"))
            h.db.close()
            h=History(path)
            try:
                self.assertEqual(h.plot("a",0,1800000000)[1],(1,105.0,105,105))
                self.assertEqual(h.stats("a",1700000002),(1,1,7.5))
                h.export("a",Path(tmp)/"export.csv")
                with open(Path(tmp)/"export.csv",encoding="utf-8-sig") as f:
                    self.assertEqual({r["metric"] for r in csv.DictReader(f)},
                                     {"heart_rate","battery","measurement_request","bluetooth_connected"})
            finally: h.db.close()

    def test_raw_records_legacy_history_and_same_minute_comparison(self):
        frame = bytes.fromhex("cd000f150104000a2421000100000e107b01")
        t=1700000000
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"history.sqlite3"
            h=History(path)
            h.write("a","reading",(t-1,88,15,6))
            h.write("a","reading",(t+2,77,15,7))
            h.write("a","reading",(t+10,123,15,6,frame,"held"))
            h.write("a","reading",(t+59,100,15,6))
            h.write("a","reading",(t+61,111,15,6))
            h.write("b","reading",(t+20,70,15,5))
            h.db.close()
            h=History(path)
            try:
                self.assertEqual(h.pulse_check("a",t,t+60),([123,100],111.5))
                self.assertEqual(h.pulse_check("b",t,t+2),([],None))
                self.assertEqual(h.recent_band_stamps("a",t+70),[(bytes.fromhex("2421"),3600)])
                self.assertEqual(h.recent_band_stamps("a",t+100000),[])
                self.assertEqual(h.recent_band_stamps("b",t+70),[])
                h.export("a",Path(tmp)/"data.csv")
                with open(Path(tmp)/"data.csv",encoding="utf-8-sig") as f:
                    rows=list(csv.DictReader(f))
                self.assertEqual([r["band_frame_hex"] for r in rows],["","",frame.hex(),"",""])
                self.assertEqual([r["osc_held"] for r in rows],["","","yes","",""])
            finally: h.db.close()

    def test_jump_check_isolated_sustained_and_gap(self):
        for readings,expected in [
            ([92,93,70,93],[True,True,False,True]),
            ([92,93,70,72],[True,True,False,True]),
            ([70,71,100,99],[True,True,False,True]),
            ([91,93,96,100],[True]*4)]:
            checker=JumpCheck()
            self.assertEqual([checker.allow(b,i*20,15) for i,b in enumerate(readings)],expected)
        self.assertTrue(checker.allow(65,160,15))
        self.assertTrue(checker.allow(120,500,300))

    def test_held_band_sample_stays_in_history_without_positive_osc_output(self):
        async def run(enabled):
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                sock.bind(("127.0.0.1",0))
                sock.setblocking(False)
                events=[]
                stop=asyncio.Event()
                async def replay(device,verified,interval,live,cycle,seen,emit,deliver):
                    for index,bpm in enumerate((92,93,70,93)):
                        packet=bytes.fromhex("cd000f150104000a2421000100000e")+bytes((index,bpm,1))
                        deliver(bpm,15,7,True,packet)
                    stop.set()
                    return True
                with patch("bridge.BleakScanner.find_device_by_address",AsyncMock(return_value=object())), \
                     patch("bridge.measure",replay):
                    await stream("test",Output(port=sock.getsockname()[1]),lambda k,v:events.append((k,v)),
                                 stop,aliases=False,confirm_jumps=enabled)
                messages=[]
                while True:
                    try: messages.append(OscMessage(sock.recv(4096)))
                    except BlockingIOError: break
                rows=[v for k,v in events if k=="reading"]
                self.assertEqual([v[1] for v in rows],[92,93,70,93])
                self.assertEqual(rows[2][-1],"held" if enabled else "allowed")
                rates=[m.params[0] for m in messages if m.address.endswith("/HeartRate")]
                self.assertEqual(rates,[92,93,93,0] if enabled else [92,93,70,93,0])
                if enabled:
                    self.assertEqual([m.params[0] for m in messages if m.address.endswith("Active")],
                                     [True,True,False,True,False])
        for enabled in (True,False):
            asyncio.run(run(enabled))

    def test_battery_learning_and_alarm_rollover(self):
        trace=[(i*3600,100-i*3,60) for i in range(8)]
        self.assertIsNone(discharge(trace[:3]))
        self.assertEqual(discharge(trace)[0],3)
        self.assertIsNone(discharge(trace+[(8*3600,100,60)]))
        self.assertIsNone(discharge(trace+[(11*3600,73,60)]))
        self.assertEqual(datetime.fromtimestamp(next_alarm("22:30",datetime(2026,11,25,23))),datetime(2026,11,26,22,30))


if __name__ == "__main__":
    unittest.main()
