/**
 * onlyoffice-editor.js — v4
 *
 * Works on BOTH matter workspace and drafting pages.
 * If __MATTER_ID__ is set (workspace), uses it as default.
 * openOnlyOfficeEditor() accepts optional matterId override.
 *
 * v4 additions:
 *   - AI Chat sidebar (flies in from right, vertical tab)
 *   - "Save as New Version" button in toolbar
 *   - Document-aware chat with MCP tool access
 */
(function() {
  'use strict';

  const DEFAULT_MATTER_ID = window.__MATTER_ID__ || '';

  const OFFICE_EXT = new Set([
    'docx','doc','xlsx','xls','pptx','ppt','odt','ods','odp','rtf'
  ]);
  const EXT_ICONS = {
    docx:'\u{1F4D8}', doc:'\u{1F4D8}', xlsx:'\u{1F4D7}', xls:'\u{1F4D7}',
    pptx:'\u{1F4D9}', ppt:'\u{1F4D9}', odt:'\u{1F4D8}', ods:'\u{1F4D7}', odp:'\u{1F4D9}', rtf:'\u{1F4D8}'
  };

  // ── Path normalization ──────────────────────────────────────
  // Convert absolute /mnt/praesidium/{tid}/... paths to relative form
  // so oo-config gets 'chats/session/file.docx' not the full absolute path
  function normalizeDocPath(p) {
    if (!p) return p;
    // /mnt/praesidium/{tenant_id}/chats/... → chats/...
    const chatsMatch = p.match(/\/mnt\/praesidium\/[^\/]+\/chats\/(.*)/);
    if (chatsMatch) return 'chats/' + chatsMatch[1];
    // /mnt/praesidium/{tenant_id}/matters/{client}/{matter}/... → relative subfolder path
    const mattersMatch = p.match(/\/mnt\/praesidium\/[^\/]+\/matters\/[^\/]+\/[^\/]+\/(.*)/);
    if (mattersMatch) return mattersMatch[1];
    return p;
  }

  // ── CSS ──────────────────────────────────────────────────────
  const style = document.createElement('style');
  style.textContent = `
    .oo-overlay {
      position:fixed; inset:0; z-index:10000;
      background:rgba(0,0,0,0.6);
      display:flex; align-items:stretch; justify-content:stretch;
      opacity:0; transition:opacity .2s ease;
      backdrop-filter:blur(2px);
    }
    .oo-overlay.visible { opacity:1; }
    .oo-container {
      flex:1; margin:12px;
      background:#fff; border-radius:8px;
      display:flex; flex-direction:column;
      overflow:hidden;
      box-shadow:0 25px 60px rgba(0,0,0,0.4);
      transform:scale(.97); transition:transform .2s ease;
    }
    .oo-overlay.visible .oo-container { transform:scale(1); }
    .oo-toolbar {
      display:flex; align-items:center; gap:8px;
      padding:6px 12px;
      background:#1B2A4A; color:#fff; flex-shrink:0;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    }
    .oo-toolbar .icon { font-size:20px; }
    .oo-toolbar .title {
      flex:1; font-size:13px; font-weight:600;
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    }
    .oo-toolbar button {
      background:rgba(255,255,255,.12); border:none; border-radius:5px;
      color:#fff; font-size:12px; padding:5px 14px; cursor:pointer;
      font-family:inherit; transition:background .15s;
    }
    .oo-toolbar button:hover { background:rgba(255,255,255,.22); }
    .oo-toolbar .btn-gold { background:rgba(200,146,58,0.3); }
    .oo-toolbar .btn-gold:hover { background:rgba(200,146,58,0.5); }
    .oo-toolbar .close-btn {
      background:none; font-size:20px; padding:2px 8px; opacity:.7;
    }
    .oo-toolbar .close-btn:hover { opacity:1; background:rgba(255,255,255,.1); }
    .oo-body-row { display:flex; flex:1; overflow:hidden; min-height:0; position:relative; }
    .oo-editor-wrap { flex:1; overflow:hidden; min-height:0; position:relative; }
    .oo-loading {
      position:absolute; inset:0;
      display:flex; align-items:center; justify-content:center;
      flex-direction:column; gap:12px;
      color:#64748b; font-size:13px; background:#f8fafc;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    }
    .oo-loading .spinner {
      width:32px; height:32px;
      border:3px solid #e2e8f0; border-top-color:#1B2A4A;
      border-radius:50%; animation:ooSpin .7s linear infinite;
    }
    @keyframes ooSpin { to { transform:rotate(360deg); } }
    .oo-error {
      position:absolute; inset:0;
      display:flex; align-items:center; justify-content:center;
      flex-direction:column; gap:8px; color:#dc2626; background:#fff;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    }
    /* ── Chat Sidebar ─────────────────────────────── */
    .oo-chat-tab {
      width:28px; background:#1B2A4A; display:flex; align-items:center;
      justify-content:center; cursor:pointer; flex-shrink:0;
      transition:background .15s; user-select:none; border-left:1px solid #0f1a30;
    }
    .oo-chat-tab:hover { background:#2a3f6a; }
    .oo-chat-tab span {
      writing-mode:vertical-rl; transform:rotate(180deg);
      font-size:11px; font-weight:700; letter-spacing:.08em;
      color:rgba(255,255,255,.7); text-transform:uppercase;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    }
    .oo-chat-tab.active { background:#C8923A; }
    .oo-chat-tab.active span { color:#fff; }
    .oo-chat-panel {
      width:0; overflow:hidden; transition:width .25s ease;
      border-left:1px solid #e2e8f0; display:flex; flex-direction:column;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      background:#fff;
    }
    .oo-chat-panel.open { width:380px; }
    .oo-chat-header {
      padding:10px 14px; border-bottom:1px solid #e2e8f0;
      display:flex; align-items:center; gap:8px; background:#f8fafc; flex-shrink:0;
    }
    .oo-chat-header .chat-title { font-size:13px; font-weight:600; color:#1B2A4A; flex:1; }
    .oo-chat-messages {
      flex:1; overflow-y:auto; padding:12px; display:flex;
      flex-direction:column; gap:10px; min-height:0;
    }
    .oo-chat-msg { display:flex; gap:8px; }
    .oo-chat-msg .avatar {
      width:22px; height:22px; border-radius:50%; display:flex;
      align-items:center; justify-content:center; font-size:8px;
      font-weight:700; color:#fff; flex-shrink:0; margin-top:2px;
    }
    .oo-chat-msg .bubble { flex:1; min-width:0; }
    .oo-chat-msg .bubble-text {
      font-size:12.5px; line-height:1.6; color:#1a1a1a;
    }
    .oo-chat-msg.user .bubble-text {
      background:#f1f5f9; border:1px solid #e2e8f0; border-radius:8px;
      padding:8px 10px;
    }
    .oo-chat-msg .tool-badge {
      font-size:10px; color:#C8923A; padding:2px 0; display:flex;
      align-items:center; gap:4px;
    }
    .oo-chat-input-row {
      display:flex; gap:6px; padding:10px 12px; border-top:1px solid #e2e8f0;
      background:#fff; flex-shrink:0;
    }
    .oo-chat-input-row input {
      flex:1; padding:7px 10px; border:1px solid #d1d5db; border-radius:6px;
      font-size:12px; font-family:inherit; color:#1a1a1a; outline:none;
      background:#fff;
    }
    .oo-chat-input-row input:focus { border-color:#3b82f6; }
    .oo-chat-input-row button {
      padding:7px 16px; background:#1B2A4A; color:#fff; border:none;
      border-radius:6px; font-size:12px; font-weight:600; cursor:pointer;
      font-family:inherit; white-space:nowrap;
    }
    .oo-chat-input-row button:disabled { opacity:.4; cursor:default; }
    .oo-chat-empty {
      flex:1; display:flex; align-items:center; justify-content:center;
      text-align:center; padding:24px; color:#94a3b8;
    }
    /* ── Context Menu (unchanged) ─────────────────── */
    .oo-ctx-menu {
      position:fixed; z-index:10001;
      background:#fff; border:1px solid #e2e8f0;
      border-radius:6px; box-shadow:0 8px 24px rgba(0,0,0,0.15);
      padding:4px 0; min-width:180px;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    }
    .oo-ctx-item {
      display:flex; align-items:center; gap:8px;
      padding:7px 14px; font-size:12px; cursor:pointer;
      color:#1a1a1a; transition:background .1s;
    }
    .oo-ctx-item:hover { background:#f1f5f9; }
    .oo-ctx-sep { height:1px; background:#e2e8f0; margin:4px 0; }
  `;
  document.head.appendChild(style);

  let currentEditor = null;
  let ooScriptLoaded = false;
  let ooScriptLoading = false;

  function loadOOScript(apiUrl) {
    return new Promise((resolve, reject) => {
      if (ooScriptLoaded && window.DocsAPI) { resolve(); return; }
      if (ooScriptLoading) {
        const iv = setInterval(() => {
          if (window.DocsAPI) { clearInterval(iv); resolve(); }
        }, 100);
        setTimeout(() => { clearInterval(iv); reject(new Error('Timeout')); }, 20000);
        return;
      }
      ooScriptLoading = true;
      const s = document.createElement('script');
      s.src = apiUrl;
      s.onload = () => { ooScriptLoaded = true; ooScriptLoading = false; resolve(); };
      s.onerror = () => { ooScriptLoading = false; reject(new Error('Failed to load OnlyOffice API')); };
      document.head.appendChild(s);
    });
  }

  function renderMarkdownSimple(text) {
    let h = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    h = h.replace(/```(\w*)\n([\s\S]*?)```/g, (_,l,c) =>
      `<pre style="background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;padding:8px;font-size:11px;font-family:monospace;overflow-x:auto;margin:6px 0;white-space:pre-wrap;">${c.trim()}</pre>`);
    h = h.replace(/`([^`]+)`/g, '<code style="background:#f1f5f9;padding:1px 4px;border-radius:3px;font-size:11px;font-family:monospace;">$1</code>');
    h = h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
    h = h.replace(/\*(.+?)\*/g,'<em>$1</em>');
    h = h.replace(/^- (.+)$/gm, '<div style="padding-left:12px;margin:2px 0;">\u2022 $1</div>');
    h = h.replace(/\n\n/g,'<div style="margin-top:6px;"></div>');
    h = h.replace(/\n/g,'<br>');
    return h;
  }

  // ── Open Editor ──────────────────────────────────────────────
  async function openEditor(path, filename, matterId) {
    path = normalizeDocPath(path);
    const mid = matterId || DEFAULT_MATTER_ID;
    if (!mid) {
      alert('Cannot open editor: no matter ID available.');
      return;
    }
    const API = `/api/v1/dms/matter/${mid}`;
    const ext = (filename || path).split('.').pop().toLowerCase();
    const icon = EXT_ICONS[ext] || '\u{1F4C4}';

    // Chat state
    let chatOpen = false;
    let chatMessages = [];
    let chatStreaming = false;

    const overlay = document.createElement('div');
    overlay.className = 'oo-overlay';
    overlay.innerHTML = `
      <div class="oo-container">
        <div class="oo-toolbar">
          <span class="icon">${icon}</span>
          <span class="title">${filename || path}</span>
          <button id="oo-save-version" class="btn-gold" title="Save current state as a new version">\u{1F4BE} Save as New Version</button>
          <button id="oo-dl">\u2B07 Download</button>
          <button class="close-btn" id="oo-close">\u00D7</button>
        </div>
        <div class="oo-body-row">
          <div class="oo-editor-wrap">
            <div class="oo-loading" id="oo-loading">
              <div class="spinner"></div>
              <div>Opening document\u2026</div>
            </div>
            <div id="oo-editor-host" style="width:100%;height:100%;"></div>
          </div>
          <div class="oo-chat-tab" id="oo-chat-toggle">
            <span>AI CHAT</span>
          </div>
          <div class="oo-chat-panel" id="oo-chat-panel">
            <div class="oo-chat-header">
              <div style="width:22px;height:22px;border-radius:50%;background:#C8923A;display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:700;color:#fff;">AI</div>
              <div class="chat-title">Document Assistant</div>
              <span id="oo-chat-close" style="cursor:pointer;font-size:16px;color:#9ca3af;padding:0 4px;">\u2715</span>
            </div>
            <div class="oo-chat-messages" id="oo-chat-messages">
              <div class="oo-chat-empty">
                <div>
                  <div style="font-size:24px;margin-bottom:6px;opacity:.4;">\u{1F4AC}</div>
                  <div style="font-size:12px;font-weight:500;margin-bottom:4px;">Document-Aware AI</div>
                  <div style="font-size:11px;">Ask about this document, request edits, or get drafting help. Full MCP tool access.</div>
                </div>
              </div>
            </div>
            <div class="oo-chat-input-row">
              <input type="text" id="oo-chat-input" placeholder="Ask about this document\u2026" />
              <button id="oo-chat-send">Send</button>
            </div>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    document.body.style.overflow = 'hidden';
    requestAnimationFrame(() => overlay.classList.add('visible'));

    // ── Close ──
    function close() {
      if (currentEditor) {
        try { currentEditor.destroyEditor(); } catch(e) {}
        currentEditor = null;
      }
      overlay.classList.remove('visible');
      document.body.style.overflow = '';
      setTimeout(() => overlay.remove(), 200);
    }
    overlay.querySelector('#oo-close').onclick = close;
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    const escH = e => { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', escH); } };
    document.addEventListener('keydown', escH);

    // ── Download ──
    overlay.querySelector('#oo-dl').onclick = () => {
      window.open(`${API}/download?path=${encodeURIComponent(path)}`, '_blank');
    };

    // ── Save as New Version ──
    overlay.querySelector('#oo-save-version').onclick = async () => {
      const btn = overlay.querySelector('#oo-save-version');
      btn.textContent = 'Saving\u2026'; btn.disabled = true;
      try {
        // Force save the current document first
        if (currentEditor) {
          try { currentEditor.forceSave(); } catch(e) {}
          await new Promise(r => setTimeout(r, 1500)); // give OO time to save
        }
        // Now call the version endpoint
        const r = await fetch(`/dms/disk/save-version`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ matter_id: mid, path: path, filename: filename })
        });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          throw new Error(d.detail || 'Save failed: HTTP ' + r.status);
        }
        const d = await r.json();
        btn.textContent = '\u2713 Version ' + (d.version_number || 'saved');
        setTimeout(() => { btn.textContent = '\u{1F4BE} Save as New Version'; btn.disabled = false; }, 2500);
      } catch (err) {
        btn.textContent = '\u2717 ' + err.message;
        setTimeout(() => { btn.textContent = '\u{1F4BE} Save as New Version'; btn.disabled = false; }, 3000);
      }
    };

    // ── Chat Toggle ──
    const chatToggle = overlay.querySelector('#oo-chat-toggle');
    const chatPanel = overlay.querySelector('#oo-chat-panel');
    const chatClose = overlay.querySelector('#oo-chat-close');
    const chatInput = overlay.querySelector('#oo-chat-input');
    const chatSendBtn = overlay.querySelector('#oo-chat-send');
    const chatMsgContainer = overlay.querySelector('#oo-chat-messages');

    function toggleChat() {
      chatOpen = !chatOpen;
      chatPanel.classList.toggle('open', chatOpen);
      chatToggle.classList.toggle('active', chatOpen);
      if (chatOpen) setTimeout(() => chatInput.focus(), 300);
    }
    chatToggle.onclick = toggleChat;
    chatClose.onclick = toggleChat;

    function scrollChat() {
      chatMsgContainer.scrollTop = chatMsgContainer.scrollHeight;
    }

    function renderMessages() {
      if (chatMessages.length === 0) {
        chatMsgContainer.innerHTML = `<div class="oo-chat-empty">
          <div><div style="font-size:24px;margin-bottom:6px;opacity:.4;">\u{1F4AC}</div>
          <div style="font-size:12px;font-weight:500;margin-bottom:4px;">Document-Aware AI</div>
          <div style="font-size:11px;">Ask about this document, request edits, or get drafting help. Full MCP tool access.</div></div></div>`;
        return;
      }
      let html = '';
      for (const msg of chatMessages) {
        const isUser = msg.role === 'user';
        const avatarBg = isUser ? '#1B2A4A' : '#C8923A';
        const avatarText = isUser ? 'You' : 'AI';
        html += `<div class="oo-chat-msg ${isUser?'user':'ai'}">
          <div class="avatar" style="background:${avatarBg};">${avatarText}</div>
          <div class="bubble">`;
        if (msg.tools && msg.tools.length > 0) {
          for (const t of msg.tools) {
            html += `<div class="tool-badge">${t.done ? '\u2022' : '\u23F3'} ${t.name}</div>`;
          }
        }
        html += `<div class="bubble-text">${isUser ? msg.content.replace(/</g,'&lt;').replace(/>/g,'&gt;') : renderMarkdownSimple(msg.content)}</div>`;
        html += `</div></div>`;
      }
      chatMsgContainer.innerHTML = html;
      scrollChat();
    }

    async function sendChat() {
      const text = chatInput.value.trim();
      if (!text || chatStreaming) return;
      chatInput.value = '';
      chatMessages.push({ role: 'user', content: text });
      chatMessages.push({ role: 'assistant', content: '', tools: [], streaming: true });
      renderMessages();

      chatStreaming = true;
      chatSendBtn.disabled = true;
      chatSendBtn.textContent = '\u2026';

      let fullText = '';
      let activeTools = [];
      try {
        const context = {
          page: 'editor',
          matter_id: mid,
          document_path: path,
          document_filename: filename,
        };
        const resp = await fetch('/api/ai-chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: chatMessages.filter(m => !m.streaming), context })
        });
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const evt = JSON.parse(line.slice(6));
              if (evt.type === 'text') {
                fullText += evt.text;
                chatMessages[chatMessages.length - 1].content = fullText;
                renderMessages();
              }
              else if (evt.type === 'tool_start') {
                activeTools.push({ name: evt.tool, done: false });
                chatMessages[chatMessages.length - 1].tools = [...activeTools];
                renderMessages();
              }
              else if (evt.type === 'block_stop') {
                activeTools = activeTools.map(t => t.done ? t : { ...t, done: true });
                chatMessages[chatMessages.length - 1].tools = [...activeTools];
                renderMessages();
              }
              else if (evt.type === 'error') {
                fullText += '\n\n**Error:** ' + evt.text;
                chatMessages[chatMessages.length - 1].content = fullText;
                renderMessages();
              }
              else if (evt.type === 'done') break;
            } catch (e) {}
          }
        }
        chatMessages[chatMessages.length - 1] = { role: 'assistant', content: fullText, tools: activeTools };
      } catch (err) {
        chatMessages[chatMessages.length - 1] = { role: 'assistant', content: '**Error:** ' + err.message };
      }
      chatStreaming = false;
      chatSendBtn.disabled = false;
      chatSendBtn.textContent = 'Send';
      renderMessages();
      chatInput.focus();
    }

    chatSendBtn.onclick = sendChat;
    chatInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
    });

    // ── Load Editor ──
    try {
      const resp = await fetch(`${API}/oo-config?path=${encodeURIComponent(path)}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      await loadOOScript(data.api_url);
      const loading = overlay.querySelector('#oo-loading');
      if (loading) loading.style.display = 'none';
      currentEditor = new DocsAPI.DocEditor('oo-editor-host', data.config);
    } catch (err) {
      overlay.querySelector('.oo-editor-wrap').innerHTML = `<div class="oo-error">
        <div style="font-size:48px;">${icon}</div>
        <div style="font-size:14px;font-weight:600;">Could not open editor</div>
        <div style="font-size:12px;color:#64748b;max-width:400px;text-align:center;">${err.message}</div>
      </div>`;
    }
  }

  // Expose globally
  window.openOnlyOfficeEditor = openEditor;

  // ── Context Menu + Preview Edit Button (workspace only) ──
  if (DEFAULT_MATTER_ID) {
    const API = `/api/v1/dms/matter/${DEFAULT_MATTER_ID}`;

    // Edit button in preview toolbar
    const previewObserver = new MutationObserver(() => {
      const backBtn = document.querySelector('button');
      if (!backBtn || !backBtn.textContent.includes('\u2190 Back')) return;
      if (document.getElementById('oo-edit-in-preview')) return;
      const toolbar = backBtn.closest('div');
      if (!toolbar) return;
      const nameSpan = toolbar.querySelector('span[style*="font-weight"]');
      if (!nameSpan) return;
      const fullText = nameSpan.textContent.trim();
      const filename = fullText.replace(/^[\u{1F4D8}\u{1F4D7}\u{1F4D9}\u{1F4D5}\u{1F4C4}\u{1F5BC}\u{1F4E7}\u{1F4E6}\u{1F3B5}\u{1F3AC}]\s*/u, '');
      const ext = filename.split('.').pop().toLowerCase();
      if (!OFFICE_EXT.has(ext)) return;
      const dlLink = toolbar.querySelector('a[title="Download"]');
      if (!dlLink) return;
      const pathMatch = (dlLink.getAttribute('href') || '').match(/path=([^&]+)/);
      if (!pathMatch) return;
      const editBtn = document.createElement('button');
      editBtn.id = 'oo-edit-in-preview';
      editBtn.textContent = '\u270F\uFE0F Edit';
      editBtn.title = 'Open in OnlyOffice Editor';
      editBtn.style.cssText = 'background:#1B2A4A;color:#fff;border:none;border-radius:4px;padding:3px 10px;font-size:11px;cursor:pointer;font-family:inherit;margin-right:4px;';
      editBtn.onclick = () => openEditor(decodeURIComponent(pathMatch[1]), filename);
      dlLink.parentNode.insertBefore(editBtn, dlLink);
    });
    previewObserver.observe(document.body, { childList: true, subtree: true });
  }

  console.log('[Praesidium] OnlyOffice editor v4 loaded' + (DEFAULT_MATTER_ID ? ' (workspace mode)' : ' (drafting mode)'));
})();
