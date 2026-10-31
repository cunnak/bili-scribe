import sys, io, json
sys.path.insert(0,'.')
from bili_scribe.core.bili_client import BiliClient
from bili_scribe.core.collector import collect, CollectConfig
from bili_scribe.core import config as cfgmod

cfg = cfgmod.load_config() if hasattr(cfgmod,'load_config') else None
print('config loaded', cfg is not None)
names=[n for n in dir(cfgmod) if not n.startswith('_')]
print(names[:25])
