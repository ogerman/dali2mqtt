"""Tests for the single-reader sniffing hasseb driver."""

import struct
import threading
from unittest import mock

import dali.gear.general as gear
from dali.address import Short

from dali2mqtt.hasseb_sniffer import SniffingHassebDriver


def report(status, seq=0, data=b"", length=None):
    length = len(data) if length is None else length
    payload = data + bytes(3 - len(data))
    return struct.pack("BBBBB3sBB", 0xAA, 0x07, seq, status, length, payload, 0, 0)


def make_driver(on_frame=None):
    with mock.patch("dali.driver.hasseb.hid") as hid:
        driver = SniffingHassebDriver(on_frame=on_frame)
    return driver, hid.device.return_value


def test_unique_ids_keep_the_python_dali_driver_name():
    driver, _ = make_driver()
    assert driver.driver_name == "SyncHassebDALIUSBDriver"


def test_sniffed_frame_is_passed_on():
    on_frame = mock.Mock()
    driver, _ = make_driver(on_frame)

    driver._handle_report(report(5, data=b"\xfe\x05"))

    on_frame.assert_called_once_with(16, b"\xfe\x05")


def test_sniffed_length_in_bits_is_accepted():
    on_frame = mock.Mock()
    driver, _ = make_driver(on_frame)

    driver._handle_report(report(5, data=b"\xfe\x05", length=16))

    on_frame.assert_called_once_with(16, b"\xfe\x05")


def test_sniffer_errors_and_idle_reports_are_dropped():
    on_frame = mock.Mock()
    driver, _ = make_driver(on_frame)

    driver._handle_report(report(6, data=b"\xfe\x05"))
    driver._handle_report(report(0))

    on_frame.assert_not_called()


def test_answer_is_matched_by_sequence_number():
    driver, _ = make_driver()
    driver._pending = gear.QueryActualLevel(Short(1))
    driver.sn = 7

    driver._handle_report(report(2, seq=6, data=b"\x10", length=1))
    assert driver._pending is not None

    driver._handle_report(report(2, seq=7, data=b"\x80", length=1))
    assert driver._pending is None
    assert driver._response_ready.is_set()
    assert driver.extract(driver._response_message).as_integer == 0x80


def test_send_waits_for_reader_thread():
    driver, device = make_driver()
    answered = threading.Event()

    def deliver(data):
        # The device answers our query (sequence number from the frame) in the
        # background, like the reader thread would.
        def later():
            driver._handle_report(report(2, seq=data[2], data=b"\x64", length=1))
            answered.set()
        threading.Timer(0.05, later).start()

    device.write.side_effect = deliver
    driver._reader = mock.Mock(is_alive=mock.Mock(return_value=True))

    response = driver.send(gear.QueryActualLevel(Short(1)))

    assert answered.wait(1)
    assert response.value == 100
    device.read.assert_not_called()


def test_send_without_answer_times_out():
    driver, device = make_driver()
    driver._reader = mock.Mock(is_alive=mock.Mock(return_value=True))
    with mock.patch("dali2mqtt.hasseb_sniffer.RESPONSE_TIMEOUT", 0.05):
        response = driver.send(gear.QueryActualLevel(Short(1)))
    assert response.raw_value is None  # python-dali reports a missing answer
    assert driver._pending is None


def written_modes(device):
    return [bytes(c.args[0])[3] for c in device.write.call_args_list if bytes(c.args[0])[1] == 0x05]


def test_start_without_sniffing_turns_a_leftover_sniff_mode_off():
    driver, device = make_driver()
    device.read.return_value = []

    driver.start(sniff=False)
    driver.stop()

    assert written_modes(device) == [0, 0]


def test_queries_pause_sniffing_until_the_bus_is_idle():
    """Firmware 2.3 garbles odd answers in sniff mode."""
    driver, device = make_driver()
    driver._sniff_wanted = driver._sniff_active = True
    driver._reader = mock.Mock(is_alive=mock.Mock(return_value=True))

    def answer(data):
        data = bytes(data)
        if data[1] == 0x07 and data[4]:
            threading.Timer(
                0.01, driver._handle_report, [report(2, seq=data[2], data=b"\x9d", length=1)]
            ).start()

    device.write.side_effect = answer
    assert driver.send(gear.QueryMinLevel(Short(0))).value == 157
    assert driver.send(gear.QueryMaxLevel(Short(0))).value == 157
    assert written_modes(device) == [0]  # paused once for the burst

    driver._maybe_resume_sniffing()
    assert written_modes(device) == [0]  # too soon
    driver._last_query -= 1
    driver._maybe_resume_sniffing()
    assert written_modes(device) == [0, 1]
    assert driver._sniff_active


def test_commands_do_not_pause_sniffing():
    driver, device = make_driver()
    driver._sniff_wanted = driver._sniff_active = True

    driver.send(gear.DAPC(Short(0), 100))

    assert written_modes(device) == []


def test_start_enables_sniffing_and_stop_disables_it():
    driver, device = make_driver()
    device.read.return_value = []

    driver.start(sniff=True)
    assert driver.reading
    driver.stop()
    assert not driver.reading

    modes = [call.args[0][3] for call in device.write.call_args_list]
    assert modes == [1, 0]
