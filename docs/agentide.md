# AgentIDE

From any model-backed chat, run:

```text
/agentIDE /workspace/your-project
```

The command is case-insensitive. It validates and permanently binds that
directory to the current chat, switches the chat to Agent mode, and opens the
project's saved live preview. If the project has no saved preview connection,
AgentIDE starts a loopback-only static preview as a baseline. The command
itself is handled by application code and is not sent to the model.

## Component Select

With a live project open, click **Element select** in the AgentIDE preview
toolbar. Hovering outlines the element under the pointer; clicking it adds a
bounded `AGENTIDE_ELEMENT_REFERENCE` to the current chat composer. The
reference includes the project workspace, page path, stable CSS selector,
visible text, safe attributes, dimensions, relevant computed styles, and
best-effort React/Vue component and source hints. Type the requested change
after the reference and send the message.

Press Escape or click **Selecting…** to cancel. Selection only works from the
chat linked to the active AgentIDE project. Preview content is treated as
untrusted metadata, and selecting an element never executes a page-provided
command or sends a chat message automatically.

AgentIDE is Odysseus's workspace-oriented web development preview. It keeps a
site or app in one real project directory and displays the running frontend in
a docked, live-updating panel. The project's private port is proxied through
the authenticated Odysseus origin, so deployments only need to expose the
normal Odysseus port.

## Use it

AgentIDE is an admin-only item in the **Tools** section and icon rail.

1. Select a workspace in the chat input.
2. Open **Tools → AgentIDE**.
3. For a framework project, start its development server on a local port and
   enter that port, then select **Connect**.
4. For plain HTML/CSS/JavaScript, select **Start static**. Odysseus allocates a
   private port and manages the static server.
5. Keep **Live reload** enabled. AgentIDE watches the bounded project revision
   and reloads the iframe when project files change, including when the
   framework does not provide hot module replacement through the proxy.

The agent can perform the same lifecycle with the `agent_ide` tool. For a
framework app, it should start the server as a detached `#!bg` shell job and
then connect AgentIDE to that port. The tool result opens the panel only after
the project connection has been persisted.

Saved AgentIDE projects are owner-scoped and can be reopened from the project
selector after a browser refresh or application restart. A chat can be linked
to a project, but the project files remain normal workspace files rather than
copies in the Documents library.

## Preview controls

- **Desktop / Tablet / Mobile** changes the iframe viewport width.
- **Reload** refreshes the preview without changing the project.
- **Open in tab** opens the same authenticated proxy URL.
- **Focus** gives the preview the full Odysseus content area.
- **Server output** shows the bounded log tail for managed static servers.
- **Stop** stops an AgentIDE-managed server but preserves the saved project.
- **Remove** stops a managed server and deletes the saved connection. It does
  not delete any project files.

## Network and security design

- Upstreams are always `127.0.0.1:<validated-port>`. A user cannot supply an
  upstream host or URL, which prevents AgentIDE from becoming a generic SSRF
  proxy.
- Ports below 1024 and the Odysseus application port 3001 are rejected.
- The proxy requires the same authenticated admin session as the UI.
- The sandbox receives a signed, project-scoped preview capability so its
  opaque-origin asset and API requests do not need access to the Odysseus
  session cookie. The capability cannot open another project or any Odysseus
  API outside that project's preview mount.
- Cookies, authorization headers, forwarded headers, and raw authentication
  data are not sent to the project server.
- Request and response bodies are size-limited.
- The preview iframe is sandboxed and intentionally omits
  `allow-same-origin` and top-navigation privileges.
- Root-relative HTML, CSS, and module references are rewritten beneath the
  project's authenticated proxy mount. Binary responses are passed through
  unchanged.

Only services in the same network namespace as Odysseus are reachable. In a
container deployment, run the project server inside the Odysseus container or
provide an explicit, private container-network forward to loopback. AgentIDE
does not access the Docker socket and does not expose arbitrary host ports.

## Current limitation

The proxy handles normal HTTP methods and asset requests, but it does not proxy
WebSocket upgrades. Vite-style HMR sockets may therefore be unavailable.
Revision polling supplies a framework-independent full-page live reload, so
saved frontend changes still appear automatically.

## Database upgrade and rollback

Startup creates the additive `agent_ide_sessions` table through Odysseus's
existing `Base.metadata.create_all` migration convention. Existing tables and
data are not rewritten.

To roll back the feature code, stop any managed AgentIDE preview from the UI
before deploying the prior version. The older application safely ignores the
additive table. If the table must be removed, back up the database and run:

```sql
DROP TABLE agent_ide_sessions;
```

Removing the table deletes only saved preview connections, not workspace
project files.
