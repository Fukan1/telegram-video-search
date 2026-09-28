import json
import tempfile
import unittest
from pathlib import Path

from preferences import load_pinned_channels, save_pinned_channels


class PreferencesTests(unittest.TestCase):
    def test_no_preset_channels_and_pin_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_pinned_channels(tmp), set())
            save_pinned_channels(tmp, {123, 456})
            self.assertEqual(load_pinned_channels(tmp), {123, 456})
            save_pinned_channels(tmp, {456})
            self.assertEqual(load_pinned_channels(tmp), {456})
            save_pinned_channels(tmp, set())
            self.assertEqual(load_pinned_channels(tmp), set())
            self.assertFalse((Path(tmp) / 'preferences.tmp').exists())

    def test_malformed_or_invalid_preferences_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'preferences.json'
            for value in ('broken', 'null', '[]', '{"pinned_channels": "invalid"}'):
                path.write_text(value, encoding='utf-8')
                self.assertEqual(load_pinned_channels(tmp), set())
            path.write_text(json.dumps({'pinned_channels': [123, 123, -1, True, '456']}), encoding='utf-8')
            self.assertEqual(load_pinned_channels(tmp), {123})
