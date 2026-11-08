"""Tests for the serialized bus wrapper."""

from unittest import mock

import dali.gear.general as gear
from dali.address import Group, Short

from dali2mqtt.bus import Bus


class FakeDriver:
    def __init__(self):
        self.sent = []

    def send(self, command):
        self.sent.append(command)
        return "response"


def test_send_goes_to_driver_and_notifies_listeners():
    driver = FakeDriver()
    bus = Bus(driver)
    listener = mock.Mock()
    bus.add_listener(listener)

    cmd = gear.DAPC(Short(1), 100)
    assert bus.send(cmd) == "response"

    assert driver.sent == [cmd]
    listener.assert_called_once_with(cmd, True)


def test_queries_are_not_reported_as_traffic():
    bus = Bus(FakeDriver())
    listener = mock.Mock()
    bus.add_listener(listener)

    bus.send(gear.QueryActualLevel(Short(1)))

    listener.assert_not_called()


def test_sniffed_frame_is_decoded_and_reported_as_foreign():
    bus = Bus(FakeDriver())
    listener = mock.Mock()
    bus.add_listener(listener)

    bus.on_sniffed_frame(16, gear.DAPC(Group(2), 120).frame.pack)

    (cmd, own), _ = listener.call_args
    assert type(cmd).__name__ == "DAPC"
    assert cmd.destination == Group(2)
    assert cmd.power == 120
    assert own is False


def test_sniffed_queries_and_other_frame_sizes_are_ignored():
    bus = Bus(FakeDriver())
    listener = mock.Mock()
    bus.add_listener(listener)

    bus.on_sniffed_frame(16, gear.QueryActualLevel(Short(1)).frame.pack)
    bus.on_sniffed_frame(8, b"\x55")
    bus.on_sniffed_frame(24, b"\x01\x02\x03")

    listener.assert_not_called()


def test_echo_of_own_frame_is_dropped():
    bus = Bus(FakeDriver())
    listener = mock.Mock()
    bus.add_listener(listener)

    cmd = gear.Off(Short(3))
    bus.send(cmd)
    bus.on_sniffed_frame(16, cmd.frame.pack)

    listener.assert_called_once_with(cmd, True)


def test_failing_listener_does_not_break_others():
    bus = Bus(FakeDriver())
    bus.add_listener(mock.Mock(side_effect=RuntimeError("boom")))
    second = mock.Mock()
    bus.add_listener(second)

    bus.send(gear.Off(Short(3)))

    second.assert_called_once()


def test_driver_name_is_the_wrapped_class():
    assert Bus(FakeDriver()).driver_name == "FakeDriver"


def test_driver_may_pin_the_name_home_assistant_knows():
    class Pinned(FakeDriver):
        driver_name = "SyncHassebDALIUSBDriver"

    assert Bus(Pinned()).driver_name == "SyncHassebDALIUSBDriver"


def test_run_sequence_sends_each_command_and_returns_the_result():
    driver = FakeDriver()
    bus = Bus(driver)
    listener = mock.Mock()
    bus.add_listener(listener)

    def sequence():
        first = yield gear.DTR0(5)
        second = yield gear.QueryActualLevel(Short(1))
        return first, second

    assert bus.run_sequence(sequence()) == ("response", "response")
    assert [type(c).__name__ for c in driver.sent] == ["DTR0", "QueryActualLevel"]
    listener.assert_called_once()  # DTR0 only, the query is not traffic


def test_extended_commands_are_preceded_by_enable_device_type():
    from dali.gear.colour import Activate

    driver = FakeDriver()
    bus = Bus(driver)
    listener = mock.Mock()
    bus.add_listener(listener)

    bus.send(Activate(Short(1)))

    assert [type(c).__name__ for c in driver.sent] == ["EnableDeviceType", "Activate"]
    assert driver.sent[0].param == 8
    listener.assert_called_once()


def test_run_sequence_honours_sleep_and_progress():
    from dali.sequences import progress, sleep

    def sequence():
        yield sleep(0.1)
        yield progress(message="half way")
        yield gear.Off(Short(1))
        return "done"

    with mock.patch("dali2mqtt.bus.time.sleep") as slept:
        assert Bus(FakeDriver()).run_sequence(sequence()) == "done"
    slept.assert_called_once_with(0.1)


def test_other_senders_wait_for_a_running_sequence():
    import threading
    import time

    driver = FakeDriver()
    bus = Bus(driver)
    started = threading.Event()
    release = threading.Event()

    def sequence():
        yield gear.DTR0(1)
        started.set()
        release.wait(1)
        yield gear.DTR1(2)

    runner = threading.Thread(target=bus.run_sequence, args=(sequence(),))
    runner.start()
    assert started.wait(1)
    other = threading.Thread(target=bus.send, args=(gear.Off(Short(1)),))
    other.start()
    time.sleep(0.05)
    release.set()
    runner.join(1)
    other.join(1)

    assert [type(c).__name__ for c in driver.sent] == ["DTR0", "DTR1", "Off"]
