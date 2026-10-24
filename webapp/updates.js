/* Additional tools share the existing Mini App authentication and project scope. */
(() => {
  "use strict";
  const ru = () => lang === "ru";
  const label = (r,e) => ru() ? r : e;
  const call = (action,body={}) => api("/api/features/"+action,body);
  const fail = e => toast(e.detail || errorText(e));
  const button = (id,text) => '<button class="btn" id="'+id+'">'+esc(text)+'</button>';
  const field = (id,placeholder,type="text") => '<input class="field" id="'+id+'" type="'+type+'" placeholder="'+esc(placeholder)+'">';
  const head = text => '<div class="head"><h3>'+esc(text)+'</h3></div>';
  let documents = null;
  async function busy(id,work) {
    const node=$(id);if(node)node.disabled=true;
    try {await work();} catch(e){fail(e);} finally {if(node?.isConnected)node.disabled=false;}
  }
  function download(file) {
    const raw=atob(file.content),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));
    const url=URL.createObjectURL(new Blob([bytes],{type:"application/octet-stream"}));
    const a=document.createElement("a");a.href=url;a.download=file.name;document.body.append(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),60000);
  }
  async function encode(file) {
    if(!file || file.size>19000000)throw {detail:label("Файл должен быть не больше 19 МБ.","Maximum file size: 19 MB.")};
    const bytes=new Uint8Array(await file.arrayBuffer());let raw="";
    for(let i=0;i<bytes.length;i+=32768)raw+=String.fromCharCode(...bytes.subarray(i,i+32768));
    return btoa(raw);
  }
  async function openDocuments() {
    try {documents=await call("documents");renderDocuments();} catch(e){fail(e);}
  }
  function renderDocuments() {
    const d=documents;
    openSheet(head(label("Документы и проекты","Documents and projects"))+
      '<label>'+label("Текущий проект","Current project")+'<select class="field" id="fx-project">'+
      '<option value="0">'+label("Входящие","Inbox")+'</option>'+d.projects.map(p=>'<option value="'+p.id+'" '+(p.id===d.project?'selected':'')+'>'+esc(p.name)+'</option>').join("")+
      '</select></label>'+field("fx-project-name",label("Название нового проекта","New project name"))+
      button("fx-create",label("Создать проект","Create project"))+
      '<p class="small">'+label("PDF, DOCX, XLSX, PPTX, текст, код и архивы — до 19 МБ. Сканы без OCR.","PDF, DOCX, XLSX, PPTX, text, code and archives, up to 19 MB. No OCR for scans.")+'</p>'+
      '<input class="field" type="file" id="fx-upload" aria-label="'+label("Загрузить документ","Upload document")+'">'+
      '<div class="list">'+d.files.map(f=>'<button class="row press" data-fx-file="'+f.id+'"><div class="body"><div class="t">'+esc(f.name)+' · v'+f.version+(f.id===d.selected?' ✓':'')+'</div><div class="d">'+esc((f.warnings||[]).join(" ").slice(0,220))+'</div></div></button>').join("")+'</div>'+
      button("fx-report",label("Подготовить отчёт","Prepare a report")));
    $("fx-project").onchange=()=>busy("fx-create",async()=>{documents=await call("project-open",{id:Number($("fx-project").value)});renderDocuments();});
    $("fx-create").onclick=()=>busy("fx-create",async()=>{documents=await call("project-create",{name:$("fx-project-name").value});renderDocuments();});
    $("fx-upload").onchange=()=>busy("fx-upload",async()=>{
      const file=$("fx-upload").files[0];if(!file)return;
      toast(label("Загружаю и читаю документ…","Uploading and reading…"));
      documents=await call("upload",{name:file.name,content:await encode(file)});renderDocuments();
    });
    document.querySelectorAll("[data-fx-file]").forEach(b=>b.onclick=()=>openFile(Number(b.dataset.fxFile)));
    $("fx-report").onclick=()=>busy("fx-report",async()=>{
      toast(label("Готовлю отчёт…","Preparing report…"));
      const r=await call("report");
      openSheet(head(label("Отчёт","Report"))+'<div class="day-detail">'+esc(r.text)+'</div>'+button("fx-word","Word")+button("fx-md","Markdown"));
      $("fx-word").onclick=()=>download(r);
      $("fx-md").onclick=()=>download({name:"secret-ai-report.md",content:btoa(unescape(encodeURIComponent(r.text)))});
    });
  }
  async function openFile(id) {
    try {
      documents=await call("file-use",{id});
      const f=documents.files.find(f=>f.id===id);
      openSheet(head(f.name+' · v'+f.version)+'<p class="small">'+esc((f.warnings||[]).join("\n"))+'</p>'+
        button("fx-text",label("Скачать извлечённый текст","Download extracted text"))+
        (/\.(docx|xlsx)$/i.test(f.name)?button("fx-edit",label("Создать исправленную копию","Create an edited copy")):"")+
        button("fx-file-report",label("Отчёт по этому файлу","Report on this file"))+
        button("fx-delete",label("Удалить эту версию","Delete this version"))+button("fx-back",label("Все файлы","All files")));
      $("fx-text").onclick=()=>busy("fx-text",async()=>download(await call("file-text",{id})));
      $("fx-edit")?.addEventListener("click",()=>editFile(f));
      $("fx-file-report").onclick=()=>busy("fx-file-report",async()=>{
        const r=await call("report");openSheet(head(label("Отчёт","Report"))+'<div class="day-detail">'+esc(r.text)+'</div>'+button("fx-download","Word"));$("fx-download").onclick=()=>download(r);
      });
      $("fx-delete").onclick=()=>{if(window.confirm(label("Удалить эту версию файла?","Delete this file version?")))busy("fx-delete",async()=>{documents=await call("file-delete",{id,confirm:true});renderDocuments();});};
      $("fx-back").onclick=renderDocuments;
    } catch(e){fail(e);}
  }
  function editFile(f) {
    const word=/\.docx$/i.test(f.name);
    openSheet(head(label("Исправленная копия","Edited copy"))+'<p class="small">'+
      label("Оригинал сохраняется. Word: одна точная замена в основном тексте; вставка получает стиль начала фрагмента. Excel: изменение существующей ячейки со значением. Формулы не перезаписываются и не пересчитываются.",
            "Original preserved. Word: one exact replacement in body text; inserted text uses the starting run style. Excel: edit an existing value cell. Formulas are neither overwritten nor recalculated.")+'</p>'+
      (word?field("fx-find",label("Точный текст для замены","Exact text to replace"))+field("fx-replace",label("Новый текст (может быть пустым)","Replacement (may be empty)")):
       field("fx-sheet",label("Точное имя листа","Exact sheet name"))+field("fx-cell","B2")+field("fx-value",label("Новое значение","New value"))+
       '<select class="field" id="fx-value-type"><option value="text">'+label("Текст","Text")+'</option><option value="number">'+label("Число","Number")+'</option></select>')+
       button("fx-edit-save",label("Создать копию","Create copy")));
    $("fx-edit-save").onclick=()=>busy("fx-edit-save",async()=>{
      const changes=word?[{find:$("fx-find").value,replace:$("fx-replace").value}]:
        [{sheet:$("fx-sheet").value,cell:$("fx-cell").value,value:$("fx-value").value,type:$("fx-value-type").value}];
      const r=await call("edit-document",{id:f.id,changes});
      openSheet(head(label("Копия готова","Copy ready"))+'<div class="day-detail">'+esc(r.changes.join("\n"))+'</div>'+button("fx-edited-download",label("Скачать исправленную копию","Download edited copy")));
      $("fx-edited-download").onclick=()=>download(r);
    });
  }
  function history() {
    openSheet(head(label("Поиск по истории","Search history"))+field("fx-query",label("Что найти?","Search for…"))+
      button("fx-search",label("Найти","Search"))+'<div id="fx-results" class="day-detail" role="status"></div>');
    const search=()=>busy("fx-search",async()=>{
      const d=await call("history",{query:$("fx-query").value});
      $("fx-results").innerHTML='<p class="small">'+esc(d.scope)+'</p>'+d.results.map(r=>'<article><b>'+esc(r.role==="user"?label("Вы","You"):"Secret AI")+' · '+esc(new Date(r.created*1000).toLocaleString())+'</b><p style="white-space:pre-wrap">'+esc(r.text)+'</p></article>').join("")+(d.results.length?"":label("Совпадений нет.","No matches."));
    });
    $("fx-search").onclick=search;$("fx-query").onkeydown=e=>{if(e.key==="Enter")search();};
  }
  async function reminders() {
    try {
      const [d,t]=await Promise.all([call("reminders"),api("/api/day/state")]);
      openSheet(head(label("Напоминания","Reminders"))+'<p class="small">'+esc(d.timezone)+' · '+
        label("Примеры: через 20 минут; каждый понедельник в 10:00; по будням в 18:00; каждый день в 09:00.",
              "Examples: in 20 minutes; every monday 10:00; weekdays 18:00; daily 09:00.")+'</p>'+
        '<select class="field" id="fx-remind-task">'+t.tasks.filter(t=>t.status==="open").map(t=>'<option value="'+t.id+'">'+esc(t.title)+'</option>').join("")+'</select>'+
        field("fx-when",label("Когда напомнить?","When?"))+button("fx-remind",label("Добавить напоминание","Add reminder"))+
        '<div class="list">'+d.reminders.map(j=>'<div class="row"><div class="body">#'+j.id+' · '+esc(new Date(j.run_at*1000).toLocaleString(undefined,{timeZone:d.timezone}))+
          (j.recurrence?' ↻':'')+'</div><button class="text-button" data-fx-cancel="'+j.id+'">'+label("Отменить","Cancel")+'</button></div>').join("")+'</div>');
      $("fx-remind").onclick=()=>busy("fx-remind",async()=>{await call("remind",{id:Number($("fx-remind-task").value),when:$("fx-when").value});reminders();});
      document.querySelectorAll("[data-fx-cancel]").forEach(b=>b.onclick=async()=>{try{await call("cancel-reminder",{id:Number(b.dataset.fxCancel)});reminders();}catch(e){fail(e);}});
    } catch(e){fail(e);}
  }
  async function status() {
    try {
      const d=await call("status"),b=d.backup;
      openSheet(head(label("Состояние бота","Bot status"))+'<div class="day-detail">'+
        esc(label("Версия: ","Version: ")+d.version+"\n"+label("Свободно: ","Free space: ")+Math.round(d.free_bytes/1048576)+" MB\n"+
          label("Активные задачи: ","Active tasks: ")+d.active_tasks+"\n"+
          label("Модели: ","Models: ")+d.models.join(", ")+"\n"+
          label("Последняя успешная копия в этом процессе: ","Last successful backup in this process: ")+(b.last_success?new Date(b.last_success*1000).toLocaleString():"—")+"\n"+
          label("Ошибка копирования: ","Backup error: ")+(b.last_error||"—"))+'</div>'+
        '<p class="small">'+label("Провайдеры: наблюдения по реальным запросам после запуска. Задержка измеряется до открытия ответа; при потоковой выдаче это не время полного ответа. Отсутствие наблюдений не означает доступность.",
          "Providers: observations from actual requests since startup. Latency measures response opening, not full streaming duration. No observations does not imply availability.")+'</p>'+
        d.providers.map(p=>'<div class="day-detail">'+esc(p.model+" · "+p.latency_ms+" ms · "+p.requests+" requests · "+p.errors+" errors · "+(p.last_error||"OK"))+'</div>').join("")+
        (b.enabled?button("fx-backups",label("Резервные копии","Backups")):'<p>'+label("DATABASE_URL не настроен.","DATABASE_URL is not configured.")+'</p>'));
      $("fx-backups")?.addEventListener("click",backups);
    } catch(e){fail(e);}
  }
  async function backups() {
    try {
      const d=await call("backups");
      openSheet(head(label("Резервные копии","Backups"))+'<p class="small">'+label("Хранятся до 24 версий. Восстановление всей базы затронет всех пользователей и применяется при следующем запуске бота. Перед ним сохраняется текущее состояние.",
        "Up to 24 versions. Restoring the whole database affects every user and takes effect at the next bot startup. Current data is backed up first.")+'</p>'+
        button("fx-backup-now",label("Создать копию сейчас","Back up now"))+
        '<div class="list">'+d.versions.map(v=>'<div class="row"><div class="body">'+esc(new Date(v.created).toLocaleString())+' · '+Math.round(v.bytes/1024)+' KB</div><button class="text-button" data-fx-restore="'+v.id+'">'+label("Восстановить","Restore")+'</button></div>').join("")+'</div>');
      $("fx-backup-now").onclick=()=>busy("fx-backup-now",async()=>{await call("backup");backups();});
      document.querySelectorAll("[data-fx-restore]").forEach(b=>b.onclick=async()=>{
        if(!window.confirm(label("Восстановить всю базу из этой версии при следующем запуске? Изменения после выбранной копии будут отменены.",
                         "Restore the entire database from this version at next startup? Later changes will be rolled back.")))return;
        try{b.disabled=true;await call("restore",{id:Number(b.dataset.fxRestore),confirm:true});toast(label("Подготовлено. Перезапусти бота через панель хостинга.","Staged. Restart the bot from the hosting panel."));}catch(e){b.disabled=false;fail(e);}
      });
    } catch(e){fail(e);}
  }
  const tools=document.createElement("div");tools.className="day-toolbar";tools.style.marginBottom="20px";
  [["fx-documents",label("Документы","Documents"),openDocuments],
   ["fx-history",label("Поиск в истории","Search history"),history],
   ["fx-reminders",label("Напоминания","Reminders"),reminders],
   ["fx-status",label("Состояние","Status"),status]].forEach(([id,text,action])=>{
     const b=document.createElement("button");b.id=id;b.textContent=text;b.onclick=action;tools.append(b);
   });
  $("view-agent").prepend(tools);
  const ownerTimer=setInterval(()=>{if(state){$("fx-status").hidden=!state.gen?.owner;clearInterval(ownerTimer);}},500);
})();
