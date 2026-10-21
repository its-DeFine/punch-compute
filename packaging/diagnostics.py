#!/usr/bin/env python3
"""Read-only seller report. No credentials, logs, inference or payment calls.
Run through punch-stack to inherit the actual Compose profiles and environment.
HTTP readiness does not establish ledger health or financial acceptance.
"""
import datetime
import json
from pathlib import Path
import re
import subprocess

SERVICES = ('model', 'new-api', 'order-api', 'livepeer')
HASH = r'[0-9a-f]{64}'
IMAGE = re.compile(r'(?:[a-z0-9./_-]+@)?sha256:' + HASH)
MODEL = re.compile(r'punch-' + HASH)
STATES = {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'}
HEALTH = {'healthy', 'unhealthy', 'starting'}
PROBE = r'''
import json, sys, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise ValueError()
opener = urllib.request.build_opener(NoRedirect())
kind = sys.argv[1]
paths = {'new_api_http':'http://new-api:3000/api/status',
         'model_catalog':'http://model:8080/models',
         'wrapper_http':'http://127.0.0.1:18086/health',
         'native_http':'http://127.0.0.1:7935/status'}
try:
    with opener.open(paths[kind], timeout=2) as response:
        raw = response.read(262145)
        if len(raw)>262144 or response.status!=200: raise ValueError()
        data = json.loads(raw)
    out = {'state':'READY'}
    if kind=='new_api_http':
        if data.get('success') is not True or data.get('data',{}).get('punch_strict_text_policy')!=1: raise ValueError()
    elif kind=='wrapper_http':
        if data.get('success') is not True or data.get('data',{}).get('ready') is not True: raise ValueError()
    elif kind=='model_catalog':
        rows=data['data']
        if not isinstance(rows,list): raise ValueError()
        out['loaded_models']=[r['id'] for r in rows if r['status']['value']=='loaded']
        if not out['loaded_models']: out['state']='UNAVAILABLE'
    elif not isinstance(data,dict): raise ValueError()
except Exception:
    out={'state':'UNAVAILABLE'}
print(json.dumps(out))
'''


def command(argv, as_json=True):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=15)
    if result.returncode: raise ValueError('command unavailable')
    return json.loads(result.stdout) if as_json else result.stdout


def image_id(value):
    return value if isinstance(value,str) and IMAGE.fullmatch(value) else None


def safe_probe(value):
    result={'state':'UNAVAILABLE'}
    if isinstance(value,dict) and value.get('state') in ('READY','UNAVAILABLE'):
        result['state']=value['state']
        if 'loaded_models' in value:
            models=value['loaded_models']
            if not isinstance(models,list) or any(not isinstance(m,str) or not MODEL.fullmatch(m) for m in models):
                return {'state':'UNAVAILABLE'}
            result['loaded_models']=models
    return result


def build_report(root):
    report={'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'state':'UNAVAILABLE','services':{},'dependencies':{},
            'ledger_readiness':{'state':'NOT_TESTED','reason':'use-authenticated-order-status'},
            'payment_readiness':{'state':'NOT_TESTED','reason':'buyer-checkout-acceptance-pending'}}
    try:
        config=command(['docker','compose','config','--format','json'])['services']
        if not isinstance(config,dict): raise ValueError()
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        report['code']='COMPOSE_UNAVAILABLE'
        return report
    report['native_enabled']='livepeer' in config
    for name in SERVICES:
        if name not in config: continue
        entry={'configured_image':image_id(config[name].get('image')),'state':'UNAVAILABLE'}
        report['services'][name]=entry
        try:
            ids=command(['docker','compose','ps','--all','--quiet',name],False).split()
            if not ids:
                entry['state']='MISSING'
                continue
            if len(ids)!=1 or not re.fullmatch(HASH,ids[0]): continue
            # Never read environment or health log payloads from inspect.
            template='{"image":{{json .Image}},"status":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}'
            metadata=command(['docker','inspect','--format',template,ids[0]])
            entry['running_image']=image_id(metadata.get('image'))
            entry['container_state']=metadata.get('status') if metadata.get('status') in STATES else 'unknown'
            entry['health']=metadata.get('health') if metadata.get('health') in HEALTH else None
            entry['state']=('STOPPED' if entry['container_state'] in ('exited','created','dead') else
                            'READY' if entry['container_state']=='running' and entry['health']=='healthy' else 'UNAVAILABLE')
            # Registry manifest digest and Docker image ID are different identities.
            if entry['configured_image'] and entry['configured_image'].startswith('sha256:'):
                entry['image_matches']=entry['configured_image']==entry['running_image']
                if not entry['image_matches']: entry['state']='UNAVAILABLE'
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
            entry['state']='UNAVAILABLE'
    for kind in ('new_api_http','model_catalog','wrapper_http','native_http'):
        service='livepeer' if kind=='native_http' else 'order-api'
        entry=report['services'].get(service)
        if not entry:
            report['dependencies'][kind]={'state':'NOT_TESTED'}
        elif entry.get('container_state')!='running':
            report['dependencies'][kind]={'state':'NOT_TESTED','reason':'probe-container-not-running'}
        else:
            try:
                result=command(['docker','compose','exec','-T',service,'python3','-c',PROBE,kind])
                report['dependencies'][kind]=safe_probe(result)
            except (OSError,ValueError,subprocess.SubprocessError):
                report['dependencies'][kind]={'state':'UNAVAILABLE'}
    report['default_model']={'state':'NOT_TESTED'}
    try:
        manifest=json.loads((root/'models/default.json').read_text())
        if not MODEL.fullmatch(manifest['model']) or not re.fullmatch(HASH,manifest['identity']['sha256']): raise ValueError()
        report['default_model']={'state':'RECORDED','model':manifest['model'],'sha256':manifest['identity']['sha256']}
    except FileNotFoundError: pass
    except (OSError,ValueError,KeyError,TypeError): report['default_model']={'state':'UNAVAILABLE'}
    entries=list(report['services'].values())
    if entries and all(e['state']=='STOPPED' for e in entries): report['state']='STOPPED'
    elif all(report['services'].get(n,{}).get('state')=='READY' for n in ('model','new-api','order-api')) and all(
            d['state']=='READY' for k,d in report['dependencies'].items() if k!='native_http' or report['native_enabled']):
        report['state']='LOCAL_SERVICES_READY'
    return report


if __name__=='__main__':
    report=build_report(Path(__file__).resolve().parent)
    print(json.dumps(report))
    raise SystemExit(0 if report['state'] in ('LOCAL_SERVICES_READY','STOPPED') else 1)
