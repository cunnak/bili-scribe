import sys, io, json
sys.path.insert(0,'.')
from bili_scribe.core.collector import collect, CollectConfig
from bili_scribe.config import load_settings
s = load_settings()
print('settings ok', type(s).__name__)
print([a for a in dir(s) if not a.startswith('_')][:30])
