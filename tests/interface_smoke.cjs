/* Local visual/interaction QA. Requires Playwright browsers; never uses a real account or API key. */
const {chromium,webkit}=require('playwright');
const fs=require('fs');const path=require('path');const http=require('http');const assert=require('assert/strict');
const root=path.resolve(__dirname,'..');const output=process.env.SECRET_UI_OUTPUT||'/tmp/secret-interface-qa';fs.mkdirSync(output,{recursive:true});
const model='anymodel:xai/grok-4.7';
const state={user:{first_name:'Рахим',id:1},bot:'secret_ai_test_bot',github:{connected:false,ready:false,owner:true},mcp:[],apps:{enabled:false,connected:[]},secretary:{enabled:false,connected:false,rules:'',notify:true},groups:[{id:-1,title:'Наш круг',level:'normal',messages:128,stickers:8,memes:3}],settings:{model,models:[model],persona:'assistant',premium:{},messages:42,personas:[{id:'assistant',emoji:'',ru:'Ассистент',en:'Assistant'}]},gen:{ready:true,owner:true,unlimited:true,credits:100,free_left:3,free_per_day:5,video_free:true,video_ready:true,video_on:true}};
const chats=[{thread:'first',title:'Идеи для моего проекта',repo:'',mode:'project',updated:Date.now()/1000,count:24,status:'done'},{thread:'second',title:'Письмо на немецком',mode:'project',repo:'',updated:Date.now()/1000-3600,count:2,status:'done'}];
const turns=Array.from({length:45},(_,i)=>({id:'turn'+i,thread:'first',prompt:i===44?'Сделай короче и сохрани главное.':'Помоги с проектом, шаг '+(i+1),summary:'Подготовил следующий вариант.\n\nПервый шаг — уточнить задачу и сохранить нужные материалы. Затем можно перейти к реализации и проверить результат.',status:'done',model,mode:'project',files:{},log:[],created:Date.now()/1000-600+i}));
let tasks=[{id:1,title:'Подобрать компьютер',details:'Бюджет до 300 евро. Нужен для учёбы и разработки.',next_step:'Сравнить два объявления и подготовить вопросы продавцу.',status:'open',priority:1,due:null},{id:2,title:'Подготовиться к контрольной',details:'Повторить основные правила и решить пять примеров.',next_step:'Начать с короткой проверки знаний.',status:'open',priority:2,due:null}];
const day=()=>({tasks,timezone:'Europe/Berlin',mode:'auto',inbox_count:3,plan:'План дня\n\n#1 Подобрать компьютер\nСравнить объявления.',review:'За сегодня завершено: 0\nОсталось дел: 2'});
const templates={ru:[{key:'studio',kind:'photo',emoji:'',title:'Студийный портрет',style:'portrait',c1:'#5782ad',c2:'#253753',preview:false},{key:'anime',kind:'photo',emoji:'',title:'Иллюстрация',style:'illustration',c1:'#6b708a',c2:'#253753',preview:false},{key:'cinema',kind:'video',emoji:'',title:'Киносцена',style:'cinema',c1:'#849daa',c2:'#253753',preview:false}],en:[]};
const html=fs.readFileSync(path.join(root,'webapp/index.html'),'utf8').replace('__LOGO__',fs.readFileSync(path.join(root,'webapp/logo.svg'),'utf8')).replace('__TEMPLATES__',JSON.stringify(templates)).replace('__APPS__',JSON.stringify({ru:[],en:[]}));
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://localhost');
 if(url.pathname.startsWith('/api/')){let body='';req.on('data',b=>body+=b);req.on('end',()=>{const data=JSON.parse(body||'{}');let response={ok:true};
 if(url.pathname==='/api/state')response=state;
 else if(url.pathname==='/api/catalog')response={apps:[],next:null,enabled:false};
 else if(url.pathname==='/api/codex/state')response={chats,github:null,can_run:false};
 else if(url.pathname==='/api/codex/thread')response={tasks:turns};
 else if(url.pathname==='/api/day/state')response=day();
 else if(url.pathname==='/api/day/status'){tasks=tasks.map(t=>t.id===data.id?{...t,status:data.status}:t);response=day();}
 else if(url.pathname==='/api/day/task')response={result:'Guten Tag! Ist der Computer noch verfügbar?'};
 res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(response));});return;}
 if(['/play-core.js','/play-ui.js','/play.css','/updates.js'].includes(url.pathname)){res.writeHead(200,{'Content-Type':url.pathname.endsWith('.css')?'text/css':'application/javascript'});res.end(fs.readFileSync(path.join(root,'webapp',url.pathname.slice(1))));return;}
 res.writeHead(200,{'Content-Type':url.pathname==='/'?'text/html':url.pathname==='/logo.svg'?'image/svg+xml':'text/html'});
 res.end(url.pathname==='/logo.svg'?fs.readFileSync(path.join(root,'webapp/logo.svg')):html);
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+server.address().port;const reports=[];
 for(const [engine,name] of [[chromium,'chromium'],[webkit,'webkit']]){
 const launch=()=>engine.launch({headless:true,...(name==='chromium'&&process.env.SECRET_UI_CHROMIUM?{executablePath:process.env.SECRET_UI_CHROMIUM,args:['--no-sandbox','--single-process','--no-zygote','--disable-dev-shm-usage']}: {})});let browser;try{browser=await launch();}catch(e){if(name==='webkit'&&process.env.SECRET_UI_SKIP_WEBKIT==='1'){reports.push({engine:name,skipped:e.message.split('\n')[0]});continue;}throw e;}
 try{for(const [width,height,label] of [[375,812,'mobile'],[1440,1000,'desktop']]){
 tasks=tasks.map(t=>({...t,status:'open'}));const context=await browser.newContext({viewport:{width,height},locale:'ru-RU',colorScheme:'dark',reducedMotion:process.env.SECRET_UI_MOTION==='1'?'no-preference':'reduce'});
 await context.route('https://telegram.org/**',r=>r.fulfill({body:''}));
 await context.addInitScript(()=>{window.Telegram={WebApp:{initData:'fixture',initDataUnsafe:{user:{id:1,first_name:'Рахим',language_code:'ru'}},colorScheme:'dark',ready(){},expand(){},isVersionAtLeast(){return true;},onEvent(){},setHeaderColor(){},setBackgroundColor(){},setBottomBarColor(){},disableVerticalSwipes(){},openTelegramLink(url){window.lastTelegramLink=url;},BackButton:{show(){},hide(){},onClick(){}}}};});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>{errors.push(e.message);console.error('BROWSER',name,label,e.message);});
 await page.goto(origin);await page.waitForFunction(()=>typeof bootReady!=='undefined'&&bootReady);await page.waitForSelector('#splash',{state:'detached'});
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);assert.equal(overflow,false,name+' '+label+' horizontal overflow');
 assert.equal(await page.locator('.action-card').count(),4);
 await page.screenshot({path:path.join(output,`${name}-${label}-home.png`),fullPage:true});
 page.on('console',m=>{if(m.text().startsWith('NAV DEBUG'))console.log(m.text());});
 await page.evaluate(()=>{const previous=switchTab;switchTab=tab=>{console.log('NAV DEBUG SWITCH',tab);return previous(tab);};document.addEventListener('click',e=>console.log('NAV DEBUG CLICK',e.target.outerHTML.slice(0,200)),true);});
 console.log('HOME BUTTON',await page.locator('[data-home-action=day]').evaluate(b=>({action:b.dataset.homeAction,handler:!!document.getElementById('home-actions').onclick})));
 await page.locator('[data-home-action=day]').click();
 await page.waitForSelector('.day-card',{timeout:10000}).catch(async e=>{console.error('DAY DEBUG',await page.evaluate(()=>({tab:document.body.dataset.tab,cards:document.querySelectorAll('.day-card').length,content:document.getElementById('day-content').innerHTML})),errors);throw e;});await page.screenshot({path:path.join(output,`${name}-${label}-tasks.png`),fullPage:true});
 await page.locator('[data-day-id="1"]').click();await page.waitForFunction(()=>document.getElementById('day-result')?.textContent.includes('Guten Tag'));
 await page.locator('[data-day-status=done]').click();await page.waitForFunction(()=>!document.getElementById('sheet').classList.contains('open'));
 assert.equal(await page.locator('.day-card').count(),1);
 await page.locator('[data-tab=gen].tab').click();await page.waitForSelector('[data-gen=photo]');await page.screenshot({path:path.join(output,`${name}-${label}-studio.png`),fullPage:true});
 await page.locator('[data-gen=photo]').click();await page.waitForSelector('#sheet.open');await page.screenshot({path:path.join(output,`${name}-${label}-generation.png`)});await page.locator('#sheet .close').click();
 await page.locator('[data-tab=agent].tab').click();await page.locator('[data-home-action=games]').click();assert.equal(await page.locator('.game-card').count(),3);assert.equal(await page.locator('[data-play]').count(),3);await page.screenshot({path:path.join(output,`${name}-${label}-games.png`),fullPage:true});
 if(process.env.SECRET_PLAY_FULL==='1'){
 await page.clock.install();
 const saved=()=>page.evaluate(()=>JSON.parse(localStorage.getItem('secret-play-v1:1')));
 const current=async()=>{const d=await saved();return page.evaluate(s=>{let g=SecretPlayCore.create(s.kind,s.seed,s.difficulty);for(const a of s.actions)g=SecretPlayCore.act(g,a);return g;},d.active);};
 const openGame=async kind=>{await page.locator(`[data-play="${kind}"]`).click();await page.locator('[data-diff="1"]').click();await page.locator('#play-start').click();};
 const solveSignal=async()=>{for(let round=0;round<5;round++){const g=await current(),r=g.rounds[g.round];await page.locator('#play-watch').click();await page.clock.runFor(12000);assert.equal(await page.locator('.play-pad button:disabled').count(),0);for(const cell of r.reverse?r.sequence.slice().reverse():r.sequence)await page.locator(`[data-cell="${cell}"]`).click();}};
 const solveVault=async()=>{const g=await current();for(const digit of g.secret)await page.locator(`[data-digit="${digit}"]`).click();await page.locator('#play-guess').click();};
 const solveRoute=async()=>{let g=await current();const steps=await page.evaluate(g=>{let all=[],at=g.position;for(const target of [...g.keys.filter(k=>!g.collected.includes(k)),g.exit]){const q=[at],prev=new Map([[at,null]]);for(let i=0;i<q.length;i++)for(const n of SecretPlayCore.neighbours(q[i],g.size))if(!g.cells[n]&&!prev.has(n)){prev.set(n,q[i]);q.push(n);}const part=[];for(let n=target;n!==at;n=prev.get(n))part.unshift(n);all.push(...part);at=target;}return all;},g);for(const cell of steps)await page.evaluate(i=>document.querySelector(`[data-step="${i}"]`)?.click(),cell);};
 await openGame('vault');await page.screenshot({path:path.join(output,`${name}-${label}-vault.png`)});await solveVault();assert.equal(await page.locator('.play-result').count(),1);assert.equal((await saved()).history.length,1);await page.locator('#play-all').click();
 await openGame('route');const before=await current();const next=await page.evaluate(g=>SecretPlayCore.neighbours(g.position,g.size).find(n=>!g.cells[n]),before);await page.keyboard.press(next===before.position+1?'ArrowRight':next===before.position-1?'ArrowLeft':next===before.position+before.size?'ArrowDown':'ArrowUp');assert.equal((await current()).moves,1);await page.reload();await page.waitForFunction(()=>typeof bootReady!=='undefined'&&bootReady);await page.waitForSelector('#splash',{state:'detached'});await page.locator('[data-home-action=games]').click();await page.locator('#play-resume').click();assert.equal((await current()).moves,1);assert.ok((await page.locator('.play-session').boundingBox()).y>=-1);await page.screenshot({path:path.join(output,`${name}-${label}-route.png`)});await solveRoute();assert.equal(await page.locator('.play-result').count(),1);await page.locator('#play-all').click();
 await openGame('signal');await page.locator('#play-watch').click();await page.clock.runFor(1000);await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});await page.clock.runFor(10000);assert.equal(await page.locator('.play-pad button:disabled').count(),9);await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));});await page.screenshot({path:path.join(output,`${name}-${label}-signal.png`)});await solveSignal();assert.equal(await page.locator('.play-result').count(),1);await page.locator('#play-all').click();assert.equal((await saved()).history.length,3);assert.ok((await saved()).medals.includes('first'));
 await page.locator('#play-daily').click();await solveSignal();await page.locator('#play-again').click();await solveVault();await page.locator('#play-again').click();await solveRoute();await page.screenshot({path:path.join(output,`${name}-${label}-daily-result.png`)});assert.equal((await saved()).history.length,6);assert.ok(Object.keys((await saved()).daily).length===1);await page.locator('#play-all').click();
 // Account-scoped records and corrupted-save recovery are isolated from other app data.
 await page.evaluate(()=>SecretPlay.mount(document.getElementById('play-root'),{lang:'ru',userId:99}));assert.equal(await page.locator('.play-record').count(),0);
 await page.evaluate(()=>{localStorage.setItem('secret-play-v1:99','broken');SecretPlay.mount(document.getElementById('play-root'),{lang:'ru',userId:99});});assert.equal(await page.locator('[data-play]').count(),3);
 await page.evaluate(()=>SecretPlay.mount(document.getElementById('play-root'),{lang:'ru',userId:1}));assert.equal(await page.locator('.play-record').count(),5);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
 await page.clock.resume();
 }
 await page.locator('[data-tab=settings].tab').click();await page.locator('#set-appearance').click();await page.locator('[data-appearance=light]').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
 await page.waitForFunction(()=>getComputedStyle(document.getElementById('scrim')).opacity==='0'&&getComputedStyle(document.getElementById('sheet')).visibility==='hidden');
 await page.screenshot({path:path.join(output,`${name}-${label}-settings-light.png`),fullPage:true});
 await page.evaluate(()=>switchTab('games'));await page.screenshot({path:path.join(output,`${name}-${label}-games-light.png`),fullPage:true});await page.locator('[data-tab=settings].tab').click();
 await page.reload();await page.waitForFunction(()=>typeof bootReady!=='undefined'&&bootReady);assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
 await page.locator('[data-tab=codex].tab').click();await page.waitForSelector('#cx-text');
 await page.locator('#cx-list').click();await page.waitForSelector('#chat-search');await page.locator('#chat-search').fill('немецком');assert.equal(await page.locator('#sheet [data-thread]:visible').count(),1);await page.locator('#chat-search').fill('');await page.locator('[data-pin-chat=first]').click();assert.equal(await page.locator('[data-pin-chat=first]').getAttribute('aria-pressed'),'true');await page.locator('#new-chat-folder').click();await page.locator('#folder-name').fill('Мои проекты');await page.locator('#folder-save').click();assert.equal(await page.locator('[data-chat-folder]').count(),3);await page.locator('[data-move-chat=first]').click();await page.locator('[data-move-folder]').last().click();await page.locator('[data-chat-folder]').last().click();assert.equal(await page.locator('[data-thread]').count(),1);await page.locator('[data-chat-folder=all]').click();await page.locator('#sheet [data-thread=first]').click();await page.waitForSelector('#cx-older');
 assert.equal(await page.locator('.cx-msg.me').count(),20);await page.locator('#cx-older').click();assert.equal(await page.locator('.cx-msg.me').count(),40);
 await page.locator('#cx-text').fill('Черновик, который должен сохраниться');await page.locator('#cx-text').focus();await page.evaluate(()=>loadCodex());assert.equal(await page.locator('#cx-text').inputValue(),'Черновик, который должен сохраниться');assert.equal(await page.locator('#cx-text').evaluate(el=>el===document.activeElement),true);
 const geometry=await page.evaluate(()=>{const r=document.getElementById('cx-composer').getBoundingClientRect();return {bottom:r.bottom,height:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth+1};});assert.ok(geometry.bottom<=geometry.height+1,'composer below viewport');assert.equal(geometry.overflow,false);
 if(label==='mobile'){
   await page.setViewportSize({width,height:500});await page.waitForFunction(()=>document.body.dataset.keyboard==='true');
   const r=await page.locator('#cx-composer').boundingBox();assert.ok(r.y+r.height<=501,'keyboard covers composer');
   assert.equal(await page.locator('#cx-text').inputValue(),'Черновик, который должен сохраниться');
   await page.screenshot({path:path.join(output,`${name}-${label}-keyboard.png`)});
   await page.setViewportSize({width,height});await page.waitForFunction(()=>document.body.dataset.keyboard==='false');
 }
 await page.screenshot({path:path.join(output,`${name}-${label}-chat.png`)});
 assert.deepEqual(errors,[]);reports.push({engine:name,viewport:label,arcade:process.env.SECRET_PLAY_FULL==='1'?'three solo wins, full daily run, reload resume, hidden playback pause, account isolation, corrupt save: passed':'hub only',checks:'navigation, themes, tasks, studio, games, history, draft focus, geometry: passed'});await context.close();if(label==='mobile'){await browser.close();browser=await launch();}
 }}finally{await browser.close();}
 }
 fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(reports,null,2));console.log(JSON.stringify(reports,null,2));
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>server.close());
