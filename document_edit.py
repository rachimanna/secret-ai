"""Create edited Office copies without executing macros, formulas or embedded content."""
import io
import math
import re
import zipfile
from xml.etree import ElementTree as ET
import docreader

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def edited(raw, name, changes):
    if not isinstance(changes,list) or not 1 <= len(changes) <= 20:
        raise ValueError("От 1 до 20 правок.")
    updates, log = {}, []
    with docreader.checked_zip(io.BytesIO(raw)) as archive:
        if name.lower().endswith(".docx"):
            root = docreader.xml(docreader.member(archive,"word/document.xml"))
            for change in changes:
                find, replacement = change.get("find"), change.get("replace")
                if not isinstance(find,str) or not find or not isinstance(replacement,str) or len(find)+len(replacement)>12000:
                    raise ValueError("Укажи текст поиска и замены.")
                paragraphs = [(p,list(p.iter(W+"t"))) for p in root.iter(W+"p")]
                matches = [(nodes,"".join(n.text or "" for n in nodes)) for _,nodes in paragraphs
                           if find in "".join(n.text or "" for n in nodes)]
                if sum(text.count(find) for _,text in matches) != 1:
                    raise ValueError("Фрагмент должен встречаться ровно один раз в основном тексте Word.")
                nodes,text=matches[0]
                start,end=text.index(find),text.index(find)+len(find)
                offset,inserted=0,False
                for node in nodes:
                    value=node.text or ""
                    a,b=offset,offset+len(value)
                    offset=b
                    if b<=start or a>=end:
                        continue
                    left=value[:max(0,start-a)]
                    right=value[max(0,end-a):] if end<b else ""
                    node.text=left+(replacement if not inserted else "")+right
                    node.set("{http://www.w3.org/XML/1998/namespace}space","preserve")
                    inserted=True
                log.append("Word: "+find[:150]+" → "+replacement[:150])
            root.attrib.pop("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable",None)
            updates["word/document.xml"]=ET.tostring(root,encoding="utf-8",xml_declaration=True)
        elif name.lower().endswith(".xlsx"):
            book=docreader.xml(docreader.member(archive,"xl/workbook.xml"))
            rels=docreader.xml(docreader.member(archive,"xl/_rels/workbook.xml.rels"))
            targets={r.get("Id"):r.get("Target") for r in rels if r.get("TargetMode")!="External"}
            sheets={}
            for sheet in book.iter(S+"sheet"):
                target=targets.get(sheet.get(R+"id"),"")
                target=target.lstrip("/") if target.startswith("/") else "xl/"+target
                if target.startswith("xl/worksheets/") and ".." not in target.split("/"):
                    sheets[sheet.get("name")]=target
            roots={}
            for change in changes:
                sheet,cell=change.get("sheet"),str(change.get("cell") or "").upper()
                if sheet not in sheets or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}",cell):
                    raise ValueError("Нужны точное имя листа и адрес ячейки, например B2.")
                path=sheets[sheet]
                if path not in roots:
                    roots[path]=docreader.xml(docreader.member(archive,path))
                root=roots[path]
                node=next((c for c in root.iter(S+"c") if c.get("r")==cell),None)
                if node is None:
                    raise ValueError("В этой версии можно изменять только существующие ячейки.")
                if node.find(S+"f") is not None:
                    raise ValueError("Ячейки с формулами не перезаписываются. Выбери ячейку со значением.")
                value=change.get("value")
                if not isinstance(value,(str,int,float)) or isinstance(value,bool) or len(str(value))>12000:
                    raise ValueError("Недопустимое значение ячейки.")
                for child in list(node):
                    if child.tag in (S+"v",S+"is"):
                        node.remove(child)
                if change.get("type")=="number":
                    number=float(value)
                    if not math.isfinite(number):
                        raise ValueError("Недопустимое число.")
                    node.attrib.pop("t",None)
                    ET.SubElement(node,S+"v").text=str(value)
                else:
                    node.set("t","inlineStr")
                    t=ET.SubElement(ET.SubElement(node,S+"is"),S+"t")
                    t.text=str(value)
                    t.set("{http://www.w3.org/XML/1998/namespace}space","preserve")
                log.append(str(sheet)+"!"+cell+" → "+str(value)[:150])
            for root in roots.values():
                root.attrib.pop("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable",None)
            updates.update({p:ET.tostring(r,encoding="utf-8",xml_declaration=True) for p,r in roots.items()})
        else:
            raise ValueError("Редактируются только DOCX и XLSX.")
        out=io.BytesIO()
        with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as result:
            for item in archive.infolist():
                result.writestr(item,updates.get(item.filename,archive.read(item)))
    return out.getvalue(),log
