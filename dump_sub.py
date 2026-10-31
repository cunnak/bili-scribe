import urllib.request, json, io
# 直接调 collector 拿字幕，统计字符数与段数（不消耗模型）
import sys
sys.path.insert(0,'.')
from bili_scribe.core import collector as col
import inspect
sig = inspect.signature(col.collect)
print('collect sig:', sig)
print([n for n in dir(col) if not n.startswith('_')][:30])
