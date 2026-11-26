"""Bluetooth sensor input and VRChat OSC output for WristLink."""

import asyncio
import contextlib
import ipaddress
import re
import socket
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak.uuids import normalize_uuid_str
from pythonosc.udp_client import SimpleUDPClient
import psutil


HR_SERVICE = normalize_uuid_str("180d")
HR_MEASUREMENT = normalize_uuid_str("2a37")
BAND_SERVICE = "7e400001-b5a4-f393-e0a9-e50e24dcca9d"
BAND_TX = "7e400002-b5a3-f393-e0a9-e50e24dcca9d"
BAND_RX = "7e400003-b5a3-f393-e0a9-e50e24dcca9d"
START = bytes.fromhex("cd 00 07 12 01 24 00 02 00 01")
STOP = START[:-1] + b"\x00"
START_ACK = bytes.fromhex("dc 00 05 12 24 00 0a 01")
# The FitPro client declares an unpaired LE session on connection.
OPEN_SESSION = bytes.fromhex("cd 00 06 12 01 0a 00 01 02")
BOARD_INFO = bytes.fromhex("cd 00 05 20 01 02 00 00")
FITPRO_AD = normalize_uuid_str("fee7")
DEFAULT_PROFILE = "Continuous · 1 second"
INTERVALS = {DEFAULT_PROFILE: 1, "Battery saver · 1 minute": 60, "Responsive · 15 seconds": 15,
             "Endurance · 5 minutes": 300}


class UnsupportedSensor(Exception):
    pass


class OscError(Exception):
    pass


class JumpCheck:
    """Confirm an isolated large departure from two recent agreeing spot readings."""
    def __init__(self):
        self.baseline = deque(maxlen=2)
        self.pending = None
        self.last_accepted = float("-inf")

    def allow(self, bpm, now, interval):
        if interval > 60 or now - self.last_accepted > 2*interval + 15:
            self.baseline.clear()
            self.pending = None
        if interval <= 60 and len(self.baseline) == 2:
            a, b = self.baseline
            if abs(a-b) <= 10 and min(abs(bpm-a),abs(bpm-b)) >= 20:
                if self.pending is None or abs(bpm-self.pending) > 10:
                    self.pending = bpm
                    return False
                self.baseline.clear()
                self.baseline.append(self.pending)
        self.pending = None
        self.last_accepted = now
        self.baseline.append(bpm)
        return True


@dataclass(frozen=True)
class Output:
    host: str = "127.0.0.1"
    port: int = 9000
    parameter: str = "HeartRate"
    mode: str = "Int"
    active: str = "HeartRateActive"

    def validate(self):
        if not self.host.strip() or not 1 <= self.port <= 65535:
            raise ValueError("Enter a destination host and a UDP port from 1 to 65535.")
        if self.mode not in ("Int", "Float"):
            raise ValueError("Choose Int or Float for the BPM parameter.")
        for name in [self.parameter] + ([self.active] if self.active else []):
            if not name or any(not re.fullmatch(r"[^\s\x00-\x1f#*,?\[\]{}]+", part) for part in name.split("/")):
                raise ValueError("Enter a parameter name, such as HeartRate or OSC/HeartRate.")
        if self.parameter == self.active:
            raise ValueError("Use different names for BPM and signal status.")


class Publisher:
    def __init__(self, config, aliases=True):
        config.validate()
        self.config = config
        self.client = None
        self.live = False
        self.aliases = aliases
        self.last_bpm = 0
        self.repeat_at = 0
        self.repeat_seconds = 4

    def send(self, bpm):
        n = min(bpm, 255)
        value = n if self.config.mode == "Int" else n / 255.0
        values = {}
        if self.aliases:
            values.update({"HR": n, "Heartrate3": n, "VRCOSC/Heartrate/Value": n,
                           "onesHR": n % 10, "tensHR": n // 10 % 10, "hundredsHR": n // 100})
        # Explicit mappings always win, including the OSC type.
        values[self.config.parameter] = value
        if self.config.active:
            values[self.config.active] = bpm > 0
        self.live = bpm > 0
        self.last_bpm = n
        self.repeat_at = time.monotonic() + self.repeat_seconds
        self._send(values)

    def _send(self, values):
        try:
            if self.client is None:
                self.client = SimpleUDPClient(self.config.host, self.config.port, family=socket.AF_INET)
            for name, value in values.items():
                self.client.send_message("/avatar/parameters/" + name, value)
        except OSError as exc:
            if self.client:
                self.client.close()
                self.client = None
            raise OscError(f"OSC unavailable · {exc}") from exc

    def repeat(self, now):
        if self.live and now >= self.repeat_at:
            due = self.repeat_at
            self.send(self.last_bpm)
            self.repeat_at = due + (1 + int((now-due)/self.repeat_seconds))*self.repeat_seconds

    def stale(self):
        if self.live:
            self.live = False
            if self.config.active:
                self._send({self.config.active: False})

    def clear(self):
        if self.last_bpm:
            self.send(0)

    def close(self):
        try:
            self.clear()
        finally:
            if self.client:
                self.client.close()


def standard_bpm(data):
    """None means malformed. Zero means signal/contact unavailable."""
    if not data:
        return None
    flags = data[0]
    width = 2 if flags & 1 else 1
    offset = 1 + width + (2 if flags & 8 else 0)
    if len(data) < offset:
        return None
    tail = len(data) - offset
    if (flags & 16 and (tail < 2 or tail % 2)) or (not flags & 16 and tail):
        return None
    if flags & 4 and not flags & 2:
        return 0
    return int.from_bytes(data[1:1+width], "little")


def fitpro_record(frame):
    """A single spot result. Bulk history, acknowledgements and other metrics are ignored."""
    if (len(frame) not in (17, 18) or frame[:1] != b"\xcd" or
            frame[3:6] != b"\x15\x01\x04" or frame[10:12] != b"\x00\x01" or
            int.from_bytes(frame[1:3], "big") != len(frame)-3 or
            int.from_bytes(frame[6:8], "big") != len(frame)-8):
        return None
    seconds = int.from_bytes(frame[12:16], "big")
    if seconds >= 86400:
        return None
    return (bytes(frame[8:10]), seconds), frame[16]


def received_ack(frame):
    return bytes([0xdc, 0, 5, frame[3], 1]) + len(frame).to_bytes(2, "big") + b"\x01"


class FitProFrames:
    """A BLE notification can contain a partial frame or several complete frames."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 3:
            size = 3 + int.from_bytes(self.buffer[1:3], "big")
            if self.buffer[0] not in (0xcd, 0xdc) or not 8 <= size <= 4096:
                del self.buffer[0]
                continue
            if len(self.buffer) < size:
                break
            frames.append(bytes(self.buffer[:size]))
            del self.buffer[:size]
        return frames


async def model_on_connection(client, tx, rx):
    q = asyncio.Queue()
    decoder = FitProFrames()
    await client.start_notify(rx, lambda _, data: q.put_nowait(bytes(data)))
    try:
        await client.write_gatt_char(tx, BOARD_INFO, response=True)
        async with asyncio.timeout(4):
            while True:
                for frame in decoder.feed(await q.get()):
                    model = board_name(frame)
                    if model is not None:
                        await client.write_gatt_char(tx, received_ack(frame), response=True)
                        return model
    finally:
        if client.is_connected:
            await client.stop_notify(rx)


async def scan(seconds=8):
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    return sorted(devices.values(), key=lambda pair: -pair[1].rssi)


async def paired_device(address):
    """A bonded band can already be connected to Windows and no longer advertising."""
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address):
        return None
    with contextlib.suppress(OSError, TimeoutError):
        async with asyncio.timeout(4):
            native = await BluetoothLEDevice.from_bluetooth_address_async(int(address.replace(":",""),16))
            if native is not None:
                try:
                    if native.device_information.pairing.is_paired:
                        return BLEDevice(address, native.name, native.device_information.id)
                finally:
                    native.close()
    return None


async def read_optional(client, uuid):
    if client.services.get_characteristic(uuid) is not None:
        with contextlib.suppress(BleakError, OSError, TimeoutError):
            return bytes(await asyncio.wait_for(client.read_gatt_char(uuid, use_cached=False), 3))
    return b""


def possible_brookstone(device, ad):
    names = {(value or "").strip().upper() for value in (device.name, ad.local_name if ad else None)}
    return bool(names & {"BROOKSTONE", "LA380"}) or (
        ad is not None and FITPRO_AD in ad.service_uuids and 0xaa01 in ad.manufacturer_data)


def board_name(frame):
    if (len(frame) < 16 or frame[:1] != b"\xcd" or frame[3:6] != b"\x20\x01\x02"
            or int.from_bytes(frame[1:3], "big") != len(frame)-3
            or int.from_bytes(frame[6:8], "big") != len(frame)-8):
        return None
    index = 15 + frame[14]
    if index >= len(frame) or index + 1 + frame[index] > len(frame):
        return None
    return frame[index+1:index+1+frame[index]].decode("ascii", errors="replace")


def band_channels(client):
    service = client.services.get_service(BAND_SERVICE)
    if service:
        tx, rx = service.get_characteristic(BAND_TX), service.get_characteristic(BAND_RX)
        if tx and rx and "write" in tx.properties and "notify" in rx.properties:
            return tx, rx
    return None


async def confirm_band(device):
    """Read the model over the known transport. Discovery never starts the optical sensor."""
    async with asyncio.timeout(18), BleakClient(device, timeout=10) as client:
        channels = band_channels(client)
        if not channels:
            return False
        return (await model_on_connection(client, *channels)).upper() == "LA380"


async def paired_bands():
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import DeviceInformation
    devices = []
    with contextlib.suppress(OSError, TimeoutError):
        async with asyncio.timeout(5):
            selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
            for info in await DeviceInformation.find_all_async(selector, []):
                if info.name.upper() not in {"BROOKSTONE","LA380"}:
                    continue
                native = await BluetoothLEDevice.from_id_async(info.id)
                if native:
                    try:
                        address = native.bluetooth_address.to_bytes(6, "big").hex(":").upper()
                        devices.append(BLEDevice(address,info.name,info.id))
                    finally:
                        native.close()
    return devices


async def find_bands():
    devices, bonded = await asyncio.gather(scan(10), paired_bands())
    addresses = {device.address for device,_ in devices}
    devices.extend((device,None) for device in bonded if device.address not in addresses)
    confirmed, unresolved = [], []
    for device, ad in devices:
        if possible_brookstone(device, ad):
            try:
                if await confirm_band(device):
                    confirmed.append(device.address)
            except (BleakError, OSError, TimeoutError):
                unresolved.append(device.address)
    return devices, confirmed, unresolved


def local_destination(host):
    if host.lower() == "localhost":
        return "127.0.0.1"
    try:
        return host if ipaddress.ip_address(host).is_loopback else None
    except ValueError:
        return None


def receiver_open(host, port):
    return any(c.laddr.port == port and c.laddr.ip in (host, "0.0.0.0")
               for c in psutil.net_connections(kind="udp4") if c.laddr)


IDLE_MODES = ("Every 15 minutes", "Pause sampling", "Use selected rate")
DEFAULT_IDLE = IDLE_MODES[2]


def effective_interval(interval, listening, idle_mode):
    if listening or idle_mode == IDLE_MODES[2]:
        return interval
    return 900 if idle_mode == IDLE_MODES[0] else None


@dataclass
class DutyCycle:
    last_start: float = float("-inf")
    retry_at: float = 0
    misses: int = 0
    seen: deque = field(default_factory=lambda: deque(maxlen=8192))
    reason: str = ""
    battery_due: float = 0
    battery_interval: int = 0
    check_until: float = 0
    pair_after: float = 0
    queue_alert: Callable[[str], None] | None = None

    def due(self, interval):
        return max(self.last_start + interval, self.retry_at)

    def missed(self, now, reason="No reading", interval=300):
        self.misses = min(self.misses + 1, 7)
        self.reason = reason
        backoff = 2 ** (self.misses - 1)
        self.retry_at = now + (min(5, backoff) if interval == 1 else min(300, max(15, interval), 5*backoff))

    def success(self):
        self.misses = 0
        self.retry_at = 0
        self.reason = ""


async def vibration(address):
    """A short FitPro find-band cue. Never changes the stored alarm list."""
    device = await paired_device(address) or await BleakScanner.find_device_by_address(address, timeout=10)
    if not device:
        raise ValueError("Band out of range. The wrist alert could not be delivered.")
    async with asyncio.timeout(18), BleakClient(device, timeout=12) as client:
        channels = band_channels(client)
        if not channels:
            raise UnsupportedSensor("Wrist vibration needs the Brookstone FitPro channel.")
        tx, rx = channels
        ack = asyncio.Event()
        decoder = FitProFrames()
        def notify(_,data):
            if VIBRATION_ACK in decoder.feed(data): ack.set()
        await client.start_notify(rx,notify)
        await vibrate_connected(client,tx,ack)


VIBRATION_ACK = bytes.fromhex("dc 00 05 12 0b 00 09 01")


async def vibrate_connected(client,tx,ack):
    on = bytes.fromhex("cd 00 06 12 01 0b 00 01 01")
    ack.clear()
    try:
        async with asyncio.timeout(5):
            await client.write_gatt_char(tx,on,response=True)
            await ack.wait()
        await asyncio.sleep(.65)
    finally:
        if client.is_connected:
            with contextlib.suppress(BleakError,OSError,TimeoutError):
                await asyncio.wait_for(client.write_gatt_char(tx,on[:-1]+b"\x00",response=True),2)


async def measure(device, verified, interval, continuous, cycle, seen, emit, deliver):
    disconnected = asyncio.Event()
    packets = asyncio.Queue()
    client = BleakClient(device, timeout=12, disconnected_callback=lambda _: disconnected.set())
    async with asyncio.timeout(55) as operation_timeout, client:
        name = "Brookstone LA380" if verified else (
            (await read_optional(client, "2a00")).decode(errors="replace").strip("\0") or device.name or device.address)
        service = client.services.get_service(HR_SERVICE)
        hr = service.get_characteristic(HR_MEASUREMENT) if service else None
        channels = band_channels(client)
        if channels and not verified:
            model = await model_on_connection(client, *channels)
            verified = model.upper() == "LA380"
            if verified:
                emit("model", "LA380")
        band = channels and verified
        if band:
            tx, channel = channels
            protocol = "Brookstone FitPro"
            if interval == 1 and continuous and time.monotonic() >= cycle.pair_after:
                try:
                    await asyncio.wait_for(client.pair(), 18)
                    cycle.pair_after = float("inf")
                except (BleakError, OSError, TimeoutError) as exc:
                    cycle.pair_after = time.monotonic()+120
                    emit("notice", f"Windows pairing unavailable · {exc}")
        elif hr and {"notify", "indicate"}.intersection(hr.properties):
            channel = hr
            protocol = "Bluetooth HR"
        else:
            raise UnsupportedSensor(f"{name} has no supported heart-rate channel.")
        level = None
        if time.monotonic() >= cycle.battery_due or cycle.battery_interval != interval:
            battery = await read_optional(client, "2a19")
            level = battery[0] if len(battery) == 1 and battery[0] <= 100 else None
            cycle.battery_due = time.monotonic() + (300 if level is not None else 60)
            cycle.battery_interval = interval
        emit("identity", (name, protocol, level, interval, time.time()))
        decoder = FitProFrames()
        alert_ack = asyncio.Event()
        def notify(_, data):
            frames = decoder.feed(data) if band else (bytes(data),)
            for packet in frames:
                if band and packet == VIBRATION_ACK:
                    alert_ack.set()
                else:
                    packets.put_nowait(packet)
        await client.start_notify(channel, notify)
        persistent = continuous and (not band or interval == 1)
        if persistent:
            operation_timeout.reschedule(None)
        emit("link", (time.time(), "connected", protocol))
        end_reason = "Spot check finished"
        try:
            if band:
                return await fitpro_session(client, tx, packets, decoder, disconnected, interval,
                                            persistent, cycle, seen, emit, deliver, alert_ack)
            cycle.last_start = last_packet = time.monotonic()
            emit("state", "Listening for heart rate")
            while not disconnected.is_set() or not packets.empty():
                try:
                    data = await asyncio.wait_for(packets.get(), 0.5)
                except TimeoutError:
                    if time.monotonic()-last_packet > 22:
                        if not persistent:
                            raise BleakError("No new sample")
                        emit("state", "Connected · awaiting heart rate")
                        last_packet = time.monotonic()
                    continue
                bpm = standard_bpm(data)
                if bpm is not None:
                    last_packet = time.monotonic()
                    deliver(bpm, interval, None, not persistent)
                    if not persistent:
                        return bpm > 0
            raise BleakError("Bluetooth link closed")
        except asyncio.CancelledError:
            end_reason = "App stopped this session"
            raise
        except (BleakError, OSError, TimeoutError) as exc:
            end_reason = str(exc) or "Bluetooth operation timed out"
            raise
        finally:
            lost = end_reason != "App stopped this session" and (disconnected.is_set() or not client.is_connected)
            event = "link_lost" if lost else "session_ended"
            emit("link", (time.time(), event, end_reason))


async def fitpro_session(client, tx, packets, decoder, disconnected, interval, persistent,
                         cycle, seen, emit, deliver, alert_ack):
    """Keep one subscription. A new optical request starts only after the previous one ends."""
    async def write(data):
        await asyncio.wait_for(client.write_gatt_char(tx, data, response=True), 5)

    await write(OPEN_SESSION)
    while not packets.empty():
        packets.get_nowait()
    decoder.buffer.clear()
    if persistent:
        cycle.queue_alert = lambda title: packets.put_nowait(("alert",title))
    try:
        while client.is_connected:
            if persistent:
                # Rate limit starts even when a firmware result arrives in under a second.
                delay = cycle.last_start + 1 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            started = cycle.last_start = time.monotonic()
            emit("request", (time.time(), interval))
            await write(START)
            emit("state", "Connected · measuring" if persistent else "Measuring")
            acknowledged = False
            pending = None
            while not disconnected.is_set() or not packets.empty():
                if time.monotonic()-started > 22:
                    if not persistent:
                        raise BleakError("No new sample")
                    emit("link", (time.time(), "sample_timeout", "Retry on existing link"))
                    await write(STOP)
                    break
                try:
                    data = await asyncio.wait_for(packets.get(), 0.4)
                except TimeoutError:
                    continue
                if isinstance(data,tuple):
                    try:
                        await vibrate_connected(client,tx,alert_ack)
                        emit("notice",data[1]+" · Band acknowledged the vibration command.")
                    except (BleakError,OSError,TimeoutError):
                        emit("notice",data[1]+" · Band did not acknowledge the vibration command.")
                    continue
                reply = None
                if data == START_ACK:
                    acknowledged = True
                else:
                    record = fitpro_record(data)
                    if record is None:
                        if (persistent and data[0] == 0xcd and client.is_connected and
                                len(data)>=8 and int.from_bytes(data[6:8],"big")==len(data)-8):
                            await write(received_ack(data))
                        continue
                    reply = received_ack(data)
                    if record[0] not in seen:
                        pending = (*record, data)
                complete = acknowledged and pending is not None
                if complete:
                    stamp, bpm, frame = pending
                    seen.append(stamp)
                    deliver(bpm, interval, time.monotonic()-started, True, frame)
                if reply and client.is_connected:
                    with contextlib.suppress(BleakError, OSError, TimeoutError):
                        await write(reply)
                if complete:
                    if not persistent:
                        return bpm > 0
                    break
            if disconnected.is_set():
                break
            if persistent and time.monotonic() >= cycle.battery_due and client.is_connected:
                battery = await read_optional(client, "2a19")
                if len(battery)==1 and battery[0]<=100:
                    emit("identity", ("Brookstone LA380","Brookstone FitPro",battery[0],interval,time.time()))
                cycle.battery_due = time.monotonic()+300
        raise BleakError("Bluetooth link closed")
    finally:
        cycle.queue_alert=None
        if client.is_connected:
            with contextlib.suppress(BleakError, OSError, TimeoutError):
                await write(STOP)


async def stream(address, config, emit, stop, *, profile=DEFAULT_PROFILE, idle=DEFAULT_IDLE,
                 aliases=True, verified=False, cycle=None, confirm_jumps=True):
    cycle = cycle or DutyCycle()
    publisher = Publisher(config, aliases)
    publisher.repeat_seconds = 1 if profile == DEFAULT_PROFILE else 4
    loopback = local_destination(config.host)
    device = None
    task = None
    expires = 0
    next_check = 0
    listening = True
    task_live = False
    task_interval = None
    last_state = None
    confirmed = verified
    next_notice = 0
    jump_check = JumpCheck()

    def network_notice(message):
        nonlocal next_notice
        if time.monotonic() >= next_notice:
            emit("notice", message)
            next_notice = time.monotonic() + 30

    def send_osc(action, *args):
        try:
            action(*args)
        except OscError as exc:
            network_notice(str(exc))

    def state(message):
        nonlocal last_state
        if last_state != message:
            emit("state", message)
            last_state = message

    def event(kind, value):
        nonlocal confirmed, expires
        if kind == "model" and value == "LA380":
            confirmed = True
        if kind == "link" and value[1] == "link_lost" and task_interval == 1:
            send_osc(publisher.stale)
            emit("signal", False)
            expires = 0
        if kind == "state":
            state(value)
        else:
            emit(kind, value)

    def deliver(bpm, interval, acquisition, spot, frame=None):
        nonlocal expires, jump_check
        if bpm:
            cycle.success()
            reading = (time.time(), bpm, interval if spot else 1, acquisition)
            expires = time.monotonic() + (min(interval + 30, 90) if spot else 12)
            if frame is not None:
                allowed = not confirm_jumps or jump_check.allow(bpm,time.monotonic(),interval)
                reading = (*reading, frame, "allowed" if allowed else "held")
                if not allowed:
                    emit("reading", reading)
                    send_osc(publisher.stale)
                    return
            emit("reading", reading)
            send_osc(publisher.send, bpm)
            state("Streaming" if listening else "Recording")
        else:
            jump_check = JumpCheck()
            send_osc(publisher.stale)
            emit("signal", False)
            expires = 0
            state("Waiting for sensor contact")

    async def attempt(interval, live):
        nonlocal device
        if device is None:
            state("Looking for your sensor")
            device = await paired_device(address) or await BleakScanner.find_device_by_address(
                address, timeout=30 if interval==1 else 12)
        if device is None:
            raise BleakError("No band advertisement received")
        state("Connecting")
        return await measure(device, confirmed, interval, live, cycle, cycle.seen, event, deliver)

    try:
        while not stop.is_set():
            now = time.monotonic()
            if loopback and now >= next_check:
                try:
                    listening = await asyncio.to_thread(receiver_open, loopback, config.port)
                except (OSError, psutil.Error) as exc:
                    network_notice(f"OSC listener check unavailable · {exc}")
                next_check = now + 3
            interval = effective_interval(INTERVALS[profile], listening, idle)
            live = listening or idle == IDLE_MODES[2]
            if now < cycle.check_until:
                interval, live = min(interval or 15,15), True
            leave_session = interval is None or (task_live and not live) or (task_interval == 1 and interval != 1)
            if task and leave_session and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                task = None
            if task and task.done():
                try:
                    if await task:
                        cycle.success()
                    else:
                        cycle.missed(now, "No sensor contact", interval or 60)
                except (BleakError, OSError, TimeoutError) as exc:
                    cycle.missed(now, str(exc) or "Bluetooth timed out", interval or 60)
                    if task_interval == 1:
                        emit("link",(time.time(),"retry",cycle.reason))
                    if task_interval != 1 or cycle.misses >= 2:
                        device = None
                    state(str(exc) or "Bluetooth timed out")
                task = None
            if interval is None:
                send_osc(publisher.clear)
                state("Waiting for OSC")
            elif task is None:
                delay = cycle.due(interval) - now
                if delay <= 0:
                    task_live = live
                    task_interval = interval
                    task = asyncio.create_task(attempt(interval, live))
                else:
                    state(cycle.reason + " · retrying" if cycle.misses else ("Band resting" if live else "Background sampling"))
                    emit("next", time.time() + delay)
            if expires and now >= expires:
                send_osc(publisher.stale)
                emit("signal", False)
                expires = 0
            elif expires and listening:
                send_osc(publisher.repeat, now)
            await asyncio.sleep(0.1 if profile == DEFAULT_PROFILE else 0.5)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        send_osc(publisher.close)
