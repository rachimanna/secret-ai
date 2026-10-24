const {chromium}=require("playwright");
const fs=require("fs"),path=require("path"),http=require("http"),assert=require("assert/strict");
const root=path.resolve(__dirname,"..");
const model="anymodel:xai/grok-4.7";
const fixture={user:{id:1,first_name:"Тест"},bot:"test_bot",github:{connected:false,ready:false,owner:true},mcp:[],apps:{enabled:false,connected:[]},secretary:{enabled:false,connected:false},groups:[],
 settings:{model,models:[model],persona:"assistant",premium:{},messages:2,personas:[{id:"assistant",emoji:"",ru:"Ассистент",en:"Assistant"}]},gen:{ready:true,owner:true,unlimited:true,credits:100,free_left:3,free_per_day:5,video_free:true,video_ready:true,video_on:true}};
let docs={projects:[],project:0,selected:null,files:[]},calls=[];
const html=fs.readFileSync(path.join(root,"webapp/index.html"),"utf8").replace("__LOGO__",fs.readFileSync(path.join(root,"webapp/logo.svg"),"utf8")).replace("__TEMPLATES__",JSON.stringify({ru:[],en:[]})).replace("__APPS__",JSON.stringify({ru:[],en:[]}));
const server=http.createServer((req,res)=>{
 const pathname=new URL(req.url,"http://localhost").pathname;
 if(pathname.startsWith("/api/")){
  let raw="";req.on("data",b=>raw+=b);req.on("end",()=>{
   const data=JSON.parse(raw||"{}");calls.push({path:pathname,data,auth:req.headers["x-telegram-init-data"]});let result={ok:true};
   if(pathname==="/api/state")result=fixture;
   else if(pathname==="/api/catalog")result={apps:[],next:null,enabled:false};
   else if(pathname==="/api/codex/state")result={chats:[],github:null,can_run:false};
   else if(pathname==="/api/day/state")result={tasks:[{id:1,title:"Отчёт",status:"open"}],timezone:"Europe/Moscow",inbox_count:0};
   else if(pathname==="/api/features/documents")result=docs;
   else if(pathname==="/api/features/upload"){docs={...docs,selected:1,files:[{id:1,name:data.name,version:1,warnings:[]}]};result=docs;}
   else if(pathname==="/api/features/file-use")result=docs;
   else if(pathname==="/api/features/history")result={scope:"Личная история",results:[{role:"user",created:1,text:"<script>private</script>"}]};
   else if(pathname==="/api/features/reminders")result={reminders:[],timezone:"Europe/Moscow"};
   else if(pathname==="/api/features/status")result={version:"test",free_bytes:1000000,active_tasks:0,models:[model],providers:[],backup:{enabled:true,last_success:1,last_error:""}};
   else if(pathname==="/api/features/backups")result={versions:[{id:7,created:"2026-10-24T00:00:00Z",bytes:100}]};
   res.writeHead(200,{"Content-Type":"application/json"});res.end(JSON.stringify(result));
  });return;
 }
 if(["/updates.js","/play-core.js","/play-ui.js","/play.css","/logo.svg"].includes(pathname)){
  res.writeHead(200,{"Content-Type":pathname.endsWith(".js")?"application/javascript":pathname.endsWith(".css")?"text/css":"image/svg+xml"});
  res.end(fs.readFileSync(path.join(root,"webapp",pathname.slice(1))));return;
 }
 res.writeHead(200,{"Content-Type":"text/html"});res.end(html);
});
(async()=>{
 await new Promise(r=>server.listen(0,"127.0.0.1",r));
 const browser=await chromium.launch({headless:true});
 try {
  const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:"reduce"});
  await context.route("https://telegram.org/**",r=>r.fulfill({body:""}));
  await context.addInitScript(()=>{window.Telegram={WebApp:{initData:"fixture",initDataUnsafe:{user:{id:1,language_code:"ru"}},ready(){},expand(){},onEvent(){},isVersionAtLeast(){return true;},setHeaderColor(){},setBackgroundColor(){},setBottomBarColor(){},disableVerticalSwipes(){},BackButton:{show(){},hide(){},onClick(){}}}};});
  const page=await context.newPage(),errors=[];page.on("pageerror",e=>{errors.push(e.message);console.error("UPDATES BROWSER",e.message);});
  await page.goto("http://127.0.0.1:"+server.address().port);
  await page.waitForFunction(()=>typeof bootReady!=="undefined"&&bootReady,{},{timeout:10000}).catch(async e=>{console.error("BOOT DEBUG",await page.locator("#boot-error").textContent().catch(()=>""),errors);throw e;});
  await page.waitForSelector("#splash",{state:"detached"});
  await page.locator("#fx-documents").click();
  await page.locator("#fx-upload").setInputFiles({name:"notes.txt",mimeType:"text/plain",buffer:Buffer.from("hello")});
  await page.waitForSelector("[data-fx-file='1']");
  await page.locator("[data-fx-file='1']").click();
  await page.waitForSelector("#fx-text");
  await page.locator("#sheet .close").click();
  await page.locator("#fx-history").click();
  await page.locator("#fx-query").fill("server");await page.locator("#fx-search").click();
  await page.waitForFunction(()=>document.getElementById("fx-results")?.textContent.includes("<script>"));
  assert.equal(await page.locator("#fx-results script").count(),0,"history text must be escaped");
  await page.locator("#sheet .close").click();
  await page.locator("#fx-reminders").click();await page.locator("#fx-when").fill("по будням в 18:00");await page.locator("#fx-remind").click();
  await page.waitForFunction(()=>!!document.getElementById("fx-remind"));
  await page.locator("#sheet .close").click();
  await page.locator("#fx-status").click();await page.locator("#fx-backups").click();
  await page.waitForSelector("[data-fx-restore]");
  page.once("dialog",d=>d.dismiss());
  await page.locator("[data-fx-restore]").click();
  assert.equal(calls.filter(c=>c.path==="/api/features/restore").length,0,"cancelled confirmation must not restore");
  assert(calls.filter(c=>c.path.startsWith("/api/features/")).every(c=>c.auth==="fixture"));
  assert(calls.some(c=>c.path==="/api/features/remind"&&c.data.when==="по будням в 18:00"));
  assert.deepEqual(errors,[]);
  console.log("Updates browser flow passed");
 } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
