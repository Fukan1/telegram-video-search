"""Run offline tests using only temporary data and mock Telegram clients."""
import os
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
