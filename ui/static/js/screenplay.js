/**
 * screenplay.js — Screenplay viewer + editor with chapter navigation,
 * approval, direct editing, and AI-assisted revision.
 */

const Screenplay = {

  slug: null,
  chapters: [],
  currentIdx: -1,
  currentData: null,  // { chapter_id, content, approved, status }

  // Edit mode state
  editMode: false,
  originalContent: null,
  dirty: false,

  init(slug) {
    Screenplay.slug = slug;
    Screenplay._loadChapters();
    Screenplay._bindKeys();
    Screenplay._bindBeforeUnload();
  },

  async _loadChapters() {
    try {
      const status = await App.api('GET', `/api/${Screenplay.slug}/status`);
      Screenplay.chapters = status.chapters || [];
      const sel = document.getElementById('sp-chapter-select');
      sel.innerHTML = '<option value="">Select chapter...</option>' +
        Screenplay.chapters.map(ch =>
          `<option value="${ch.chapter_id}">${App.escapeHtml(ch.title || ch.chapter_id)}</option>`
        ).join('');

      // Auto-select chapter from URL param or first chapter
      const urlChapter = new URLSearchParams(window.location.search).get('chapter');
      const target = urlChapter && Screenplay.chapters.find(c => c.chapter_id === urlChapter)
        ? urlChapter
        : (Screenplay.chapters.length > 0 ? Screenplay.chapters[0].chapter_id : null);
      if (target) {
        sel.value = target;
        Screenplay.load();
      }
    } catch (e) {
      App.showToast('Failed to load chapters', 'error');
    }
  },

  async load() {
    const chapterId = document.getElementById('sp-chapter-select').value;

    // Guard: unsaved changes when switching chapters
    if (Screenplay.editMode && Screenplay.dirty) {
      if (!confirm('You have unsaved changes. Switch chapters anyway?')) {
        document.getElementById('sp-chapter-select').value =
          Screenplay.chapters[Screenplay.currentIdx].chapter_id;
        return;
      }
    }

    // Exit edit mode silently when switching chapters
    if (Screenplay.editMode) {
      Screenplay._forceExitEditMode();
    }

    if (!chapterId) {
      document.getElementById('sp-content').innerHTML =
        '<div class="text-gray-600 text-center py-20">Select a chapter to view its screenplay</div>';
      document.getElementById('sp-approve-btn').classList.add('hidden');
      document.getElementById('sp-edit-btn').classList.add('hidden');
      document.getElementById('sp-status-badge').innerHTML = '';
      return;
    }

    Screenplay.currentIdx = Screenplay.chapters.findIndex(c => c.chapter_id === chapterId);

    document.getElementById('sp-content').innerHTML =
      '<div class="flex items-center justify-center py-20"><div class="skeleton h-6 w-48"></div></div>';

    try {
      const data = await App.api('GET', `/api/${Screenplay.slug}/chapter/${chapterId}/screenplay`);
      Screenplay.currentData = data;
      Screenplay._render(data);
    } catch (e) {
      if (e.message && e.message.includes('404')) {
        document.getElementById('sp-content').innerHTML =
          `<div class="text-center py-20">
            <div class="text-gray-500 text-lg mb-2">No screenplay yet</div>
            <div class="text-gray-600 text-sm">Run the Screenplay stage for this chapter from the Dashboard.</div>
          </div>`;
        document.getElementById('sp-approve-btn').classList.add('hidden');
        document.getElementById('sp-edit-btn').classList.add('hidden');
        document.getElementById('sp-status-badge').innerHTML = '';
      } else {
        document.getElementById('sp-content').innerHTML =
          '<div class="text-red-400 text-center py-20">Failed to load screenplay</div>';
      }
    }
  },

  _render(data) {
    const container = document.getElementById('sp-content');
    container.innerHTML = Screenplay._formatScreenplay(data.content);

    // Update approval button
    const btn = document.getElementById('sp-approve-btn');
    btn.classList.remove('hidden');
    if (data.approved) {
      btn.textContent = 'Approved';
      btn.className = 'px-3 py-1.5 rounded text-sm font-medium transition-colors bg-green-600 hover:bg-green-500 text-white';
    } else {
      btn.textContent = 'Approve';
      btn.className = 'px-3 py-1.5 rounded text-sm font-medium transition-colors bg-green-800 hover:bg-green-700 text-white';
    }

    // Show edit button
    document.getElementById('sp-edit-btn').classList.remove('hidden');

    // Status badge
    document.getElementById('sp-status-badge').innerHTML =
      App.statusBadge(data.approved ? 'approved' : data.status || 'pending');
  },

  /**
   * Convert screenplay markdown to styled HTML.
   * Recognizes standard screenplay format elements.
   */
  _formatScreenplay(md) {
    const lines = md.split('\n');
    let html = '';
    let inDialogue = false;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const trimmed = line.trim();

      if (!trimmed) {
        if (inDialogue) inDialogue = false;
        html += '<div class="h-4"></div>';
        continue;
      }

      // Scene headings: INT. / EXT. or lines starting with #
      if (/^(INT\.|EXT\.|INT\/EXT\.)/.test(trimmed)) {
        inDialogue = false;
        html += `<div class="sp-scene-heading">${App.escapeHtml(trimmed)}</div>`;
        continue;
      }

      // Markdown headings → title
      if (trimmed.startsWith('#')) {
        inDialogue = false;
        const text = trimmed.replace(/^#+\s*/, '');
        if (trimmed.startsWith('# ')) {
          html += `<div class="sp-title">${App.escapeHtml(text)}</div>`;
        } else if (trimmed.startsWith('## ')) {
          html += `<div class="sp-subtitle">${App.escapeHtml(text)}</div>`;
        } else {
          html += `<div class="sp-section">${App.escapeHtml(text)}</div>`;
        }
        continue;
      }

      // Bold text (**text**) → treated as stage direction or emphasis
      if (trimmed.startsWith('**') && trimmed.endsWith('**')) {
        inDialogue = false;
        const text = trimmed.slice(2, -2);
        html += `<div class="sp-direction">${App.escapeHtml(text)}</div>`;
        continue;
      }

      // Character name (ALL CAPS, possibly with (V.O.) or (O.S.))
      // Must be short-ish and all uppercase letters
      if (/^[A-Z][A-Z\s.''-]+(\s*\(.*\))?\s*$/.test(trimmed) && trimmed.length < 60) {
        inDialogue = true;
        html += `<div class="sp-character">${App.escapeHtml(trimmed)}</div>`;
        continue;
      }

      // Parenthetical (inside dialogue)
      if (inDialogue && trimmed.startsWith('(') && trimmed.endsWith(')')) {
        html += `<div class="sp-parenthetical">${App.escapeHtml(trimmed)}</div>`;
        continue;
      }

      // Transition (CUT TO:, FADE IN:, DISSOLVE TO:, etc.)
      if (/^(FADE\s*(IN|OUT|TO)|CUT\s+TO|DISSOLVE\s+TO|SMASH\s+CUT|MATCH\s+CUT|WIPE\s+TO):?\s*$/i.test(trimmed) ||
          (trimmed.endsWith(':') && /^[A-Z\s]+:$/.test(trimmed) && trimmed.length < 30)) {
        inDialogue = false;
        html += `<div class="sp-transition">${App.escapeHtml(trimmed)}</div>`;
        continue;
      }

      // Dialogue line (after character name)
      if (inDialogue) {
        html += `<div class="sp-dialogue">${App.escapeHtml(trimmed)}</div>`;
        continue;
      }

      // Action/description (default)
      html += `<div class="sp-action">${App.escapeHtml(trimmed)}</div>`;
    }

    return html;
  },

  // ------------------------------------------------------------------
  // Edit mode
  // ------------------------------------------------------------------

  enterEditMode() {
    if (!Screenplay.currentData || !Screenplay.currentData.content) return;

    Screenplay.editMode = true;
    Screenplay.originalContent = Screenplay.currentData.content;
    Screenplay.dirty = false;

    // Show editor, hide formatted view
    document.getElementById('sp-content').classList.add('hidden');
    document.getElementById('sp-editor-wrap').classList.remove('hidden');
    document.getElementById('sp-editor').value = Screenplay.currentData.content;

    // Show edit controls, hide view controls
    document.getElementById('sp-view-controls').classList.add('hidden');
    document.getElementById('sp-edit-controls').classList.remove('hidden');
    document.getElementById('sp-ai-bar').classList.remove('hidden');

    // Update char count
    Screenplay._updateCharCount();
    document.getElementById('sp-dirty-indicator').classList.add('hidden');

    // Listen for changes
    document.getElementById('sp-editor').addEventListener('input', Screenplay._onEditorInput);
  },

  exitEditMode() {
    if (Screenplay.dirty) {
      if (!confirm('You have unsaved changes. Discard them?')) return;
    }
    Screenplay.currentData.content = Screenplay.originalContent;
    Screenplay._forceExitEditMode();
    Screenplay._render(Screenplay.currentData);
  },

  /** Exit edit mode without confirmation or content restore. */
  _forceExitEditMode() {
    Screenplay.editMode = false;
    Screenplay.dirty = false;

    document.getElementById('sp-content').classList.remove('hidden');
    document.getElementById('sp-editor-wrap').classList.add('hidden');
    document.getElementById('sp-view-controls').classList.remove('hidden');
    document.getElementById('sp-edit-controls').classList.add('hidden');
    document.getElementById('sp-ai-bar').classList.add('hidden');

    document.getElementById('sp-editor').removeEventListener('input', Screenplay._onEditorInput);
  },

  // ------------------------------------------------------------------
  // Save
  // ------------------------------------------------------------------

  async save() {
    const content = document.getElementById('sp-editor').value;
    const chapterId = Screenplay.currentData.chapter_id;
    const btn = document.getElementById('sp-save-btn');

    if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }

    try {
      const result = await App.api('PUT',
        `/api/${Screenplay.slug}/chapter/${chapterId}/screenplay`,
        { content }
      );

      // Update local state
      Screenplay.currentData.content = content;
      Screenplay.currentData.approved = false;
      Screenplay.currentData.status = 'draft';
      Screenplay.originalContent = content;
      Screenplay.dirty = false;
      document.getElementById('sp-dirty-indicator').classList.add('hidden');

      // Status badge
      document.getElementById('sp-status-badge').innerHTML = App.statusBadge('draft');

      if (result.downstream_warning) {
        App.showToast('Saved. Shots may need re-generation (cinematographer already ran).', 'warning');
      } else {
        App.showToast('Screenplay saved', 'success');
      }
    } catch (e) {
      App.showToast('Failed to save screenplay', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
    }
  },

  // ------------------------------------------------------------------
  // AI Revision
  // ------------------------------------------------------------------

  async aiRevise() {
    const instructionEl = document.getElementById('sp-ai-instruction');
    const instruction = instructionEl ? instructionEl.value.trim() : '';
    if (!instruction) {
      App.showToast('Enter a revision instruction', 'warning');
      return;
    }

    const chapterId = Screenplay.currentData.chapter_id;
    const btn = document.getElementById('sp-ai-btn');
    const currentContent = document.getElementById('sp-editor').value;

    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="animate-pulse">Revising...</span>';
    }

    try {
      const result = await App.api('POST',
        `/api/${Screenplay.slug}/chapter/${chapterId}/screenplay/revise`,
        { instruction, content: currentContent }
      );

      // Put revised text in editor for review
      document.getElementById('sp-editor').value = result.revised;
      Screenplay.dirty = true;
      document.getElementById('sp-dirty-indicator').classList.remove('hidden');
      Screenplay._updateCharCount();

      const cost = result.cost_usd ? `$${result.cost_usd.toFixed(4)}` : '';
      App.showToast(`Revised ${cost}. Review and save when ready.`, 'success');
      instructionEl.value = '';
    } catch (e) {
      App.showToast('AI revision failed: ' + (e.message || 'unknown error'), 'error');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '&#9889; Revise';
      }
    }
  },

  // ------------------------------------------------------------------
  // Undo
  // ------------------------------------------------------------------

  async undo() {
    if (!Screenplay.currentData) return;
    if (!confirm('Restore the previous version of this screenplay?')) return;

    const chapterId = Screenplay.currentData.chapter_id;

    try {
      const result = await App.api('POST',
        `/api/${Screenplay.slug}/chapter/${chapterId}/screenplay/undo`
      );

      if (Screenplay.editMode) {
        document.getElementById('sp-editor').value = result.content;
        Screenplay.currentData.content = result.content;
        Screenplay.originalContent = result.content;
        Screenplay.dirty = false;
        document.getElementById('sp-dirty-indicator').classList.add('hidden');
        Screenplay._updateCharCount();
      } else {
        Screenplay.currentData.content = result.content;
        Screenplay._render(Screenplay.currentData);
      }

      App.showToast('Previous version restored', 'success');
    } catch (e) {
      if (e.message && e.message.includes('404')) {
        App.showToast('No backup available', 'info');
      } else {
        App.showToast('Undo failed', 'error');
      }
    }
  },

  // ------------------------------------------------------------------
  // Approval
  // ------------------------------------------------------------------

  async toggleApproval() {
    if (!Screenplay.currentData) return;
    const chapterId = Screenplay.currentData.chapter_id;

    try {
      const result = await App.api('POST', `/api/${Screenplay.slug}/chapter/${chapterId}/screenplay/approve`);
      Screenplay.currentData.approved = result.approved;
      Screenplay.currentData.status = result.status;
      Screenplay._render(Screenplay.currentData);
      App.showToast(
        result.approved ? 'Screenplay approved' : 'Screenplay approval removed',
        result.approved ? 'success' : 'info'
      );
    } catch (e) {
      App.showToast('Failed to update approval', 'error');
    }
  },

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  _onEditorInput() {
    Screenplay.dirty = true;
    document.getElementById('sp-dirty-indicator').classList.remove('hidden');
    Screenplay._updateCharCount();
  },

  _updateCharCount() {
    const editor = document.getElementById('sp-editor');
    const countEl = document.getElementById('sp-char-count');
    if (editor && countEl) {
      const lines = editor.value.split('\n').length;
      const chars = editor.value.length;
      countEl.textContent = `${lines} lines, ${chars.toLocaleString()} chars`;
    }
  },

  _bindBeforeUnload() {
    window.addEventListener('beforeunload', (e) => {
      if (Screenplay.editMode && Screenplay.dirty) {
        e.preventDefault();
        e.returnValue = '';
      }
    });
  },

  _bindKeys() {
    document.addEventListener('keydown', (e) => {
      // Only on screenplay page
      if (!document.getElementById('screenplay-root')) return;

      // Ctrl+E — toggle edit mode
      if ((e.ctrlKey || e.metaKey) && e.key === 'e') {
        e.preventDefault();
        if (Screenplay.editMode) Screenplay.exitEditMode();
        else Screenplay.enterEditMode();
        return;
      }

      // Ctrl+S — save in edit mode
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        if (Screenplay.editMode) Screenplay.save();
        return;
      }

      // Skip navigation shortcuts when typing in inputs
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;

      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
        e.preventDefault();
        Screenplay._navigate(1);
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
        e.preventDefault();
        Screenplay._navigate(-1);
      }
    });
  },

  _navigate(delta) {
    const newIdx = Screenplay.currentIdx + delta;
    if (newIdx < 0 || newIdx >= Screenplay.chapters.length) return;
    const sel = document.getElementById('sp-chapter-select');
    sel.value = Screenplay.chapters[newIdx].chapter_id;
    Screenplay.load();
  },
};
