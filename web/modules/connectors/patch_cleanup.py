"""
Patch script for client_matter_cleanup.html
Run: docker cp this file to praesidium-web:/tmp/patch_cleanup.py
Then: docker exec praesidium-web python /tmp/patch_cleanup.py
"""

content = open('/app/core/templates/tenant_admin/client_matter_cleanup.html').read()
original_len = len(content)

# ── 1. Replace static merge select with typeahead ────────────────────────
old_merge = '''      {# Merge into dropdown #}
      <select class="merge-select" id="merge-{{ client.id }}"
              onclick="event.stopPropagation()"
              onchange="onMergeSelect(this, '{{ client.id }}')"
              style="padding:3px 6px; font-size:11px; border:1px solid var(--border-color,#e2e8f0); border-radius:4px; max-width:160px; background:var(--surface,#fff); color:var(--text);">
        <option value="">Merge into...</option>
        {% for other in all_clients %}
        {% if other.id != client.id %}
        <option value="{{ other.id }}">{{ other.client_name }}</option>
        {% endif %}
        {% endfor %}
      </select>'''

new_merge = '''      {# Merge into typeahead #}
      <div style="position:relative; flex-shrink:0;" onclick="event.stopPropagation()">
        <input type="text"
               class="merge-search"
               id="merge-search-{{ client.id }}"
               placeholder="Merge into..."
               autocomplete="off"
               data-client-id="{{ client.id }}"
               oninput="searchMergeClients(this, '{{ client.id }}')"
               onfocus="searchMergeClients(this, '{{ client.id }}')"
               onblur="hideMergeDropdown('{{ client.id }}')"
               style="padding:3px 6px; font-size:11px; border:1px solid var(--border-color,#e2e8f0); border-radius:4px; width:140px; background:var(--surface,#fff); color:var(--text);">
        <input type="hidden" id="merge-val-{{ client.id }}" value="">
        <div id="merge-drop-{{ client.id }}"
             style="display:none; position:absolute; right:0; top:100%; z-index:300;
                    background:var(--surface,#fff); border:1px solid var(--border-color,#e2e8f0);
                    border-radius:4px; box-shadow:0 4px 12px rgba(0,0,0,0.1);
                    min-width:200px; max-height:180px; overflow-y:auto; font-size:11px;">
        </div>
      </div>'''

if old_merge in content:
    content = content.replace(old_merge, new_merge)
    print('✓ merge dropdown replaced')
else:
    print('✗ merge dropdown NOT FOUND')

# ── 2. Replace onMergeSelect JS function with typeahead functions ─────────
old_merge_js = '''function onMergeSelect(select, clientId) {
  if (select.value) {
    selectedClients.add(clientId);
    const cb = document.querySelector(`#client-${clientId} .client-check`);
    if (cb) cb.checked = true;
  }
  updateSelectionCount();
}'''

new_merge_js = '''// ── Merge typeahead ──────────────────────────────────────────────────────
const mergeTimers = {};

async function searchMergeClients(input, clientId) {
  const q = input.value.trim();
  const drop = document.getElementById('merge-drop-' + clientId);

  clearTimeout(mergeTimers[clientId]);
  if (!q || q.length < 1) {
    drop.style.display = 'none';
    document.getElementById('merge-val-' + clientId).value = '';
    return;
  }

  mergeTimers[clientId] = setTimeout(async () => {
    try {
      const resp = await fetch(`/tenant-admin/client-matter-cleanup/search-clients?q=${encodeURIComponent(q)}&exclude=${clientId}`);
      const data = await resp.json();
      if (!data.clients || !data.clients.length) {
        drop.innerHTML = '<div style="padding:8px 10px; color:var(--muted);">No matches</div>';
        drop.style.display = 'block';
        return;
      }
      drop.innerHTML = data.clients.map(c => `
        <div style="padding:6px 10px; cursor:pointer; border-bottom:1px solid #f8fafc;"
             onmouseover="this.style.background='#f1f5f9'"
             onmouseout="this.style.background=''"
             onmousedown="selectMergeTarget('${clientId}', '${c.id}', ${JSON.stringify(c.name)})">
          <div style="font-weight:500; color:var(--text);">${escHtml(c.name)}</div>
          <div style="font-size:10px; color:var(--muted);">${c.matter_count} matter${c.matter_count !== 1 ? 's' : ''}</div>
        </div>`).join('');
      drop.style.display = 'block';
    } catch(e) {
      drop.style.display = 'none';
    }
  }, 200);
}

function selectMergeTarget(clientId, targetId, targetName) {
  document.getElementById('merge-search-' + clientId).value = targetName;
  document.getElementById('merge-val-' + clientId).value = targetId;
  document.getElementById('merge-drop-' + clientId).style.display = 'none';
  // Mark client as selected
  selectedClients.add(clientId);
  const cb = document.querySelector(`#client-${clientId} .client-check`);
  if (cb) cb.checked = true;
  updateSelectionCount();
}

function hideMergeDropdown(clientId) {
  setTimeout(() => {
    const drop = document.getElementById('merge-drop-' + clientId);
    if (drop) drop.style.display = 'none';
  }, 200);
}

function escHtml(str) {
  if (!str) return '';
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            .replace(/"/g,'&quot;').replace(/\'/g,'&#39;');
}

function onMergeSelect(select, clientId) {
  // Legacy - kept for compatibility
  if (select.value) {
    selectedClients.add(clientId);
    const cb = document.querySelector(`#client-${clientId} .client-check`);
    if (cb) cb.checked = true;
  }
  updateSelectionCount();
}'''

if old_merge_js in content:
    content = content.replace(old_merge_js, new_merge_js)
    print('✓ merge JS replaced')
else:
    print('✗ merge JS NOT FOUND - appending')
    # Append before closing script tag
    content = content.replace('// ── Folder picker modal ───', new_merge_js + '\n// ── Folder picker modal ───')

# ── 3. Update commit handler to read merge-val instead of merge select ────
old_commit_merge = '''    // Collect client updates
  const clientUpdates = [];
  selectedClients.forEach(clientId => {
    const nameInput = document.getElementById('cname-' + clientId);
    const mergeSelect = document.getElementById('merge-' + clientId);
    const update = {client_id: clientId};
    if (nameInput && nameInput.value !== nameInput.dataset.original) {
      update.client_name = nameInput.value.trim();
    }
    if (mergeSelect && mergeSelect.value) {
      update.merge_into = mergeSelect.value;
    }
    if (Object.keys(update).length > 1) clientUpdates.push(update);
  });'''

new_commit_merge = '''  // Collect client updates
  const clientUpdates = [];
  selectedClients.forEach(clientId => {
    const nameInput = document.getElementById('cname-' + clientId);
    const mergeVal  = document.getElementById('merge-val-' + clientId);
    const update = {client_id: clientId};
    if (nameInput && nameInput.value !== nameInput.dataset.original) {
      update.client_name = nameInput.value.trim();
    }
    if (mergeVal && mergeVal.value) {
      update.merge_into = mergeVal.value;
    }
    if (Object.keys(update).length > 1) clientUpdates.push(update);
  });'''

if old_commit_merge in content:
    content = content.replace(old_commit_merge, new_commit_merge)
    print('✓ commit merge logic updated')
else:
    print('✗ commit merge logic NOT FOUND')

# ── 4. After commit success, update merge search inputs ──────────────────
old_merge_commit_ui = '''      clientUpdates.forEach(u => {
        if (u.merge_into) {
          const row = document.getElementById('client-' + u.client_id);
          if (row) { row.style.opacity = '0.3'; committed['c_' + u.client_id] = true; }
        } else {
          const inp = document.getElementById('cname-' + u.client_id);
          if (inp && u.client_name) { inp.dataset.original = u.client_name; inp.classList.remove('modified'); }
          if (u.client_name) committed['c_' + u.client_id] = true;
        }
      });'''

new_merge_commit_ui = '''      clientUpdates.forEach(u => {
        if (u.merge_into) {
          const row = document.getElementById('client-' + u.client_id);
          if (row) { row.style.opacity = '0.3'; committed['c_' + u.client_id] = true; }
          // Clear merge search input
          const ms = document.getElementById('merge-search-' + u.client_id);
          const mv = document.getElementById('merge-val-' + u.client_id);
          if (ms) ms.value = '';
          if (mv) mv.value = '';
        } else {
          const inp = document.getElementById('cname-' + u.client_id);
          if (inp && u.client_name) { inp.dataset.original = u.client_name; inp.classList.remove('modified'); }
          if (u.client_name) committed['c_' + u.client_id] = true;
        }
      });'''

if old_merge_commit_ui in content:
    content = content.replace(old_merge_commit_ui, new_merge_commit_ui)
    print('✓ commit UI updated')
else:
    print('✗ commit UI NOT FOUND')

open('/app/core/templates/tenant_admin/client_matter_cleanup.html', 'w').write(content)
print(f'\nDone. Size: {original_len} -> {len(content)} chars')
