"""Exercise production counting with synthetic tracks; no GPU/services needed."""
import os
import runpy
import unittest
from datetime import datetime
from unittest import mock

import numpy as np


class RoiPassageTest(unittest.TestCase):
    def setUp(self):
        self.rolling = mock.Mock()
        self.trend = mock.Mock()
        self.modbus = mock.Mock()
        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              'ds', 'parts', '40_speed_counting.py')
        self.runtime = runpy.run_path(source, init_globals={
            'os': os, 'np': np, 'datetime': datetime,
            'CLASS_NAMES': {0: 'car', 1: 'person'},
            '_norm_class_name': lambda value: value,
            '_display_class_name': lambda value: value,
            '_record_rolling_event': self.rolling,
            '_record_vehicle_trend_event': self.trend,
            '_record_modbus_roi_event': self.modbus,
            '_update_vehicle_speed_trends': mock.Mock(),
            '_acc_empty': lambda: {}, '_acc_update': mock.Mock(),
        })
        self.state = {'track_state': {}, 'counts': {}}
        self.line1 = ((0, 100), (100, 100))
        self.line2 = ((100, 200), (0, 200))
        self.roi = [[0, 100], [100, 100], [100, 200], [0, 200]]
        self.frame = 0

    def feed(self, y, seconds, tid=100, cls=0, x=50, offset=0):
        self.runtime['_do_counting'](
            [(tid, x - 10, y - 20, x + 10, y, cls)],
            *self.line1, *self.line2, self.roi,
            state=self.state, frame_idx=self.frame, video_fps=30,
            ref_distance=10, frame_time_sec=seconds, elapsed_offset=offset,
            line1=self.line1, line2=self.line2)
        self.frame += 1

    def up(self):
        for y, t in ((210, 0), (190, 1), (110, 1.8), (90, 2)):
            self.feed(y, t)

    def down(self):
        for y, t in ((90, 9), (110, 10), (190, 11), (210, 12)):
            self.feed(y, t)

    def test_id_100_round_trip_counts_each_direction_and_speed(self):
        self.up()
        self.down()
        self.assertEqual(self.state['counts'][0], {'상행': 1, '하행': 1})
        events = self.state['det_log']
        self.assertEqual([event['tid'] for event in events], [100, 100])
        self.assertEqual([event['speed_kph'] for event in events], [36, 18])
        self.assertEqual(self.modbus.call_args_list,
                         [mock.call('car', '상행', 36), mock.call('car', '하행', 18)])
        self.assertEqual(self.rolling.call_count, 2)
        self.assertEqual(self.trend.call_count, 2)

    def test_third_passage_same_id_counts_again(self):
        self.up()
        self.down()
        self.feed(190, 20)
        self.feed(90, 21)
        self.assertEqual(self.state['counts'][0], {'상행': 2, '하행': 1})
        self.assertEqual(self.modbus.call_count, 3)

    def test_same_direction_can_repeat_after_returning_around_line_endpoints(self):
        self.up()
        self.feed(90, 3, x=200)
        self.feed(210, 4, x=200)
        self.feed(210, 5)
        self.feed(190, 6)
        self.feed(90, 7)
        self.assertEqual(self.state['counts'][0], {'상행': 2, '하행': 0})

    def test_slanted_lines_round_trip_counts_both_directions(self):
        self.line1 = ((326, 235), (715, 231))
        self.line2 = ((800, 274), (302, 281))
        for y, t in ((300, 0), (270, 1), (210, 2), (250, 10), (300, 12)):
            self.feed(y, t, x=500)
        self.assertEqual(self.state['counts'][0], {'상행': 1, '하행': 1})
        self.assertEqual([event['speed_kph'] for event in self.state['det_log']], [36, 18])

    def test_exit_and_stationary_frames_cannot_count_twice(self):
        self.up()
        for t in range(3, 15):
            self.feed(90, t)
        self.assertEqual(self.state['counts'][0], {'상행': 1, '하행': 0})
        self.assertEqual(self.modbus.call_count, 1)
        self.assertIsNone(self.state['track_state'][100]['line1_cross'])
        self.assertIsNone(self.state['track_state'][100]['line2_cross'])

    def test_turn_back_through_entry_aborts_and_reentry_uses_new_time(self):
        for y, t in ((210, 0), (190, 1), (210, 2)):
            self.feed(y, t)
        self.assertEqual(self.modbus.call_count, 0)
        self.assertIsNone(self.state['track_state'][100]['line2_cross'])
        self.feed(190, 6)
        self.feed(90, 7)
        self.assertEqual(self.state['det_log'][0]['speed_kph'], 36)

    def test_starting_inside_does_not_arm_an_exit_as_entry(self):
        self.feed(150, 0)
        self.feed(90, 1)
        self.assertEqual(self.modbus.call_count, 0)
        self.feed(110, 2)
        self.feed(210, 3)
        self.assertEqual(self.state['counts'][0], {'상행': 0, '하행': 1})

    def test_two_overlapping_tracks_are_independent(self):
        for y, t in ((210, 0), (190, 1), (90, 2)):
            for tid in (100, 101):
                self.feed(y, t, tid=tid)
        self.assertEqual(self.state['counts'][0], {'상행': 2, '하행': 0})
        self.assertEqual(self.modbus.call_count, 2)

    def test_invalid_speed_does_not_block_later_valid_passage(self):
        for y, t in ((210, 0), (190, .01), (90, .02), (110, 1), (210, 2)):
            self.feed(y, t)
        self.assertEqual(self.state['counts'][0], {'상행': 0, '하행': 1})
        self.assertEqual(self.modbus.call_count, 1)

    def test_crossing_both_lines_in_one_frame_is_consumed_in_path_order(self):
        self.feed(210, 0)
        self.feed(90, 1)
        # Preserve the existing speed policy: equal crossing times are invalid.
        self.assertEqual(self.modbus.call_count, 0)
        self.feed(110, 2)
        self.feed(210, 3)
        self.assertEqual(self.state['counts'][0], {'상행': 0, '하행': 1})

    def test_line_endpoint_orientation_does_not_change_direction(self):
        self.line1 = tuple(reversed(self.line1))
        self.line2 = tuple(reversed(self.line2))
        self.up()
        self.down()
        self.assertEqual(self.state['counts'][0], {'상행': 1, '하행': 1})

    def test_swapping_line_labels_does_not_change_direction(self):
        self.line1, self.line2 = self.line2, self.line1
        self.up()
        self.down()
        self.assertEqual(self.state['counts'][0], {'상행': 1, '하행': 1})

    def test_changed_id_mid_passage_does_not_share_entry(self):
        self.feed(210, 0)
        self.feed(190, 1)
        self.feed(110, 1.8, tid=101)
        self.feed(90, 2, tid=101)
        self.assertEqual(self.modbus.call_count, 0)

    def test_new_entry_clears_old_speed_and_bev_samples(self):
        self.up()
        ts = self.state['track_state'][100]
        ts['speed_samples'] = [999]
        ts['bev_y_hist'] = [(999, 0)]
        self.feed(110, 10)
        self.assertIsNone(ts['last_speed_kph'])
        self.assertIsNone(ts['speed'])
        self.assertEqual(ts['speed_samples'], [])
        self.assertEqual(len(ts['bev_y_hist']), 1)

    def test_trend_timestamp_is_exit_time_in_both_directions(self):
        self.up()
        self.down()
        self.assertEqual([call.args[4] for call in self.trend.call_args_list], [2, 12])

    def test_person_round_trip_preserves_speed_independent_count_policy(self):
        for y, t in ((210, 0), (190, .01), (90, .02), (110, .03), (210, .04)):
            self.feed(y, t, cls=1)
        self.assertEqual(self.state['counts'][1], {'상행': 1, '하행': 1})
        self.assertEqual(self.modbus.call_count, 2)


if __name__ == '__main__':
    unittest.main()
