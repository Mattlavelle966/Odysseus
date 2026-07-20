"""Authenticated AgentIDE project/session API and loopback preview proxy."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import re
from typing import AsyncIterator, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from src import agent_ide
from src.auth_helpers import require_user
from src.tool_security import owner_is_admin_or_single_user

_MAX_PROXY_REQUEST_BYTES = 10 * 1024 * 1024
_MAX_PROXY_RESPONSE_BYTES = 40 * 1024 * 1024
_SERVICE_PATH_RE = re.compile(r"^__agentide_port/(\d+)(?:/(.*))?$")
_COOKIE_JARS: dict[tuple[str, int], httpx.Cookies] = {}
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_FORWARDED_REQUEST_HEADERS = {
    "accept",
    "accept-encoding",
    "accept-language",
    "content-type",
    "if-modified-since",
    "if-none-match",
    "range",
    "user-agent",
}
_FORWARDED_RESPONSE_HEADERS = {
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-language",
    "content-range",
    "content-type",
    "etag",
    "expires",
    "last-modified",
}


class AgentIDEConnect(BaseModel):
    workspace_path: str = Field(min_length=1, max_length=4096)
    port: Optional[int] = None
    service_ports: list[int] = Field(default_factory=list, max_length=16)
    entry_path: str = Field(default="/", max_length=2048)
    chat_session_id: Optional[str] = Field(default=None, max_length=128)
    name: Optional[str] = Field(default=None, max_length=160)
    start_static: bool = False
    agent_ide_id: Optional[str] = Field(default=None, max_length=128)


def _require_agent_ide_owner(request: Request) -> str:
    owner = require_user(request)
    if not owner_is_admin_or_single_user(owner):
        raise HTTPException(status_code=403, detail="AgentIDE is admin-only")
    return owner


def _proxy_prefix(agent_ide_id: str, token: Optional[str] = None) -> str:
    if token:
        return f"/api/agentide/{agent_ide_id}/preview-token/{token}/"
    return f"/api/agentide/{agent_ide_id}/preview/"


def _resolve_proxy_target(record, path: str) -> tuple[int, str]:
    """Resolve a proxy path to one explicitly registered loopback service."""
    match = _SERVICE_PATH_RE.match(path)
    if not match:
        return agent_ide.validate_port(record.port), path
    port = agent_ide.validate_port(match.group(1))
    if not agent_ide.is_service_port_allowed(record, port):
        raise HTTPException(
            status_code=403,
            detail=f"Port {port} is not registered for this AgentIDE project",
        )
    return port, match.group(2) or ""


def _service_proxy_path(prefix: str, port: int, primary_port: int) -> str:
    if port == primary_port:
        return prefix
    return f"{prefix}__agentide_port/{port}/"


def _cookie_jar(agent_ide_id: str, port: int) -> httpx.Cookies:
    return _COOKIE_JARS.setdefault((agent_ide_id, port), httpx.Cookies())


def _cookie_request(method: str, port: int, path_and_query: str) -> httpx.Request:
    """Use localhost cookie semantics while transport stays pinned to 127.0.0.1."""
    return httpx.Request(
        method,
        f"http://localhost:{port}/" + path_and_query.lstrip("/"),
    )


def _capture_project_cookies(
    jar: httpx.Cookies,
    upstream: httpx.Response,
    cookie_request: httpx.Request,
) -> None:
    """Store upstream cookies, normalizing explicit loopback domains.

    Python's standard cookie jar stores ``Domain=localhost`` but never sends it
    back. Browsers treat localhost as the development host, so convert only
    explicit loopback domains to host-only cookies before extraction.
    """
    set_cookies = [
        re.sub(
            r";\s*domain=(?:\.?localhost|127\.0\.0\.1|\[::1\])(?=\s*;|$)",
            "",
            value,
            flags=re.I,
        )
        for value in upstream.headers.get_list("set-cookie")
    ]
    if set_cookies:
        jar.extract_cookies(httpx.Response(
            upstream.status_code,
            headers=[("set-cookie", value) for value in set_cookies],
            request=cookie_request,
        ))


def clear_preview_state(agent_ide_id: str) -> None:
    for key in [key for key in _COOKIE_JARS if key[0] == agent_ide_id]:
        _COOKIE_JARS.pop(key, None)


def _forwardable_request_header(name: str) -> bool:
    lowered = name.strip().lower()
    return bool(lowered) and (
        lowered not in _HOP_BY_HOP
        and lowered not in {
            "cookie",
            "host",
            "origin",
            "referer",
            "content-length",
        }
        and not lowered.startswith(("sec-", "proxy-", "x-forwarded-"))
    )


def _rewrite_root_references(text: str, prefix: str, content_type: str) -> str:
    """Rewrite root URLs so an internal site remains under its proxy mount."""
    if "text/html" in content_type:
        # HTML attributes. Protocol-relative URLs (//cdn...) remain external.
        text = re.sub(
            r"(?i)(\b(?:src|href|action|poster)\s*=\s*[\"'])/(?!/)",
            lambda match: match.group(1) + prefix,
            text,
        )
        text = re.sub(
            r"(?i)(\bsrcset\s*=\s*[\"'][^\"']*?)(?<![\w:])/(?!/)",
            lambda match: match.group(1) + prefix,
            text,
        )
        base = f'<base href="{prefix}">'
        if re.search(r"(?i)<head(?:\s[^>]*)?>", text):
            text = re.sub(
                r"(?i)(<head(?:\s[^>]*)?>)",
                lambda match: match.group(1) + base,
                text,
                count=1,
            )
        else:
            text = base + text
    if "text/css" in content_type or "text/html" in content_type:
        text = re.sub(
            r"(?i)(url\(\s*[\"']?)/(?!/)",
            lambda match: match.group(1) + prefix,
            text,
        )
    if (
        "javascript" in content_type
        or "ecmascript" in content_type
        or "text/html" in content_type
    ):
        # Vite and browser modules commonly import from /src or /@vite.
        text = re.sub(
            r"(?m)(\b(?:from|import)\s*(?:\(\s*)?[\"'])/(?!/)",
            lambda match: match.group(1) + prefix,
            text,
        )
    return text


def _preview_bootstrap(
    prefix: str,
    *,
    primary_port: Optional[int] = None,
    service_ports: Optional[list[int]] = None,
) -> str:
    """Keep requests proxied and expose a bounded, read-only element picker.

    The preview remains in a sandboxed opaque-origin iframe. The picker only
    reports DOM metadata to its parent after an explicit, request-scoped
    activation message; it cannot call application APIs or execute tools.
    """
    quoted = json.dumps(prefix)
    primary = int(primary_port or 0)
    allowed = [primary, *(service_ports or [])] if primary else []
    port_prefixes = {
        str(port): _service_proxy_path(prefix, port, primary)
        for port in allowed
    }
    template = r"""<script>(function(){
const p=__AGENTIDE_PREFIX__;
const primaryPort=__AGENTIDE_PRIMARY_PORT__;
const portPrefixes=__AGENTIDE_PORT_PREFIXES__;
const page=new URL(location.href);
const loopback=h=>h==='localhost'||h==='127.0.0.1'||h==='[::1]';
const cleanDiagnostic=(v,n=500)=>String(v==null?'':v).split(p).join('/').replace(/\s+/g,' ').trim().slice(0,n);
const report=(kind,details={})=>{
  const safe={};
  for(const [key,value] of Object.entries(details).slice(0,12))safe[cleanDiagnostic(key,60)]=cleanDiagnostic(value,500);
  parent.postMessage({type:'odysseus-agentide-diagnostic',kind:cleanDiagnostic(kind,60),details:safe,time:Date.now()},'*');
};
const portFor=q=>Number(q.port||(q.protocol==='https:'||q.protocol==='wss:'?443:80));
const u=(x,websocket=false)=>{
  if(x instanceof URL)x=x.href;
  if(typeof x!=='string')return x;
  if(websocket&&x.startsWith(p)){
    const proxied=new URL(x,location.href);
    proxied.protocol=location.protocol==='https:'?'wss:':'ws:';
    return proxied.href;
  }
  if(x.startsWith('/')&&!x.startsWith('//')&&!x.startsWith(p)){
    const relative=p+x.slice(1);
    if(!websocket)return relative;
    const proxied=new URL(relative,location.href);
    proxied.protocol=location.protocol==='https:'?'wss:':'ws:';
    return proxied.href;
  }
  try{
    const q=new URL(x,location.href);
    const network=['http:','https:','ws:','wss:'].includes(q.protocol);
    const local=network&&loopback(q.hostname);
    const sameOrigin=q.host===page.host&&network;
    if((local||sameOrigin)&&q.pathname.startsWith('/')&&!q.pathname.startsWith(p)){
      let mount=p;
      if(local){
        const port=portFor(q);
        mount=portPrefixes[String(port)];
        if(!mount){
          report('unregistered-port',{url:x,port,help:'Add this port to Services in AgentIDE'});
          return x;
        }
      }
      const relative=mount+q.pathname.slice(1)+q.search+q.hash;
      if(websocket){
        const proxied=new URL(relative,location.href);
        proxied.protocol=location.protocol==='https:'?'wss:':'ws:';
        return proxied.href;
      }
      return relative;
    }
  }catch(_){}
  return x;
};
const f=window.fetch;window.fetch=function(x,o){
  if(typeof x==='string')x=u(x);
  else if(x instanceof Request){
    const y=u(x.url);if(y!==x.url)x=new Request(y,x);
  }
  const url=typeof x==='string'?x:x.url;
  const started=performance.now();
  return f.call(this,x,o).then(response=>{
    report(response.ok?'network':'http-error',{method:o&&o.method||x&&x.method||'GET',url,status:response.status,statusText:response.statusText,duration:Math.round(performance.now()-started)+'ms'});
    return response;
  }).catch(error=>{report('fetch-error',{url,error:error&&error.message||error});throw error;});
};
const xo=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(m,x){
  arguments[1]=u(x);this.__agentideMethod=m;this.__agentideURL=arguments[1];this.__agentideStarted=performance.now();
  this.addEventListener('load',()=>report(this.status>=400?'http-error':'network',{method:m,url:this.__agentideURL,status:this.status,statusText:this.statusText,duration:Math.round(performance.now()-this.__agentideStarted)+'ms'}));
  this.addEventListener('error',()=>report('xhr-error',{method:m,url:this.__agentideURL}));
  return xo.apply(this,arguments);
};
if(window.EventSource){
  const E=window.EventSource;
  const AE=function(x,o){const source=new E(u(x),o);source.addEventListener('error',()=>report('eventsource-error',{url:x}));return source;};
  AE.prototype=E.prototype;Object.setPrototypeOf(AE,E);window.EventSource=AE;
}
if(window.WebSocket){
  const W=window.WebSocket;
  const AW=function(x,protocols){
    const target=u(x,true);
    const socket=protocols===undefined?new W(target):new W(target,protocols);
    socket.addEventListener('error',()=>report('websocket-error',{url:x,target}));
    return socket;
  };
  AW.prototype=W.prototype;Object.setPrototypeOf(AW,W);window.WebSocket=AW;
}
const nativeOpen=window.open;
window.open=function(url,...args){return nativeOpen.call(this,u(url),...args);};
const rewriteElement=element=>{
  if(!(element instanceof Element))return;
  for(const name of ['src','href','action','poster']){
    if(element.hasAttribute(name)){const before=element.getAttribute(name),after=u(before);if(after!==before)element.setAttribute(name,after);}
  }
};
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('[src],[href],[action],[poster]').forEach(rewriteElement);
  new MutationObserver(records=>records.forEach(record=>{
    if(record.type==='attributes')rewriteElement(record.target);
    record.addedNodes.forEach(node=>{rewriteElement(node);node.querySelectorAll&&node.querySelectorAll('[src],[href],[action],[poster]').forEach(rewriteElement);});
  })).observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['src','href','action','poster']});
});
addEventListener('error',event=>report('runtime-error',{message:event.message,source:event.filename,line:event.lineno,column:event.colno}),true);
addEventListener('unhandledrejection',event=>report('unhandled-rejection',{reason:event.reason&&event.reason.message||event.reason}),true);
for(const level of ['warn','error']){
  const original=console[level];
  console[level]=function(...args){report('console-'+level,{message:args.map(value=>typeof value==='string'?value:(value&&value.message)||String(value)).join(' ')});return original.apply(this,args);};
}
report('preview-ready',{page:location.pathname});

let pickRequest='',pickTarget=null,pickBox=null,pickLabel=null;
const clean=(v,n=240)=>String(v==null?'':v).replace(/\s+/g,' ').trim().slice(0,n);
const cssEscape=v=>window.CSS&&CSS.escape?CSS.escape(String(v)):String(v).replace(/[^A-Za-z0-9_-]/g,c=>'\\'+c);
const unique=s=>{try{return document.querySelectorAll(s).length===1;}catch(_){return false;}};
function selectorFor(el){
  if(!(el instanceof Element))return '';
  if(el.id){const s='#'+cssEscape(el.id);if(unique(s))return s;}
  for(const key of ['data-testid','data-test','data-cy']){
    const value=el.getAttribute(key);
    if(value){const s=el.tagName.toLowerCase()+'['+key+'="'+cssEscape(value)+'"]';if(unique(s))return s;}
  }
  const parts=[];let node=el;
  while(node&&node.nodeType===1&&parts.length<7){
    let part=node.tagName.toLowerCase();
    if(node.id){part+='#'+cssEscape(node.id);parts.unshift(part);break;}
    const classes=Array.from(node.classList||[]).filter(Boolean).slice(0,3);
    if(classes.length)part+='.'+classes.map(cssEscape).join('.');
    const parentNode=node.parentElement;
    if(parentNode){
      const same=Array.from(parentNode.children).filter(child=>child.tagName===node.tagName);
      if(same.length>1)part+=':nth-of-type('+(same.indexOf(node)+1)+')';
    }
    parts.unshift(part);node=parentNode;
    const joined=parts.join(' > ');if(unique(joined))return joined;
  }
  return parts.join(' > ');
}
function componentFor(el){
  const explicit=el.getAttribute('data-component')||el.getAttribute('data-component-name');
  if(explicit)return clean(explicit,160);
  const vue=el.__vueParentComponent;
  if(vue&&vue.type)return clean(vue.type.name||vue.type.__name||'',160);
  const key=Object.keys(el).find(k=>k.startsWith('__reactFiber$')||k.startsWith('__reactInternalInstance$'));
  let fiber=key?el[key]:null;const names=[];
  for(let i=0;fiber&&i<20;i++,fiber=fiber.return){
    const type=fiber.elementType||fiber.type;
    const name=typeof type==='function'?(type.displayName||type.name):(type&&typeof type==='object'?(type.displayName||type.name):'');
    if(name&&name!==el.tagName.toLowerCase()&&!names.includes(name))names.unshift(clean(name,80));
  }
  return names.slice(-4).join(' > ');
}
function sourceFor(el){
  const attr=el.getAttribute('data-source')||el.getAttribute('data-file');
  if(attr)return clean(attr,300);
  const key=Object.keys(el).find(k=>k.startsWith('__reactFiber$')||k.startsWith('__reactInternalInstance$'));
  let fiber=key?el[key]:null;
  for(let i=0;fiber&&i<20;i++,fiber=fiber.return){
    const src=fiber._debugSource;
    if(src&&src.fileName)return clean(src.fileName+(src.lineNumber?':'+src.lineNumber:'')+(src.columnNumber?':'+src.columnNumber:''),300);
  }
  return '';
}
function pagePath(){
  let path=location.pathname;
  if(path.startsWith(p))path='/'+path.slice(p.length);
  return clean(path+location.search+location.hash,1200)||'/';
}
function safeAttribute(value){
  return clean(String(value==null?'':value).split(p).join('/'),300);
}
function detailsFor(el){
  const attrs={};
  for(const name of ['id','role','aria-label','title','name','type','href','alt','data-testid','data-test','data-cy','data-component','data-source','data-file']){
    if(el.hasAttribute(name))attrs[name]=safeAttribute(el.getAttribute(name));
  }
  const rect=el.getBoundingClientRect(),style=getComputedStyle(el),clone=el.cloneNode(false);
  for(const attr of Array.from(clone.attributes||[])){
    if(/^on/i.test(attr.name)||['value','srcdoc','nonce','integrity'].includes(attr.name.toLowerCase()))clone.removeAttribute(attr.name);
    else if(['href','src','action','poster'].includes(attr.name.toLowerCase()))clone.setAttribute(attr.name,safeAttribute(attr.value));
  }
  return {
    tag:clean(el.tagName.toLowerCase(),40),
    selector:clean(selectorFor(el),1000),
    component:componentFor(el),
    source:sourceFor(el),
    page:pagePath(),
    text:clean(el.innerText||el.textContent,500),
    classes:Array.from(el.classList||[]).map(v=>clean(v,100)).filter(Boolean).slice(0,12),
    attributes:attrs,
    opening_html:clean(clone.outerHTML.split(p).join('/'),1200),
    bounds:{x:Math.round(rect.x),y:Math.round(rect.y),width:Math.round(rect.width),height:Math.round(rect.height)},
    styles:{display:style.display,position:style.position,color:style.color,backgroundColor:style.backgroundColor,fontSize:style.fontSize,fontWeight:style.fontWeight}
  };
}
function ensurePicker(){
  if(pickBox)return;
  pickBox=document.createElement('div');pickBox.setAttribute('data-odysseus-element-picker','box');
  Object.assign(pickBox.style,{position:'fixed',zIndex:'2147483646',pointerEvents:'none',border:'2px solid #7c5cff',background:'rgba(124,92,255,.10)',boxSizing:'border-box',display:'none'});
  pickLabel=document.createElement('div');pickLabel.setAttribute('data-odysseus-element-picker','label');
  Object.assign(pickLabel.style,{position:'fixed',zIndex:'2147483647',pointerEvents:'none',padding:'4px 7px',borderRadius:'4px',background:'#17151f',color:'#fff',font:'11px/1.35 ui-monospace,monospace',maxWidth:'70vw',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',display:'none'});
  document.documentElement.append(pickBox,pickLabel);
}
function paint(el){
  if(!(el instanceof Element)||el.closest('[data-odysseus-element-picker]'))return;
  ensurePicker();pickTarget=el;const r=el.getBoundingClientRect();
  Object.assign(pickBox.style,{display:'block',left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px'});
  pickLabel.textContent=(componentFor(el)||el.tagName.toLowerCase())+' · '+selectorFor(el);
  Object.assign(pickLabel.style,{display:'block',left:Math.max(4,r.left)+'px',top:Math.max(4,r.top-25)+'px'});
}
function clearPicker(){
  pickRequest='';pickTarget=null;
  if(pickBox)pickBox.style.display='none';
  if(pickLabel)pickLabel.style.display='none';
  document.documentElement.style.cursor='';
}
function onMove(event){if(pickRequest)paint(event.target);}
function onClick(event){
  if(!pickRequest||!(event.target instanceof Element))return;
  event.preventDefault();event.stopPropagation();event.stopImmediatePropagation();
  const requestId=pickRequest,element=detailsFor(event.target);clearPicker();
  parent.postMessage({type:'odysseus-agentide-element-selected',requestId,element},'*');
}
function onKey(event){
  if(pickRequest&&event.key==='Escape'){
    event.preventDefault();const requestId=pickRequest;clearPicker();
    parent.postMessage({type:'odysseus-agentide-element-cancelled',requestId},'*');
  }
}
addEventListener('message',event=>{
  const data=event.data||{};
  if(event.source!==parent||data.type!=='odysseus-agentide-element-select')return;
  if(data.action==='cancel'){clearPicker();return;}
  if(data.action==='start'&&typeof data.requestId==='string'&&data.requestId.length<=100){
    pickRequest=data.requestId;ensurePicker();document.documentElement.style.cursor='crosshair';
  }
});
document.addEventListener('pointermove',onMove,true);
document.addEventListener('click',onClick,true);
document.addEventListener('keydown',onKey,true);
addEventListener('scroll',()=>{if(pickRequest&&pickTarget)paint(pickTarget);},true);
})();</script>"""
    return (
        template
        .replace("__AGENTIDE_PREFIX__", quoted)
        .replace("__AGENTIDE_PRIMARY_PORT__", str(primary))
        .replace("__AGENTIDE_PORT_PREFIXES__", json.dumps(port_prefixes))
    )


def _rewrite_loopback_references(
    text: str,
    *,
    prefix: str,
    primary_port: int,
    service_ports: list[int],
) -> str:
    """Translate literal loopback URLs while leaving unregistered ports alone."""
    allowed = {primary_port, *service_ports}
    pattern = re.compile(
        r"(?P<scheme>https?|wss?)://"
        r"(?P<host>localhost|127\.0\.0\.1|\[::1\])"
        r"(?::(?P<port>\d+))?(?P<path>/[^\s\"'<>)]*)?",
        re.I,
    )

    def replace(match: re.Match) -> str:
        scheme = match.group("scheme").lower()
        default_port = 443 if scheme in {"https", "wss"} else 80
        port = int(match.group("port") or default_port)
        if port not in allowed:
            return match.group(0)
        mount = _service_proxy_path(prefix, port, primary_port)
        path = (match.group("path") or "/").lstrip("/")
        # Browser bootstrap converts this same-origin HTTP URL back to ws(s)
        # when it is passed to the WebSocket constructor.
        return mount + path

    return pattern.sub(replace, text)


def _rewrite_preview_body(
    body: bytes,
    *,
    content_type: str,
    agent_ide_id: str,
    token: Optional[str] = None,
    primary_port: Optional[int] = None,
    service_ports: Optional[list[int]] = None,
) -> bytes:
    textual = any(
        marker in content_type.lower()
        for marker in ("text/html", "text/css", "javascript", "ecmascript")
    )
    if not textual:
        return body
    charset = "utf-8"
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.I)
    if match:
        charset = match.group(1)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    prefix = _proxy_prefix(agent_ide_id, token)
    services = service_ports or []
    rewritten = _rewrite_root_references(
        text,
        prefix,
        content_type.lower(),
    )
    if primary_port:
        rewritten = _rewrite_loopback_references(
            rewritten,
            prefix=prefix,
            primary_port=primary_port,
            service_ports=services,
        )
    if "text/html" in content_type.lower():
        bootstrap = _preview_bootstrap(
            prefix,
            primary_port=primary_port,
            service_ports=services,
        )
        if re.search(r"(?i)<head(?:\s[^>]*)?>", rewritten):
            rewritten = re.sub(
                r"(?i)(<head(?:\s[^>]*)?>)",
                lambda match: match.group(1) + bootstrap,
                rewritten,
                count=1,
            )
        else:
            rewritten = bootstrap + rewritten
    return rewritten.encode("utf-8")


async def _proxy(
    request: Request,
    agent_ide_id: str,
    path: str,
    token: Optional[str] = None,
) -> Response:
    if token:
        record = agent_ide.get_session_unscoped(agent_ide_id)
        if record is None or not agent_ide.valid_preview_token(record, token):
            raise HTTPException(status_code=404, detail="AgentIDE preview not found")
    else:
        owner = _require_agent_ide_owner(request)
        record = agent_ide.get_session(owner, agent_ide_id)
    if record is None:
        raise HTTPException(status_code=404, detail="AgentIDE session not found")
    if "\x00" in path or "\\" in path:
        raise HTTPException(status_code=400, detail="Invalid preview path")
    try:
        port, target_path = _resolve_proxy_target(record, path)
        service_ports = agent_ide.service_ports_for(record)[1:]
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if token and request.method == "OPTIONS":
        requested_headers = request.headers.get("access-control-request-headers", "")
        allowed_headers = ", ".join(
            item.strip()
            for item in requested_headers.split(",")
            if _forwardable_request_header(item)
        )
        return Response(
            status_code=204,
            headers={
                "access-control-allow-origin": "null",
                "access-control-allow-methods": "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS",
                "access-control-allow-headers": allowed_headers or "Content-Type",
                "access-control-max-age": "600",
            },
        )

    raw_body = await request.body()
    if len(raw_body) > _MAX_PROXY_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Preview request is too large")

    upstream_path = "/" + target_path.lstrip("/")
    query = request.url.query
    target = f"http://127.0.0.1:{port}{upstream_path}"
    if query:
        target += "?" + query
    headers = {
        key: value
        for key, value in request.headers.items()
        if _forwardable_request_header(key)
    }
    headers["host"] = f"localhost:{port}"
    headers["origin"] = f"http://localhost:{port}"
    headers["referer"] = f"http://localhost:{port}{upstream_path}"

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=None, write=20.0, pool=5.0),
        follow_redirects=False,
    )
    try:
        upstream_request = client.build_request(
            request.method,
            target,
            headers=headers,
            content=raw_body or None,
        )
        jar = _cookie_jar(agent_ide_id, port)
        cookie_request = _cookie_request(
            request.method,
            port,
            upstream_path + (("?" + query) if query else ""),
        )
        jar.set_cookie_header(cookie_request)
        if cookie_request.headers.get("cookie"):
            upstream_request.headers["cookie"] = cookie_request.headers["cookie"]
        upstream = await client.send(upstream_request, stream=True)
        _capture_project_cookies(jar, upstream, cookie_request)

        content_type = upstream.headers.get(
            "content-type",
            "application/octet-stream",
        )
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in _FORWARDED_RESPONSE_HEADERS
            and key.lower() not in {"content-length", "content-encoding"}
        }
        location = upstream.headers.get("location")
        if location:
            if location.startswith("/") and not location.startswith("//"):
                response_headers["location"] = (
                    _service_proxy_path(
                        _proxy_prefix(agent_ide_id, token),
                        port,
                        agent_ide.validate_port(record.port),
                    )
                    + location.lstrip("/")
                )
            else:
                response_headers["location"] = _rewrite_loopback_references(
                    location,
                    prefix=_proxy_prefix(agent_ide_id, token),
                    primary_port=agent_ide.validate_port(record.port),
                    service_ports=service_ports,
                )
        response_headers["x-agentide-preview"] = agent_ide_id
        response_headers["x-agentide-upstream-port"] = str(port)
        if token:
            response_headers["access-control-allow-origin"] = "null"
            response_headers["access-control-expose-headers"] = (
                "Content-Type, Content-Range, X-AgentIDE-Preview, "
                "X-AgentIDE-Upstream-Port"
            )

        textual = any(
            marker in content_type.lower()
            for marker in ("text/html", "text/css", "javascript", "ecmascript")
        )
        if request.method == "HEAD":
            await upstream.aclose()
            await client.aclose()
            return Response(
                status_code=upstream.status_code,
                headers=response_headers,
            )
        if textual:
            chunks = []
            size = 0
            async for chunk in upstream.aiter_bytes():
                size += len(chunk)
                if size > _MAX_PROXY_RESPONSE_BYTES:
                    raise HTTPException(
                        status_code=502,
                        detail="Preview response exceeded 40 MB",
                    )
                chunks.append(chunk)
            await upstream.aclose()
            await client.aclose()
            body = _rewrite_preview_body(
                b"".join(chunks),
                content_type=content_type,
                agent_ide_id=agent_ide_id,
                token=token,
                primary_port=agent_ide.validate_port(record.port),
                service_ports=service_ports,
            )
            if "text/html" in content_type.lower():
                response_headers["cache-control"] = "no-store"
            return Response(
                content=body,
                status_code=upstream.status_code,
                headers=response_headers,
                media_type=None,
            )

        async def stream_body() -> AsyncIterator[bytes]:
            size = 0
            try:
                async for chunk in upstream.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_PROXY_RESPONSE_BYTES:
                        break
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        if "text/event-stream" in content_type.lower():
            response_headers["cache-control"] = "no-cache"
            response_headers["x-accel-buffering"] = "no"
        return StreamingResponse(
            stream_body(),
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=None,
        )
    except HTTPException:
        await client.aclose()
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Nothing is reachable on AgentIDE port {port}",
        ) from exc
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"AgentIDE preview failed: {type(exc).__name__}",
        ) from exc


async def _websocket_proxy(
    websocket: WebSocket,
    agent_ide_id: str,
    token: str,
    path: str,
) -> None:
    """Relay a capability-scoped browser socket to a registered dev service."""
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.exceptions import ConnectionClosed, WebSocketException

    record = agent_ide.get_session_unscoped(agent_ide_id)
    if record is None or not agent_ide.valid_preview_token(record, token):
        await websocket.close(code=4404, reason="AgentIDE preview not found")
        return
    if "\x00" in path or "\\" in path:
        await websocket.close(code=4400, reason="Invalid preview path")
        return
    try:
        port, target_path = _resolve_proxy_target(record, path)
    except (ValueError, HTTPException):
        await websocket.close(code=4403, reason="Unregistered AgentIDE service")
        return

    query = websocket.url.query
    target = f"ws://127.0.0.1:{port}/" + target_path.lstrip("/")
    if query:
        target += "?" + query
    requested_protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    cookie_request = _cookie_request("GET", port, target_path)
    _cookie_jar(agent_ide_id, port).set_cookie_header(cookie_request)
    additional_headers = {}
    if cookie_request.headers.get("cookie"):
        additional_headers["cookie"] = cookie_request.headers["cookie"]

    try:
        async with websocket_connect(
            target,
            origin=f"http://localhost:{port}",
            subprotocols=requested_protocols or None,
            additional_headers=additional_headers or None,
            proxy=None,
            max_size=_MAX_PROXY_RESPONSE_BYTES,
            open_timeout=3,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def browser_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close(code=message.get("code", 1000))
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def upstream_to_browser() -> None:
                async for message in upstream:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

            first, second = (
                asyncio.create_task(browser_to_upstream()),
                asyncio.create_task(upstream_to_browser()),
            )
            done, pending = await asyncio.wait(
                {first, second},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                with suppress(
                    WebSocketDisconnect,
                    ConnectionClosed,
                    asyncio.CancelledError,
                ):
                    await task
    except (OSError, TimeoutError, ConnectionClosed, WebSocketException):
        with suppress(RuntimeError):
            await websocket.close(code=1013, reason=f"Port {port} is unavailable")


def setup_agent_ide_routes():
    router = APIRouter(prefix="/api/agentide", tags=["agentide"])

    @router.get("/sessions")
    def list_agent_ide_sessions(request: Request):
        owner = _require_agent_ide_owner(request)
        return {"sessions": agent_ide.list_sessions(owner)}

    @router.post("/sessions")
    def connect_agent_ide(request: Request, payload: AgentIDEConnect):
        owner = _require_agent_ide_owner(request)
        try:
            if payload.start_static:
                return agent_ide.start_static_session(
                    owner=owner,
                    workspace_path=payload.workspace_path,
                    port=payload.port,
                    entry_path=payload.entry_path,
                    chat_session_id=payload.chat_session_id,
                    name=payload.name,
                    agent_ide_id=payload.agent_ide_id,
                    service_ports=payload.service_ports,
                )
            if payload.port is None:
                raise ValueError("Port is required when connecting to a server")
            return agent_ide.connect_session(
                owner=owner,
                workspace_path=payload.workspace_path,
                port=payload.port,
                entry_path=payload.entry_path,
                chat_session_id=payload.chat_session_id,
                name=payload.name,
                agent_ide_id=payload.agent_ide_id,
                service_ports=payload.service_ports,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{agent_ide_id}")
    def get_agent_ide_status(request: Request, agent_ide_id: str):
        owner = _require_agent_ide_owner(request)
        try:
            payload = agent_ide.session_status(owner, agent_ide_id)
            payload["log_tail"] = agent_ide.read_managed_log(
                owner,
                agent_ide_id,
                limit=8000,
            )
            return payload
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/sessions/{agent_ide_id}")
    def disconnect_agent_ide(
        request: Request,
        agent_ide_id: str,
        delete: bool = False,
    ):
        owner = _require_agent_ide_owner(request)
        try:
            result = agent_ide.stop_session(
                owner,
                agent_ide_id,
                delete=delete,
            )
            clear_preview_state(agent_ide_id)
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.api_route(
        "/{agent_ide_id}/preview",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy_preview_root(request: Request, agent_ide_id: str):
        return await _proxy(request, agent_ide_id, "")

    @router.api_route(
        "/{agent_ide_id}/preview/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy_preview_path(
        request: Request,
        agent_ide_id: str,
        path: str,
    ):
        return await _proxy(request, agent_ide_id, path)

    @router.api_route(
        "/{agent_ide_id}/preview-token/{token}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy_preview_token_root(
        request: Request,
        agent_ide_id: str,
        token: str,
    ):
        return await _proxy(request, agent_ide_id, "", token)

    @router.api_route(
        "/{agent_ide_id}/preview-token/{token}/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy_preview_token_path(
        request: Request,
        agent_ide_id: str,
        token: str,
        path: str,
    ):
        return await _proxy(request, agent_ide_id, path, token)

    @router.websocket("/{agent_ide_id}/preview-token/{token}")
    async def proxy_preview_token_socket_root(
        websocket: WebSocket,
        agent_ide_id: str,
        token: str,
    ):
        await _websocket_proxy(websocket, agent_ide_id, token, "")

    @router.websocket("/{agent_ide_id}/preview-token/{token}/{path:path}")
    async def proxy_preview_token_socket_path(
        websocket: WebSocket,
        agent_ide_id: str,
        token: str,
        path: str,
    ):
        await _websocket_proxy(websocket, agent_ide_id, token, path)

    return router
