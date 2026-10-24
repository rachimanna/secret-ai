import asyncio
import base64
import hashlib
import hmac
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import httpx

import bot
import connector_check
import extensions as ext
import integrations
import mcpbridge
import miniapp
from apps import Pipedream, PipedreamError

MARKDOWN = '---\nname: review\ndescription: Review code changes\n---\nRead the diff and explain concrete bugs.'


def init_data(uid=1, token='fixture-token'):
    fields = {'user':json.dumps({'id':uid,'language_code':'ru'}),'auth_date':str(int(time.time()))}
    check = '\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
    secret = hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)


def zipped(files):
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,content in files.items():
            archive.writestr(name,content)
    return {'encoding':'base64','content':base64.b64encode(raw.getvalue()).decode()}


class ExtensionTest(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store=integrations.Connections(Path(self.folder.name)/'vault','fixture')
        self.addCleanup(self.store.db.close)

    def install(self,uid=1):
        return ext.dispatch(self.store,uid,'install',{'content':MARKDOWN,'confirm':True})

    def test_install_preview_is_read_only_and_confirmation_required(self):
        result=ext.dispatch(self.store,1,'preview',{'content':MARKDOWN})
        self.assertEqual(result['package']['skills'][0]['id'],'review')
        self.assertEqual(ext.installed(self.store,1),[])
        with self.assertRaises(ValueError):
            ext.dispatch(self.store,1,'install',{'content':MARKDOWN})
        self.install()
        self.assertEqual(ext.installed(self.store,2),[])
        with self.store.lock:
            raw=self.store.db.execute("SELECT secret FROM connections WHERE service='extension'").fetchone()[0]
        self.assertNotIn(b'Read the diff',raw)

    def test_enabled_state_export_reimport_and_user_boundaries(self):
        self.install()
        for action,data in [('export',{'id':'review'}),('toggle',{'id':'review','enabled':False}),('delete',{'id':'review','confirm':True})]:
            with self.assertRaises(ValueError):
                ext.dispatch(self.store,2,action,data)
        ext.dispatch(self.store,1,'toggle',{'id':'review','enabled':False})
        self.assertEqual(ext.skill_tool(self.store,1),[])
        exported=ext.dispatch(self.store,1,'export',{'id':'review'})
        with self.assertRaises(ValueError):
            ext.dispatch(self.store,1,'install',{'content':exported['content'],'confirm':True})
        ext.dispatch(self.store,1,'install',{'content':exported['content'],'confirm':True,'replace':True})
        self.assertTrue(ext.installed(self.store,1)[0]['enabled'])
        with self.assertRaises(ValueError):
            ext.dispatch(self.store,1,'delete',{'id':'review'})
        ext.dispatch(self.store,1,'delete',{'id':'review','confirm':True})
        self.assertEqual(ext.installed(self.store,1),[])

    def test_multiple_skills_zip_and_connector_suggestions(self):
        package=ext.parse(zipped({'skills/review/SKILL.md':MARKDOWN,'skills/editor/SKILL.md':MARKDOWN.replace('review','editor')}))
        self.assertEqual(len(package['skills']),2)
        package['connectors']=[{'label':'Docs','url':'https://example.com/mcp'}]
        ext.dispatch(self.store,1,'install',{'content':json.dumps(package),'confirm':True})
        self.assertEqual(self.store.get(1,'mcp'),[], 'Installing a package does not connect its accounts')

    def test_invalid_and_executable_packages_are_rejected(self):
        invalid=[{'content':'no frontmatter'}, {'content':'---\nname: x\n---\nbody'},
                 {'content':'---\nname: x\ndescription: !!python/object:os.system {}\n---\nbody'},
                 zipped({'../SKILL.md':MARKDOWN}), zipped({'SKILL.md':MARKDOWN,'run.py':'print(1)'}),
                 zipped({'SKILL.md':MARKDOWN,'large.txt':'x'*(ext.MAX_BYTES+1)}),
                 {'encoding':'base64','content':'!!!!'}, {'content':'x'*(ext.MAX_BYTES+1)}]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:60]),self.assertRaises(ValueError):
                ext.parse(payload)
        package=dict(ext.BUILTINS[0],hooks={'run':'echo no'})
        with self.assertRaises(ValueError):
            ext.validate(package)

    def test_corrupt_json_types_and_duplicate_skills_rejected(self):
        p=ext.parse({'content':MARKDOWN})
        for changed in [dict(p,skills={}),dict(p,skills=[p['skills'][0]]*2),dict(p,connectors=[{'label':'x','url':'https://example.com','token':'private'}])]:
            with self.assertRaises(ValueError):
                ext.validate(changed)

    def test_skill_reader_rechecks_disabled_state(self):
        self.install()
        tool=ext.skill_tool(self.store,1)[0]
        self.assertIn('review/review',tool['hint'])
        self.assertIn('Read the diff',asyncio.run(tool['run']({'id':'review/review'})))
        ext.dispatch(self.store,1,'toggle',{'id':'review','enabled':False})
        self.assertIn('disabled',asyncio.run(tool['run']({'id':'review/review'})))
        self.assertEqual(ext.skill_tool(self.store,2),[])

    def test_install_limit_and_replacement(self):
        p=ext.parse({'content':MARKDOWN})
        for index in range(ext.MAX_PACKAGES):
            ext.dispatch(self.store,1,'install',{'content':json.dumps(dict(p,id=f'skill-{index}')),'confirm':True})
        with self.assertRaises(ValueError):
            self.install()
        ext.dispatch(self.store,1,'install',{'content':json.dumps(dict(p,id='skill-0')),'confirm':True,'replace':True})

    def test_disabled_connector_excluded_from_tools_and_cold_app_routing(self):
        self.store.save(1,'mcp','AcmeDocs','key',url='https://example.com/mcp',meta={'enabled':False})
        box=integrations.Toolbox(None,self.store,{},1)
        self.assertFalse(any(t.get('type')=='mcp' for t in box.definitions()))
        self.store.save(1,'mcp','AcmeDocs','key',url='https://example.com/mcp')
        self.store.save(1,'mcp','AcmeVault','key',url='https://example.com/mcp')
        self.assertTrue(bot.apps_named({'connections':self.store},1,'Посмотри в AcmeVault'))
        self.assertFalse(bot.apps_named({'connections':self.store},2,'Посмотри в AcmeVault'))

    def test_custom_urls_validate_structure(self):
        for url in ['http://example.com','https://localhost/mcp','https://127.0.0.1/mcp','https://user:secret@example.com/mcp','https://example.com/mcp?key=secret','https://example.com/#secret']:
            with self.subTest(url=url),self.assertRaises(ValueError):
                integrations.check_mcp('test',url)
        self.assertEqual(integrations.check_mcp('my-app','https://example.com/mcp')[0],'my_app')

    def test_named_pipedream_app_still_reachable_beyond_budget(self):
        client=Pipedream('client','secret','project')
        self.addCleanup(client.http.close)
        accounts=[{'slug':f'app{i}','name':f'Service {i}','healthy':True} for i in range(15)]
        with patch.object(client,'accounts',return_value=accounts),patch.object(client,'access_token',return_value='fixture'):
            tools,names=client.mcp_tools(1,query='Use Service 14 for this request')
        self.assertEqual(names[0],'Service 14')
        self.assertEqual(len(tools),12)


class SkillWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_model_can_load_installed_skill_and_continue(self):
        with tempfile.TemporaryDirectory() as folder:
            store=integrations.Connections(Path(folder)/'vault','fixture')
            try:
                ext.dispatch(store,1,'install',{'content':MARKDOWN,'confirm':True})
                skill=ext.skill_tool(store,1)[0]
                toolbox=integrations.Toolbox(None,store,{},1,extra=[skill])
                async def first():
                    yield NS(choices=[NS(delta=NS(content=None,tool_calls=[NS(index=0,id='call-1',function=NS(name='load_skill',arguments='{"id":"review/review"}'))]),finish_reason='tool_calls')])
                async def second():
                    yield NS(choices=[NS(delta=NS(content='Reviewed.',tool_calls=[]),finish_reason='stop')])
                create=AsyncMock(side_effect=[first(),second()])
                data={'anymodel_chat':NS(chat=NS(completions=NS(create=create)))}
                live=NS(update=AsyncMock(),status=lambda value:None)
                answer,_=await bot.chat_answer(NS(bot_data=data),live,{'model':'anymodel:xai/grok-4.7',
                    'instructions':toolbox.instructions(),'input':[{'role':'user','content':'Review my changes'}]},toolbox)
                self.assertEqual(answer,'Reviewed.')
                self.assertIn('Read the diff',json.dumps(create.call_args.kwargs['messages']))
                self.assertTrue(any(t['function']['name']=='load_skill' for t in create.call_args_list[0].kwargs['tools']))
            finally:
                store.close()

    async def test_skill_tool_does_not_replace_workspace_permissions(self):
        workspace=NS(definitions=lambda:[{'type':'function','name':'read_file'}],status=lambda *a:'reading',run=AsyncMock(return_value='file'),proposals=['change'])
        reader=AsyncMock(return_value='instructions')
        wrapped=ext.SkillWorkspace(workspace,[{'definition':{'name':'load_skill'},'run':reader}])
        self.assertEqual(len(wrapped.definitions()),2)
        self.assertEqual(await wrapped.run('load_skill',{'id':'x'}),'instructions')
        workspace.run.assert_not_called()
        self.assertEqual(await wrapped.run('read_file',{}),'file')
        self.assertIs(wrapped.proposals,workspace.proposals)

    async def test_mcp_budget_is_shared_and_names_do_not_collide(self):
        mcpbridge._cache.clear()
        async def tools(server):
            return [{'name':('tool'+str(i)), 'inputSchema':{'type':'object'}} for i in range(70)] if server.label=='large' else [{'name':'only'}]
        with patch.object(mcpbridge.Server,'open',AsyncMock()),patch.object(mcpbridge.Server,'tools',tools):
            bridge=await mcpbridge.load(None,[{'server_url':'https://large.example/mcp','server_label':'large'}, {'server_url':'https://small.example/mcp','server_label':'small'}])
        self.assertEqual(len(bridge.functions),60)
        self.assertTrue(bridge.handles('small__only'))
        self.assertNotEqual(mcpbridge._name('app','a'*90+'1'),mcpbridge._name('app','a'*90+'2'))
        self.assertNotEqual(mcpbridge._name('app','a.b'),mcpbridge._name('app','a_b'))
        mcpbridge._cache.clear()

    async def test_probe_does_not_execute_application_tools(self):
        with patch('connector_check.socket.getaddrinfo',return_value=[(None,None,None,None,('93.184.216.34',443))]), \
             patch.object(mcpbridge.Server,'open',AsyncMock()) as opened, \
             patch.object(mcpbridge.Server,'tools',AsyncMock(return_value=[{'name':'read','description':'Read files'}])), \
             patch.object(mcpbridge.Server,'call',AsyncMock()) as called:
            result=await connector_check.probe({'label':'test','url':'https://example.com/mcp','token':'private'})
        self.assertTrue(result['ok']); opened.assert_awaited_once();called.assert_not_called()


class HTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory()
        cls.store=integrations.Connections(Path(cls.folder.name)/'vault','fixture-token')
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),miniapp.make_handler({
            'token':'fixture-token','allowed':{1,2},'store':cls.store,'pipedream':NS(enabled=False),
            'github':None,'direct':NS(connected=lambda uid:[]),'bridge':None}))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.client=httpx.Client(base_url=f'http://127.0.0.1:{cls.server.server_port}')

    @classmethod
    def tearDownClass(cls):
        cls.client.close();cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.store.db.close();cls.folder.cleanup()

    def request(self,action,data=None,uid=1):
        return self.client.post('/api/extensions/'+action,json=data or {},headers={'X-Telegram-Init-Data':init_data(uid)})

    def test_authentication_and_ownership_on_real_http_routes(self):
        self.assertEqual(self.client.post('/api/extensions/state',json={}).status_code,401)
        self.assertEqual(self.request('state',uid=3).status_code,403)
        self.assertEqual(self.request('install',{'content':MARKDOWN,'confirm':True,'user_id':2}).status_code,200)
        self.assertEqual(self.request('state',uid=2).json()['installed'],[])
        self.assertEqual(self.request('delete',{'id':'review','confirm':True},uid=2).status_code,400)
        self.assertEqual(self.request('install',{'content':MARKDOWN}).status_code,400)

    def test_connector_checks_use_only_owned_saved_tokens(self):
        self.store.save(1,'mcp','private','secret-token',url='https://example.com/mcp')
        with patch('connector_check.check',return_value={'ok':True,'count':0,'tools':[]}) as check:
            self.assertEqual(self.request('check',{'label':'private'},uid=2).status_code,400)
            check.assert_not_called()
            self.assertTrue(self.request('check',{'label':'private','token':'forged'}).json()['ok'])
            self.assertEqual(check.call_args.args[0]['token'],'secret-token')
        self.assertEqual(self.request('connector-toggle',{'label':'private','enabled':False},uid=2).status_code,400)

    def test_builtin_catalog_survives_external_catalog_failure(self):
        # The same route with a failing Pipedream client still returns direct connectors.
        config={'pipedream':NS(enabled=True,catalog=lambda *a:(_ for _ in ()).throw(PipedreamError('offline'))),
                'github':None,'direct':None,'store':self.store,'bridge':None}
        isolated=miniapp.make_handler(config)
        receiver=object.__new__(isolated);results=[];receiver._send=lambda status,body:results.append((status,body))
        receiver._route('/api/catalog',{'id':1,'language_code':'ru'},{'q':'страницы'})
        self.assertEqual(results[0][0],200)
        self.assertTrue(results[0][1]['error'])
        self.assertTrue(any(a['slug']=='notion' for a in results[0][1]['apps']))


if __name__=='__main__':
    unittest.main()
