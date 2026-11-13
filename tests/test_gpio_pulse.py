import os
import sys
import threading
import types
import unittest
from unittest import mock

from gpio_pulse import GpioPulseOutput


class GpioPulseOutputTest(unittest.TestCase):
    def test_trigger_outputs_one_active_high_pulse(self):
        transitions = []
        went_high = threading.Event()
        went_low_after_high = threading.Event()

        fake_gpio = types.ModuleType('Jetson.GPIO')
        fake_gpio.BOARD = 10
        fake_gpio.OUT = 20
        fake_gpio.setwarnings = mock.Mock()
        fake_gpio.setmode = mock.Mock()
        fake_gpio.setup = mock.Mock()
        fake_gpio.cleanup = mock.Mock()

        def output(_channel, value):
            transitions.append(value)
            if value == 1:
                went_high.set()
            elif went_high.is_set():
                went_low_after_high.set()

        fake_gpio.output = output
        fake_jetson = types.ModuleType('Jetson')
        fake_jetson.GPIO = fake_gpio

        env = {
            'DS_CAMERA_GPIO_PULSE_ENABLED': '1',
            'DS_CAMERA_GPIO_BOARD_PIN': '29',
            'DS_CAMERA_GPIO_PULSE_MS': '10',
            'DS_CAMERA_GPIO_ACTIVE_HIGH': '1',
        }
        with mock.patch.dict(os.environ, env), mock.patch.dict(
            sys.modules,
            {'Jetson': fake_jetson, 'Jetson.GPIO': fake_gpio},
        ):
            pulse = GpioPulseOutput()
            pulse.start()
            pulse.trigger()
            self.assertTrue(went_high.wait(1.0))
            self.assertTrue(went_low_after_high.wait(1.0))
            pulse.stop()

        fake_gpio.setup.assert_called_once_with(29, 20, initial=0)
        self.assertIn([1, 0], [transitions[i:i + 2] for i in range(len(transitions) - 1)])
        fake_gpio.cleanup.assert_called_once_with(29)

    def test_repeated_triggers_are_coalesced_and_return_low(self):
        transitions = []
        went_low_after_high = threading.Event()

        fake_gpio = types.ModuleType('Jetson.GPIO')
        fake_gpio.BOARD = 10
        fake_gpio.OUT = 20
        fake_gpio.setwarnings = mock.Mock()
        fake_gpio.setmode = mock.Mock()
        fake_gpio.setup = mock.Mock()
        fake_gpio.cleanup = mock.Mock()

        def output(_channel, value):
            transitions.append(value)
            if value == 0 and 1 in transitions:
                went_low_after_high.set()

        fake_gpio.output = output
        fake_jetson = types.ModuleType('Jetson')
        fake_jetson.GPIO = fake_gpio

        env = {
            'DS_CAMERA_GPIO_PULSE_ENABLED': '1',
            'DS_CAMERA_GPIO_BOARD_PIN': '29',
            'DS_CAMERA_GPIO_PULSE_MS': '50',
            'DS_CAMERA_GPIO_ACTIVE_HIGH': '1',
        }
        with mock.patch.dict(os.environ, env), mock.patch.dict(
            sys.modules,
            {'Jetson': fake_jetson, 'Jetson.GPIO': fake_gpio},
        ):
            pulse = GpioPulseOutput()
            for _ in range(20):
                pulse.trigger()
            self.assertTrue(went_low_after_high.wait(1.0))
            pulse.stop()

        self.assertEqual(transitions.count(1), 1)
        self.assertGreaterEqual(transitions.count(0), 1)


if __name__ == '__main__':
    unittest.main()
