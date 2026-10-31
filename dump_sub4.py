import sys, io, json
sys.path.insert(0,'.')
from bili_scribe.config import AppConfig
from bili_scribe.infra.store import Store
from bili_scribe.core.collector import collect
from bili_scribe.core.bili_client import BiliClient if False else None
