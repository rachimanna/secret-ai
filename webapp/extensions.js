/* Declarative plugins and private skills. Shares the Mini App's authenticated API and sheets. */
(() => {
  const ru = lang === "ru", tr = (a,b) => ru ? a : b;
  let library = null, busy = false, previewSeq = 0;
  const fail = e => toast(e.detail || e.message || T.err);
  const ask = text => new Promise(resolve => tg?.showConfirm && tg.isVersionAtLeast?.('6.2') ? tg.showConfirm(text,resolve) : resolve(window.confirm(text)));
  async function call(action, body = {}) {
    const response = await fetch('/api/extensions/' + action, {method:'POST',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':tg?.initData || ''},body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || errorText({code:data.error}));
    return data;
  }
  const button = (id,text) => `<button class="btn secondary" id="${id}" style="margin-top:8px">${esc(text)}</button>`;
  const head = (title,sub) => `<div class="head"><h3>${esc(title)}</h3><p class="lead">${esc(sub)}</p></div>`;
  function draw() {
    const hub = $('extension-hub');
    hub.innerHTML = `<div class="list" style="margin-bottom:16px">
      <button class="row press" id="ext-library"><span class="ic">✦</span><span class="body"><span class="t">${tr('Навыки и плагины','Skills and plugins')}</span><span class="d">${tr('Установить, создать и настроить','Install, create and manage')}</span></span><span>›</span></button>
      <button class="row press" id="ext-connectors"><span class="ic">${SYM.mcp}</span><span class="body"><span class="t">${tr('Мои подключения','My connections')}</span><span class="d">${tr('Приложения и свои MCP-серверы','Apps and custom MCP servers')}</span></span><span>›</span></button></div>`;
    $('ext-library').onclick = () => openLibrary();
    $('ext-connectors').onclick = openConnections;
  }
  async function openLibrary() {
    if (busy) return;
    busy = true;
    const seq = sheetSeq, tab = document.body.dataset.tab;
    try { library = await call('state'); if(seq === sheetSeq && tab === document.body.dataset.tab) showLibrary(); }
    catch (e) { fail(e); }
    finally { busy = false; }
  }
  function showLibrary() {
    openSheet(head(tr('Навыки и плагины','Skills and plugins'),tr('Навыки учат ИИ работать по вашим правилам. Плагины объединяют навыки и подключения.','Skills teach AI your workflows. Plugins bundle skills and connection suggestions.')) +
      button('ext-import',tr('Импорт SKILL.md / JSON / ZIP','Import SKILL.md / JSON / ZIP')) + button('ext-create',tr('Создать свой навык','Create a skill')) +
      `<h4>${tr('Установлено','Installed')}</h4><div id="ext-installed" class="list">${(library.installed || []).map(p => `<button class="row press" data-ext-id="${esc(p.id)}"><span class="body"><span class="t">${esc(p.name)}</span><span class="d">${esc(p.description)} · ${p.enabled ? tr('Включён','Enabled') : tr('Выключен','Disabled')}</span></span><span>›</span></button>`).join('') || `<p class="lead">${tr('Пока пусто. Выберите готовый навык ниже или добавьте свой.','Choose a starter skill below or import your own.')}</p>`}</div>
      <h4>${tr('Готовые навыки','Starter skills')}</h4><div class="list">${(library.catalog || []).map(p => `<button class="row press" data-ext-builtin="${esc(p.id)}"><span class="body"><span class="t">${esc(p.name)}</span><span class="d">${esc(p.description)}</span></span><span>＋</span></button>`).join('')}</div>`);
    $('ext-import').onclick = importSheet;
    $('ext-create').onclick = createSheet;
    $('sheet').querySelectorAll('[data-ext-id]').forEach(b => b.onclick = () => detail(library.installed.find(p => p.id === b.dataset.extId)));
    $('sheet').querySelectorAll('[data-ext-builtin]').forEach(b => b.onclick = () => preview({builtin:b.dataset.extBuiltin}));
  }
  function importSheet() {
    openSheet(head(tr('Импорт расширения','Import an extension'),tr('Самостоятельный SKILL.md, plugin.json или ZIP с такими файлами. До 512 КиБ. Код, hooks и дополнительные ресурсы не устанавливаются.','Standalone SKILL.md, plugin.json or ZIP containing these files. Up to 512 KiB. Code, hooks and extra resources are not installed.')) +
      `<label for="ext-file">${tr('Файл расширения','Extension file')}</label><input class="field" type="file" id="ext-file" accept=".md,.json,.zip">
      <label for="ext-source">${tr('Или вставьте содержимое SKILL.md / JSON','Or paste SKILL.md / JSON')}</label><textarea class="field" id="ext-source" rows="9" maxlength="524288"></textarea>` + button('ext-preview',tr('Посмотреть перед установкой','Preview installation')));
    $('ext-file').onchange = async e => {
      const file = e.target.files[0]; if (!file) return;
      if (file.size > 524288) { toast(tr('Максимум 512 КиБ','Maximum 512 KiB')); return; }
      const bytes = new Uint8Array(await file.arrayBuffer());
      let raw = ''; for (let i=0;i<bytes.length;i+=8192) raw += String.fromCharCode(...bytes.subarray(i,i+8192));
      await preview({encoding:'base64',content:btoa(raw)});
    };
    $('ext-preview').onclick = () => preview({content:$('ext-source').value});
  }
  function createSheet() {
    openSheet(head(tr('Свой навык','Create a skill'),tr('Опишите, когда применять навык и как выполнять задачу. После сохранения он доступен в личном чате и задачах Mini App.','Describe when to apply the skill and how to do the task. It becomes available in private chat and Mini App tasks.')) +
      `<label for="ext-name">${tr('Название','Name')}</label><input class="field" id="ext-name" maxlength="80">
      <label for="ext-description">${tr('Когда применять','When to use')}</label><textarea class="field" id="ext-description" rows="2" maxlength="600"></textarea>
      <label for="ext-instructions">${tr('Инструкции','Instructions')}</label><textarea class="field" id="ext-instructions" rows="9" maxlength="16000"></textarea>` + button('ext-save-preview',tr('Посмотреть перед установкой','Preview installation')));
    $('ext-save-preview').onclick = () => {
      const name = $('ext-name').value.trim(), description = $('ext-description').value.trim();
      const id = 'custom-' + Date.now().toString(36);
      preview({content:JSON.stringify({id,name,description,skills:[{id:'main',name,description,instructions:$('ext-instructions').value}],connectors:[]})});
    };
  }
  async function preview(payload) {
    const seq = ++previewSeq, sheet = sheetSeq;
    try {
      const result = await call('preview',payload);
      if (seq !== previewSeq || sheet !== sheetSeq) return;
      const p = result.package;
      openSheet(head(p.name, p.description) + `<p class="lead">${tr('Версия','Version')}: ${esc(p.version)} · ${p.skills.length} ${tr('навыков','skills')} · ${p.connectors.length} ${tr('подключений','connections')}</p>` + contents(p) +
        `<p class="lead">${tr('Подключения добавляются отдельно кнопкой после установки. Ключи и доступ к аккаунтам пакет не предоставляет.','Connections are added separately after installation. A package does not grant account access or provide keys.')}</p>` +
        `<button class="btn" id="ext-install">${result.replaces ? tr('Заменить установленный плагин','Replace installed plugin') : tr('Установить','Install')}</button>`);
      $('ext-install').onclick = async e => {
        e.currentTarget.disabled = true;
        try {
          // Install precisely what the user previewed, not a mutable external source.
          library = await call('install',{content:JSON.stringify(p),confirm:true,replace:result.replaces});
          detail(library.installed.find(x => x.id === p.id)); toast(T.saved);
        } catch (error) { fail(error); $('ext-install')?.removeAttribute('disabled'); }
      };
    } catch (e) { if (seq === previewSeq && sheet === sheetSeq) fail(e); }
  }
  function contents(p) {
    return p.skills.map(s => `<details style="margin:12px 0"><summary>${esc(s.name)}</summary><p class="lead">${esc(s.description)}</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere;font:inherit">${esc(s.instructions)}</pre></details>`).join('') +
      p.connectors.map(c => `<p class="lead">${esc(c.label)} · ${esc(c.url)}</p>`).join('');
  }
  function detail(p) {
    if (!p) return;
    openSheet(head(p.name,p.description) + `<p class="lead">${esc(p.id)} · ${esc(p.version)} · ${p.enabled ? tr('Включён','Enabled') : tr('Выключен','Disabled')}</p>` + contents(p) +
      button('ext-toggle',p.enabled ? tr('Выключить','Disable') : tr('Включить','Enable')) + button('ext-export',tr('Скачать плагин','Download plugin')) +
      button('ext-edit',tr('Изменить инструкции','Edit instructions')) +
      p.connectors.map((c,i) => button('ext-connect-' + i,tr('Подключить ','Connect ') + c.label)).join('') + button('ext-delete',tr('Удалить плагин','Delete plugin')) + button('ext-back',tr('Все навыки','All skills')));
    $('ext-toggle').onclick = async () => { try { library = await call('toggle',{id:p.id,enabled:!p.enabled}); detail(library.installed.find(x=>x.id===p.id)); } catch(e) {fail(e);} };
    $('ext-export').onclick = async () => { try { const file = await call('export',{id:p.id}); const url = URL.createObjectURL(new Blob([file.content],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=file.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000); } catch(e) {fail(e);} };
    $('ext-delete').onclick = async () => { if (!await ask(tr('Удалить этот плагин? Подключённые отдельно приложения останутся.','Delete this plugin? Separately connected apps will remain.'))) return; try { library = await call('delete',{id:p.id,confirm:true});showLibrary(); } catch(e) {fail(e);} };
    $('ext-back').onclick = showLibrary;
    $('ext-edit').onclick = () => {
      const {enabled,...source}=p;
      openSheet(head(tr('Редактор плагина','Plugin editor'),tr('Сохраните ID, чтобы обновить существующий плагин.','Keep the ID to update the installed plugin.')) +
        `<label for="ext-edit-source">plugin.json</label><textarea class="field" id="ext-edit-source" rows="15"></textarea>` + button('ext-edit-preview',tr('Посмотреть изменения','Preview changes')));
      $('ext-edit-source').value=JSON.stringify(source,null,2);
      $('ext-edit-preview').onclick=()=>preview({content:$('ext-edit-source').value});
    };
    p.connectors.forEach((c,i)=>$('ext-connect-'+i).onclick=()=>connectorForm(c));
  }
  function openConnections() {
    const rows = state?.mcp || [];
    const apps = (state?.apps?.connected || []).map(a=>({...a,description:(a.account || T.connected)+(a.healthy === false ? ' · ⚠️' : '')}));
    if(state?.github?.connected) apps.unshift({slug:'github',name:'GitHub',img:GH_LOGO,description:'@'+state.github.login});
    openSheet(head(tr('Мои подключения','My connections'),tr('Подключите приложение из каталога или добавьте публичный HTTPS MCP-сервер. Проверка получает список инструментов без выполнения действий.','Connect an app from the catalog or add a public HTTPS MCP server. The check lists tools without executing actions.')) +
      button('ext-add-connector',tr('Добавить MCP-сервер','Add MCP server')) + button('ext-app-catalog',tr('Открыть каталог приложений','Browse apps')) +
      `<div class="list">${apps.map(appRow).join('')}</div>` +
      `<div class="list">${rows.map((r,i)=>`<div class="row"><div class="body"><div class="t">${esc(r.label)}</div><div class="d">${esc(r.url)} · ${r.enabled !== false ? tr('Включён','Enabled') : tr('Выключен','Disabled')}</div><button class="press" data-ext-check="${i}">${tr('Проверить инструменты','Check tools')}</button> · <button class="press" data-ext-toggle="${i}">${r.enabled !== false ? tr('Выключить','Disable') : tr('Включить','Enable')}</button> · <button class="press" data-ext-remove="${i}">${tr('Удалить','Delete')}</button></div></div>`).join('')}</div><div id="ext-check-result" role="status" style="white-space:pre-wrap;overflow-wrap:anywhere"></div>`);
    $('ext-add-connector').onclick=()=>connectorForm();
    $('ext-app-catalog').onclick=()=>{closeSheet();switchTab('apps');$('search').focus();};
    $('sheet').querySelectorAll('[data-ext-check]').forEach(b=>b.onclick=async()=>{
      b.disabled=true;const target=$('ext-check-result');target.textContent=tr('Проверяю подключение…','Checking connection…');
      try { const r=await call('check',{label:rows[+b.dataset.extCheck].label});if(target.isConnected)target.textContent=r.ok ? tr('Доступно инструментов: ','Available tools: ')+r.count+'\n'+r.tools.map(t=>t.name+' — '+t.description).join('\n') : r.message; }catch(e){fail(e);}finally{b.disabled=false;}
    });
    $('sheet').querySelectorAll('[data-ext-toggle]').forEach(b=>b.onclick=async()=>{const row=rows[+b.dataset.extToggle];try{await call('connector-toggle',{label:row.label,enabled:row.enabled===false});await load(true);openConnections();}catch(e){fail(e);}});
    $('sheet').querySelectorAll('[data-ext-remove]').forEach(b=>b.onclick=async()=>{const row=rows[+b.dataset.extRemove];if(!await ask(tr('Удалить подключение ','Delete connection ')+row.label+'?'))return;try{state=await api('/api/disconnect',{service:'mcp',label:row.label});renderAll();openConnections();}catch(e){fail(e);}});
  }
  function connectorForm(preset={}) {
    openSheet(head(tr('Добавить подключение','Add a connection'),tr('Публичный HTTPS endpoint MCP Streamable HTTP. Для OAuth используйте каталог. Действия подключённого приложения выполняются по запросам к ИИ.','Public HTTPS MCP Streamable HTTP endpoint. Use the catalog for OAuth. Connected app actions run through AI requests.')) +
      `<label for="ext-label">${tr('Название (латиницей)','Name (Latin letters)')}</label><input class="field" id="ext-label" maxlength="32" value="${esc(preset.label || '')}">
      <label for="ext-url">URL</label><input class="field" id="ext-url" type="url" maxlength="500" value="${esc(preset.url || '')}" placeholder="https://example.com/mcp">
      <label for="ext-token">${tr('Ключ Bearer, если требуется','Bearer key, if required')}</label><input class="field" id="ext-token" type="password" autocomplete="off" maxlength="8192">` + button('ext-add',tr('Сохранить подключение','Save connection')));
    $('ext-add').onclick=async e=>{
      const label=$('ext-label').value.trim().replace(/[ -]/g,'_');
      if((state?.mcp || []).some(r=>r.label===label) && !await ask(tr('Заменить адрес и ключ существующего подключения?','Replace this connection’s URL and key?')))return;
      const b=e.currentTarget;b.disabled=true;
      try{state=await api('/api/mcp',{label,url:$('ext-url').value,token:$('ext-token').value});renderAll();openConnections();toast(T.saved);}catch(error){fail(error);b.disabled=false;}
    };
  }
  document.addEventListener('secret-add-connector',e=>connectorForm(e.detail || {}));
  draw();
})();
