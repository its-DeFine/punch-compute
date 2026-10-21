"""Actual handler + ModelOrders + private file state; identity/grant seams synthetic.
No HTTP response mocks, network, funds, router calls or model load required.
Old implementation creates a conflicting prepared order; repaired code must refuse
before writing order state. Both run from the same coherent retained snapshot.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

APP=Path(__file__).resolve().parents[1]/'app'
sys.path.insert(0,str(APP))

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    obj=importlib.util.module_from_spec(spec);sys.modules[name]=obj
    spec.loader.exec_module(obj);return obj

class AdmissionCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        env={'GW_DATA_DIR':str(root/'data')}
        for key,name in [('GW_SELLER_TOKEN_FILE','seller'),('GW_ADMIN_USER_FILE','admin'),('GW_ADMIN_PASS_FILE','pass')]:
            p=root/name;p.write_text('synthetic');p.chmod(0o600);env[key]=str(p)
        with patch.dict(os.environ,env):
            self.g=module('admission_gateway',os.environ.get('ADMISSION_GATEWAY',str(APP/'order-gateway.py')))
        mo=module('admission_models',os.environ.get('ADMISSION_MODELS',str(APP/'model_orders.py')))
        self.g.MODELS=mo.ModelOrders(self.g,str(root/'models'),'http://unused.invalid',0)
        self.calls=[]
        class Router:
            def models(inner):return {'model-a':{'status':{'value':'loaded'}},'model-b':{'status':{'value':'unloaded'}}}
            def load(inner,model):self.calls.append(('load',model))
            def unload(inner,model):self.calls.append(('unload',model))
        self.g.MODELS.router=Router()
        self.mo=mo
        self.g.buyer_identity=lambda token:42
        self.h=self.g.Handler.__new__(self.g.Handler);self.h.headers={}
        self.offer={'model':'model-a','allowance_credits':100,'window_seconds':3600,'withdrawn':False,
            'input_rate':1.0,'output_rate':5.0,'cache_rate':1.0,'native_runner_id':'runner-1',
            'native_recipient_address':'0x'+'cc'*20,'price_wei':2500}
        self.g.save(self.g.OFFERS_FILE,{'offer':self.offer})
        self.existing=dict(self.offer,state='READY',active_model='model-b',expires_at=time.time()+3600,buyer_user_id=77)
        self.g.save(self.g.ORDERS_FILE,{'existing':self.existing})
        self.body={'buyer_token':'synthetic','offer_id':'offer','idempotency_key':'new','payer_address':'0x'+'aa'*20}
    def prepare(self):
        self.h.path='/purchases/prepare'
        with self.g.STATE_LOCK:return self.h.dispatch('POST',self.body)
    def test_conflicting_preparation_refused_before_state_write(self):
        before=self.g.load(self.g.ORDERS_FILE)
        with self.assertRaises(self.g.Failure) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'MODEL_CAPACITY_BUSY')
        self.assertEqual(self.g.load(self.g.ORDERS_FILE),before)
    def test_conflicting_free_acquisition_refused_before_grant(self):
        self.g.CHECKOUT_LIVE=False
        self.g.complete_order=lambda *a: self.fail('grant must not be reached')
        self.h.path='/acquire'
        with self.assertRaises(self.g.Failure) as caught:
            with self.g.STATE_LOCK:self.h.dispatch('POST',self.body)
        self.assertEqual(caught.exception.code,'MODEL_CAPACITY_BUSY')
        self.assertEqual(list(self.g.load(self.g.ORDERS_FILE)),['existing'])
    def test_same_model_and_expired_history_allow_preparation(self):
        self.existing['active_model']='model-a'
        self.g.save(self.g.ORDERS_FILE,{'existing':self.existing})
        prepared=self.prepare();self.assertEqual(prepared['state'],'PREPARED')
        self.existing['active_model']='model-b';self.existing['expires_at']=time.time()-1
        self.g.save(self.g.ORDERS_FILE,{'existing':self.existing})
        self.assertEqual(self.prepare()['state'],'PREPARED')
    def test_expired_different_model_drains_before_loading_default(self):
        self.existing.update(expires_at=time.time()-1,token_id=9,strict_text_policy={'test':True})
        self.g.save(self.g.ORDERS_FILE,{'existing':self.existing})
        self.g.MODELS.router.models=lambda:{'model-a':{'status':{'value':'unloaded'}},'model-b':{'status':{'value':'loaded'}}}
        self.g.owned_session=lambda order:'synthetic-session'
        self.mo.disable_token=lambda api,sess,tid:self.calls.append(('drain',tid))
        self.prepare()
        self.assertEqual(self.calls,[('drain',9),('unload','model-b'),('load','model-a')])
    def test_drain_failure_does_not_load_or_create_order(self):
        self.existing.update(expires_at=time.time()-1,token_id=9,strict_text_policy={'test':True})
        self.g.save(self.g.ORDERS_FILE,{'existing':self.existing})
        self.g.MODELS.router.models=lambda:{'model-a':{'status':{'value':'unloaded'}},'model-b':{'status':{'value':'loaded'}}}
        self.g.owned_session=lambda order:'synthetic-session'
        def fail(*args):raise self.g.Failure('DRAIN_PENDING',409)
        self.mo.disable_token=fail
        with self.assertRaises(self.g.Failure):self.prepare()
        self.assertEqual(self.calls,[])
        self.assertEqual(list(self.g.load(self.g.ORDERS_FILE)),['existing'])
    def test_prepared_purchase_blocks_another_orders_model_switch(self):
        order=dict(self.existing,model='model-a',active_model='model-a',model_coverage={'test':True},strict_text_policy={'test':True})
        pending=dict(self.offer,state='PREPARED',expires_at=time.time()+3600)
        self.g.save(self.g.ORDERS_FILE,{'existing':order,'pending':pending})
        with self.assertRaises(self.g.Failure) as caught:
            self.g.MODELS.request('existing',order,dict(registry='huggingface',repository='owner/model',revision='a'*40,file='model.gguf',idempotency_key='switch'))
        self.assertEqual(caught.exception.code,'MODEL_CAPACITY_BUSY')
        self.assertEqual(self.g.load(self.g.MODELS.file),{})

    def test_status_uses_native_settlement_when_consume_log_is_missing(self):
        self.existing.update(offer_id='offer', token_id=9)
        self.g.owned_session=lambda order:'synthetic-session'
        summary={'reserved_count':0, 'dispatched_count':0, 'held_quota':0,
                 'settled_count':1, 'settled_quota':14, 'input_tokens':9,
                 'output_tokens':1, 'unrecorded_settled_count':0}
        key={'remain_quota':86,'used_quota':14,'status':1,'strict_reservations':summary}
        calls=[]
        def api(method, path, token=None):
            calls.append(path)
            if path == '/api/token/9': return key
            if path == '/api/user/self': return {'quota':86}
            if path.startswith('/api/log/self'): return {'items':[], 'total':0}
            self.fail('unexpected native API route')
        self.g.api=api
        result=self.g.status('existing',self.existing)
        self.assertEqual((result['input_tokens'],result['output_tokens']),(9,1))
        self.assertTrue(result['usage_complete'])
        self.assertEqual(result['usage_source'],'native_settlement')
        self.assertFalse(any(path.startswith('/api/log/self') for path in calls))
        key['used_quota']=34
        result=self.g.status('existing',self.existing)
        self.assertFalse(result['usage_complete'])
        self.assertEqual(result['untracked_usage_quota'],20)
        key['used_quota']=14;summary['unrecorded_settled_count']=1
        self.assertFalse(self.g.status('existing',self.existing)['usage_complete'])

    def test_maintenance_routes_auth_persist_and_gate_mutations(self):
        self.g.save(self.g.ORDERS_FILE,{})
        self.g.MAINTENANCE=self.maintenance()
        self.h.path='/maintenance/acquire'
        with self.assertRaises(self.g.Failure) as caught:
            self.h.dispatch('POST',{'seller_token':'wrong','operation_id':'update-1'})
        self.assertEqual(caught.exception.code,'SELLER_AUTH_INVALID')
        held=self.h.dispatch('POST',{'seller_token':'synthetic','operation_id':'update-1','owner':'intruder'})
        self.assertEqual(held['operation']['state'],'HELD')
        self.assertEqual(held['operation']['owner'],'seller')
        self.g.MAINTENANCE=self.maintenance() # process reconstruction, same files
        before=self.g.load(self.g.ORDERS_FILE)
        for route in ('/purchases/prepare','/purchases/activate','/acquire','/offers'):
            self.h.path=route
            with self.subTest(route=route), self.assertRaises(self.g.Failure) as caught:
                self.h.dispatch('POST',self.body)
            self.assertEqual(caught.exception.code,'MAINTENANCE_ACTIVE')
        self.assertEqual(self.g.load(self.g.ORDERS_FILE),before)
        self.existing['buyer_user_id']=42
        self.g.save(self.g.ORDERS_FILE,{'existing':self.existing})
        self.h.path='/orders/existing/model'
        with self.assertRaises(self.g.Failure) as caught:self.h.dispatch('POST',{})
        self.assertEqual(caught.exception.code,'MAINTENANCE_ACTIVE')
        self.h.path='/health'
        self.assertTrue(self.h.dispatch('GET',{})['ready'])
        self.h.path='/maintenance/status'
        self.assertEqual(self.h.dispatch('POST',{'seller_token':'synthetic'})['admission'],'closed')
        self.h.path='/maintenance/release'
        with self.assertRaises(self.g.Failure) as caught:
            self.h.dispatch('POST',{'seller_token':'synthetic','operation_id':'wrong'})
        self.assertEqual(caught.exception.code,'NO_OPERATION')
        self.h.dispatch('POST',{'seller_token':'synthetic','operation_id':'update-1'})
        self.assertTrue(self.g.MAINTENANCE.admission_open())
        self.g.save(self.g.ORDERS_FILE,{})
        self.assertEqual(self.prepare()['state'],'PREPARED')

    def maintenance(self):
        from maintenance import Maintenance
        return Maintenance(self.g)
    def test_maintenance_fresh_uses_actual_gateway_empty_file_contract(self):
        self.g.save(self.g.ORDERS_FILE,{})
        gate=self.maintenance()
        self.assertTrue(gate.admission_open())
        self.assertEqual(gate.acquire('update-1','seller')['operation']['state'],'HELD')
        self.assertFalse(self.maintenance().admission_open())
    def test_maintenance_exhausted_unexpired_order_can_drain(self):
        self.g.save(str(Path(self.g.DATA)/'maintenance.json'),{'admission':'open','operation':None})
        policy={'input_rate':1,'output_rate':5,'cache_rate':1,'context_tokens':64,'max_output_tokens':8}
        self.existing.update(token_id=9,strict_text_policy=policy)
        self.g.save(self.g.ORDERS_FILE,{'existing':self.existing})
        key={'id':9,'status':2,'remain_quota':0,'used_quota':100,'unlimited_quota':False,
             'strict_text_policy':policy,'strict_reservations':{'reserved_count':0,'dispatched_count':0,'held_quota':0}}
        calls=[]
        self.g.owned_session=lambda order:'synthetic-session'
        def api(method,path,body=None,token=None):
            calls.append(method)
            if method=='PUT':key['status']=body['status'];return {}
            return key
        self.g.api=api
        result=self.maintenance().acquire('update-1','seller')
        self.assertEqual(result['operation']['state'],'HELD',result)
        self.assertIn('PUT',calls,'already-disabled token still needs authoritative drain')
        self.assertEqual((key['remain_quota'],key['used_quota']),(0,100))
    def test_maintenance_missing_summary_fields_refuse_upgrade(self):
        self.g.save(str(Path(self.g.DATA)/'maintenance.json'),{'admission':'open','operation':None})
        self.existing.update(token_id=9,strict_text_policy={'bound':True},expires_at=time.time()-1)
        self.g.save(self.g.ORDERS_FILE,{'existing':self.existing})
        self.g.owned_session=lambda order:'synthetic-session'
        self.g.api=lambda *a,**kw:{'id':9,'status':2,'remain_quota':0,'strict_text_policy':{'bound':True},'strict_reservations':{}}
        result=self.maintenance().acquire('update-1','seller')
        self.assertEqual(result['operation']['state'],'REFUSED',result)
        self.assertIn('NATIVE_SUMMARY_UNAVAILABLE',[b['code'] for b in result['blockers']])

if __name__=='__main__':unittest.main(verbosity=2)
