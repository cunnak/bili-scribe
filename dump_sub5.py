import sys, io, json
sys.path.insert(0,'.')
from bili_scribe.config import AppConfig
from bili_scribe.infra.store import Store
from bili_scribe.infra.bili_client import BiliClient
from bili_scribe.core.collector import collect

cfg = AppConfig.load('config.yaml')
store = Store(cfg.db_path, cfg.key_path)
client = BiliClient(store, logger=lambda m: None)
b = collect(client, 'BV1eAHG6LE4Y', 1, cfg.collect)
info = {
 'title': b.title, 'up': b.up_name, 'duration': b.duration, 'tname': b.tname,
 'source_kind': b.source_kind, 'source_label': b.source_label,
 'source_trust': b.source_trust, 'source_detail': b.source_detail,
 'body_chars': len(b.body), 'body_lines': b.body.count('\n')+1,
 'warnings': b.warnings, 'tags': b.tags[:8], 'desc_len': len(b.desc),
 'platform_summary_len': len(b.platform_summary),
}
io.open('sub_info.json','w',encoding='utf-8').write(json.dumps(info,ensure_ascii=False,indent=1))
io.open('sub_body.txt','w',encoding='utf-8').write(b.body)
print('dur', b.duration, 'chars', len(b.body))
