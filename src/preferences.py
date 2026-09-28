"""Local preferences. New installations have no preset channels."""
import json
from pathlib import Path


def load_pinned_channels(data_dir):
    try:
        content = json.loads((Path(data_dir) / 'preferences.json').read_text(encoding='utf-8'))
        values = content.get('pinned_channels', [])
        if not isinstance(values, list):
            return set()
        return {value for value in values if type(value) is int and value > 0}
    except (OSError, ValueError, AttributeError):
        return set()


def save_pinned_channels(data_dir, channels):
    values = sorted({value for value in channels if type(value) is int and value > 0})
    path = Path(data_dir) / 'preferences.json'
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps({'pinned_channels': values}), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
