"""Authenticated AgentIDE project/session API and loopback preview proxy."""

from __future__ import annotations

import json
import re
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src import agent_ide
from src.auth_helpers import require_user
from src.tool_security import owner_is_admin_or_single_user

_MAX_PROXY_REQUEST_BYTES = 10 * 1024 * 1024
_MAX_PROXY_RESPONSE_BYTES = 40 * 1024 * 1024
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


def _preview_bootstrap(prefix: str) -> str:
    """Keep requests proxied and expose a bounded, read-only element picker.

    The preview remains in a sandboxed opaque-origin iframe. The picker only
    reports DOM metadata to its parent after an explicit, request-scoped
    activation message; it cannot call application APIs or execute tools.
    """
    quoted = json.dumps(prefix)
    template = r"""<script>(function(){
const p=__AGENTIDE_PREFIX__;
const u=x=>typeof x==='string'&&x.startsWith('/')&&!x.startsWith('//')&&!x.startsWith(p)?p+x.slice(1):x;
const f=window.fetch;window.fetch=function(x,o){
  if(typeof x==='string')x=u(x);
  else if(x instanceof Request&&x.url.startsWith(location.origin+'/')){
    const q=new URL(x.url);x=new Request(u(q.pathname+q.search),x);
  }
  return f.call(this,x,o);
};
const xo=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(m,x){arguments[1]=u(x);return xo.apply(this,arguments);};
if(window.EventSource){const E=window.EventSource;window.EventSource=function(x,o){return new E(u(x),o);};}

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
    return template.replace("__AGENTIDE_PREFIX__", quoted)


def _rewrite_preview_body(
    body: bytes,
    *,
    content_type: str,
    agent_ide_id: str,
    token: Optional[str] = None,
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
    rewritten = _rewrite_root_references(
        text,
        prefix,
        content_type.lower(),
    )
    if "text/html" in content_type.lower():
        bootstrap = _preview_bootstrap(prefix)
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
    try:
        port = agent_ide.validate_port(record.port)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if "\x00" in path or "\\" in path:
        raise HTTPException(status_code=400, detail="Invalid preview path")
    if token and request.method == "OPTIONS":
        requested_headers = request.headers.get("access-control-request-headers", "")
        allowed_headers = ", ".join(
            item.strip()
            for item in requested_headers.split(",")
            if item.strip().lower() in _FORWARDED_REQUEST_HEADERS
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

    upstream_path = "/" + path.lstrip("/")
    query = request.url.query
    target = f"http://127.0.0.1:{port}{upstream_path}"
    if query:
        target += "?" + query
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _FORWARDED_REQUEST_HEADERS
        and key.lower() not in _HOP_BY_HOP
    }
    headers["host"] = f"127.0.0.1:{port}"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=2.0),
            follow_redirects=False,
        ) as client:
            async with client.stream(
                request.method,
                target,
                headers=headers,
                content=raw_body or None,
            ) as upstream:
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
                body = b"".join(chunks)
                content_type = upstream.headers.get(
                    "content-type",
                    "application/octet-stream",
                )
                body = _rewrite_preview_body(
                    body,
                    content_type=content_type,
                    agent_ide_id=agent_ide_id,
                    token=token,
                )
                response_headers = {
                    key: value
                    for key, value in upstream.headers.items()
                    if key.lower() in _FORWARDED_RESPONSE_HEADERS
                    and key.lower() not in {"content-length", "content-encoding"}
                }
                location = upstream.headers.get("location")
                if location and location.startswith("/") and not location.startswith("//"):
                    response_headers["location"] = (
                        _proxy_prefix(agent_ide_id, token) + location.lstrip("/")
                    )
                response_headers["x-agentide-preview"] = agent_ide_id
                if token:
                    response_headers["access-control-allow-origin"] = "null"
                if "text/html" in content_type.lower():
                    response_headers["cache-control"] = "no-store"
                return Response(
                    content=body,
                    status_code=upstream.status_code,
                    headers=response_headers,
                    media_type=None,
                )
    except HTTPException:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Nothing is reachable on AgentIDE port {port}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"AgentIDE preview failed: {type(exc).__name__}",
        ) from exc


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
            return agent_ide.stop_session(
                owner,
                agent_ide_id,
                delete=delete,
            )
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

    return router
