/**
 * characters.js — Character profiles page.
 * Shows character cards with visual tags, expandable fully-editable detail panels.
 */

const Characters = {

  slug: null,
  characters: [],

  async init(slug) {
    Characters.slug = slug;
    try {
      Characters.characters = await App.api('GET', `/api/${slug}/characters`);
      Characters._render();
    } catch (e) {
      document.getElementById('character-grid').innerHTML =
        '<div class="col-span-full text-center py-12 text-gray-600">No characters yet. Run the Characters stage to generate profiles.</div>';
    }
  },

  _render() {
    const grid = document.getElementById('character-grid');
    const countEl = document.getElementById('char-count');
    if (!grid) return;

    if (countEl) countEl.textContent = `${Characters.characters.length} characters`;

    // Show/hide "Generate Missing Visuals" button
    const missingCount = Characters.characters.filter(ch => !ch.visual_tag).length;
    const missingBtn = document.getElementById('gen-missing-visuals-btn');
    if (missingBtn) {
      if (missingCount > 0) {
        missingBtn.classList.remove('hidden');
        missingBtn.textContent = `Generate Missing Visuals (${missingCount})`;
      } else {
        missingBtn.classList.add('hidden');
      }
    }

    // Show/hide "Regenerate Visual Tags" button (for characters that HAVE tags)
    const hasTagCount = Characters.characters.filter(ch => ch.visual_tag).length;
    const regenBtn = document.getElementById('regen-visuals-btn');
    if (regenBtn) {
      if (hasTagCount > 0) {
        regenBtn.classList.remove('hidden');
        regenBtn.textContent = `Regenerate Visual Tags (${hasTagCount})`;
      } else {
        regenBtn.classList.add('hidden');
      }
    }

    if (Characters.characters.length === 0) {
      grid.innerHTML = `
        <div class="col-span-full text-center py-12">
          <div class="text-gray-600 text-lg mb-2">No character profiles</div>
          <div class="text-gray-700 text-sm">Run the Characters stage from the dashboard to generate profiles with visual consistency tags.</div>
        </div>
      `;
      return;
    }

    grid.innerHTML = Characters.characters.map(ch => `
      <div class="card p-4 cursor-pointer hover:ring-1 hover:ring-babylon-500 transition-all"
           onclick="Characters.expand('${App.escapeHtml(ch.character_id)}')">
        <div class="flex items-start justify-between mb-2">
          <h3 class="text-sm font-semibold text-gray-200">${App.escapeHtml(ch.display_name || ch.character_id)}</h3>
          <span class="text-xs text-gray-600">${App.escapeHtml(ch.character_id)}</span>
        </div>
        ${ch.has_reference_image ? `
          <div class="mb-2 rounded overflow-hidden aspect-square bg-gray-800 relative group"
               onclick="event.stopPropagation(); Characters.generateReference('${App.escapeHtml(ch.character_id)}')">
            <img src="/api/${Characters.slug}/character/${ch.character_id}/reference-image?t=${Date.now()}"
                 alt="${App.escapeHtml(ch.display_name || ch.character_id)}"
                 class="w-full h-full object-cover" />
            <div class="absolute inset-0 bg-black/60 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
              <span class="text-xs text-white">Regenerate</span>
            </div>
          </div>
        ` : `
          <div class="mb-2 rounded aspect-square bg-gray-800/50 border border-dashed border-gray-700
                      flex items-center justify-center hover:border-babylon-500 transition-colors"
               onclick="event.stopPropagation(); Characters.generateReference('${App.escapeHtml(ch.character_id)}')">
            <div class="text-center">
              <div class="text-gray-600 text-2xl mb-1">+</div>
              <div class="text-xs text-gray-600">Generate Reference</div>
            </div>
          </div>
        `}
        ${ch.role ? `<div class="text-xs text-gray-500 mb-2">${App.escapeHtml(ch.role)}</div>` : ''}
        ${ch.visual_tag ? `
          <div class="mt-2 p-2 bg-gray-800/50 rounded border border-gray-700/50">
            <div class="text-xs text-gray-600 uppercase mb-1">Visual Tag</div>
            <div class="text-xs text-babylon-300 leading-relaxed">${App.escapeHtml(ch.visual_tag)}</div>
          </div>
        ` : `
          <div class="mt-2 p-2 bg-yellow-900/20 rounded border border-yellow-700/40">
            <div class="text-xs text-yellow-500">Missing visual tag</div>
          </div>
        `}
        <div class="flex items-center gap-2 mt-3 flex-wrap">
          ${ch.has_voice ? '<span class="badge badge-approved">Voice</span>' : ''}
          ${ch.has_reference_image ? '<span class="badge badge-approved">Reference</span>' : ''}
          ${ch.has_lora ? '<span class="badge" style="background:rgba(16,185,129,0.15);color:#34d399;border-color:rgba(16,185,129,0.3)">LoRA</span>' : ''}
          ${ch.has_training_images && !ch.has_lora ? `<span class="badge badge-draft">${ch.training_images_count} training imgs</span>` : ''}
          ${ch.tier ? `<span class="badge badge-draft">${App.escapeHtml(ch.tier)}</span>` : ''}
        </div>
      </div>
    `).join('');
  },

  async expand(characterId) {
    const panel = document.getElementById('character-detail');
    if (!panel) return;

    if (panel.dataset.character === characterId && !panel.classList.contains('hidden')) {
      panel.classList.add('hidden');
      return;
    }

    panel.dataset.character = characterId;
    panel.classList.remove('hidden');
    panel.innerHTML = '<div class="skeleton h-32 w-full"></div>';

    try {
      const char = await App.api('GET', `/api/${Characters.slug}/character/${characterId}`);
      Characters._renderDetail(panel, char);
      // Scroll the detail panel into view
      requestAnimationFrame(() => {
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    } catch (e) {
      panel.innerHTML = '<div class="text-red-400 text-sm">Failed to load character details.</div>';
    }
  },

  async generateReference(characterId) {
    let char, visualStyle = '';
    try {
      char = await App.api('GET', `/api/${Characters.slug}/character/${characterId}`);
    } catch (e) {
      App.showToast('Failed to load character', 'error');
      return;
    }
    // Load visual style from world bible for period-accurate imagery
    try {
      const wb = await App.api('GET', `/api/${Characters.slug}/world-bible`);
      const bible = wb.world_bible || wb;
      visualStyle = bible.visual_style || '';
    } catch (e) { /* ignore */ }

    // Always use pen/ink/marker storyboard medium for character images
    // If we have a world visual_style, strip photorealistic terms but keep world-specific elements
    let style = 'Pen, ink, and marker storyboard illustration, bold confident linework, rich saturated color washes, cinematic composition, film pre-production concept art style';
    if (visualStyle) {
      // Strip photorealistic terms, keep world-specific elements (lighting, palette, setting)
      const worldParts = visualStyle
        .replace(/photorealistic|cinematic film still|photograph|3D render|CGI/gi, '')
        .replace(/,\s*,/g, ',').replace(/^[,\s]+|[,\s]+$/g, '').trim();
      if (worldParts) style += ', ' + worldParts;
    }
    const vis = char.visual_tag || '';
    const phys = (char.description || {}).physical_appearance || '';
    const costume = char.costume_default || '';
    const promptPreview = [style, 'Character portrait, three-quarter view', vis, phys, costume ? `Wearing ${costume}` : '', 'Detailed face, expressive ink linework, vivid marker color, single character'].filter(Boolean).join('. ');

    App.openModal(`
      <h3 class="text-base font-semibold text-gray-100 mb-3">
        Generate Reference &mdash; ${App.escapeHtml(char.display_name || characterId)}
      </h3>
      <div class="mb-3">
        <label class="text-xs text-gray-500 uppercase mb-1 block">Prompt (editable)</label>
        <textarea id="ref-prompt" rows="4"
                  class="w-full bg-gray-800 border border-gray-700 rounded p-2 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-babylon-500"
        >${App.escapeHtml(promptPreview)}</textarea>
      </div>
      <div class="mb-4">
        <label class="text-xs text-gray-500 uppercase mb-1 block">Feedback / Adjustments</label>
        <input id="ref-feedback" type="text" placeholder="e.g., make hair darker, add scar on left cheek"
               class="w-full bg-gray-800 border border-gray-700 rounded p-2 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-babylon-500" />
      </div>
      <div class="flex justify-end gap-2">
        <button onclick="App.closeModal()" class="text-sm text-gray-400 hover:text-gray-200 px-3 py-1">Cancel</button>
        <button onclick="Characters._doGenerate('${App.escapeHtml(characterId)}')"
                id="ref-gen-btn"
                class="bg-babylon-600 hover:bg-babylon-500 text-white text-sm px-4 py-1.5 rounded transition-colors">
          Generate (Free)
        </button>
      </div>
    `);
  },

  async _doGenerate(characterId) {
    const promptEl = document.getElementById('ref-prompt');
    const feedbackEl = document.getElementById('ref-feedback');
    const prompt = promptEl ? promptEl.value.trim() : '';
    const feedback = feedbackEl ? feedbackEl.value.trim() : '';
    const btn = document.getElementById('ref-gen-btn');

    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Generating...';
    }

    try {
      await App.api('POST', `/api/${Characters.slug}/character/${characterId}/generate-reference`, {
        prompt_override: prompt || undefined,
        feedback: feedback || undefined,
      });
      App.closeModal();
      App.showToast('Reference image generated!', 'success');
      Characters.init(Characters.slug);
    } catch (e) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Generate (Free)';
      }
    }
  },

  // ── AI-assisted revision ──

  async aiRevise(characterId) {
    const el = document.getElementById('ce-ai-feedback');
    if (!el) return;
    const feedback = el.value.trim();
    if (!feedback) {
      App.showToast('Enter feedback for Claude', 'warning');
      return;
    }

    const btn = document.getElementById('ce-ai-btn');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="animate-pulse">Claude is revising...</span>';
    }

    try {
      const res = await App.api('POST', `/api/${Characters.slug}/character/${characterId}/ai-edit`, { feedback });
      App.showToast(`Character revised ($${res.cost_usd?.toFixed(4) || '?'})`, 'success');
      // Refresh cards + detail panel with updated data
      Characters.characters = await App.api('GET', `/api/${Characters.slug}/characters`);
      Characters._render();
      Characters.expand(characterId);
    } catch (e) {
      // Error toast from App.api
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '&#9889; Ask Claude';
      }
    }
  },

  // ── Batch generate missing visual tags ──

  async generateMissingVisuals() {
    const missing = Characters.characters.filter(ch => !ch.visual_tag);
    if (missing.length === 0) {
      App.showToast('All characters already have visual tags', 'info');
      return;
    }

    const names = missing.map(ch => ch.display_name || ch.character_id).join(', ');
    App.openModal(`
      <h3 class="text-base font-semibold text-gray-100 mb-3">Generate Missing Visual Tags</h3>
      <p class="text-sm text-gray-400 mb-3">
        <strong>${missing.length}</strong> character(s) are missing visual tags:
      </p>
      <div class="mb-4 p-3 bg-gray-800/60 rounded border border-gray-700 text-sm text-gray-300 max-h-32 overflow-y-auto">
        ${missing.map(ch => `<div class="mb-1">&bull; <span class="text-gray-200">${App.escapeHtml(ch.display_name || ch.character_id)}</span> <span class="text-gray-600">${App.escapeHtml(ch.role || '')}</span></div>`).join('')}
      </div>
      <p class="text-xs text-gray-500 mb-4">
        Claude will infer physical appearance from the source text, world bible, and character roles.
        Existing characters' visual tags will be used to ensure visual contrast.
      </p>
      <div class="flex justify-end gap-2">
        <button onclick="App.closeModal()" class="text-sm text-gray-400 hover:text-gray-200 px-3 py-1">Cancel</button>
        <button onclick="Characters._doGenerateMissingVisuals()"
                id="gen-visuals-btn"
                class="bg-purple-700 hover:bg-purple-600 text-white text-sm px-4 py-1.5 rounded transition-colors">
          Generate (${missing.length} characters)
        </button>
      </div>
    `);
  },

  async _doGenerateMissingVisuals() {
    const btn = document.getElementById('gen-visuals-btn');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="animate-pulse">Claude is generating...</span>';
    }

    try {
      const res = await App.api('POST', `/api/${Characters.slug}/characters/generate-missing-visuals`);
      App.closeModal();
      App.showToast(`Visual tags generated for ${res.updated} character(s) ($${res.cost_usd?.toFixed(4) || '?'})`, 'success');
      Characters.characters = await App.api('GET', `/api/${Characters.slug}/characters`);
      Characters._render();
    } catch (e) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Generate';
      }
    }
  },

  // ── Regenerate visual tags (for existing characters) ──

  async regenerateVisuals() {
    const withTags = Characters.characters.filter(ch => ch.visual_tag);
    if (withTags.length === 0) {
      App.showToast('No characters have visual tags to regenerate', 'info');
      return;
    }

    App.openModal(`
      <h3 class="text-base font-semibold text-gray-100 mb-3">Regenerate Visual Tags</h3>
      <p class="text-sm text-gray-400 mb-2">
        Re-derive visual_tag, costume_default, and role from <strong>screenplay dialogue</strong>
        and <strong>world bible</strong>. This fixes characters with wrong occupation, missing gender,
        or non-period clothing.
      </p>
      <div class="mb-3 flex gap-2">
        <button onclick="Characters._regenSelectAll(true)" class="text-xs text-babylon-400 hover:text-babylon-300">Select All</button>
        <button onclick="Characters._regenSelectAll(false)" class="text-xs text-gray-500 hover:text-gray-300">Deselect All</button>
      </div>
      <div class="mb-4 p-3 bg-gray-800/60 rounded border border-gray-700 max-h-48 overflow-y-auto">
        ${withTags.map(ch => `
          <label class="flex items-start gap-2 mb-2 cursor-pointer text-sm">
            <input type="checkbox" class="regen-cb mt-0.5" value="${App.escapeHtml(ch.character_id)}" checked />
            <div>
              <span class="text-gray-200">${App.escapeHtml(ch.display_name || ch.character_id)}</span>
              <span class="text-gray-600 text-xs block">${App.escapeHtml(ch.visual_tag || '').substring(0, 60)}...</span>
            </div>
          </label>
        `).join('')}
      </div>
      <div class="flex justify-end gap-2">
        <button onclick="App.closeModal()" class="text-sm text-gray-400 hover:text-gray-200 px-3 py-1">Cancel</button>
        <button onclick="Characters._doRegenerateVisuals()"
                id="regen-visuals-btn-modal"
                class="bg-amber-700 hover:bg-amber-600 text-white text-sm px-4 py-1.5 rounded transition-colors">
          Regenerate Selected
        </button>
      </div>
    `);
  },

  _regenSelectAll(checked) {
    document.querySelectorAll('.regen-cb').forEach(cb => cb.checked = checked);
  },

  async _doRegenerateVisuals() {
    const cbs = document.querySelectorAll('.regen-cb:checked');
    const ids = Array.from(cbs).map(cb => cb.value);
    if (ids.length === 0) {
      App.showToast('Select at least one character', 'warning');
      return;
    }

    const btn = document.getElementById('regen-visuals-btn-modal');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="animate-pulse">Claude is regenerating...</span>';
    }

    try {
      const res = await App.api('POST', `/api/${Characters.slug}/characters/regenerate-visuals`, {
        character_ids: ids,
      });
      App.closeModal();
      App.showToast(`Visual tags regenerated for ${res.updated} character(s) ($${res.cost_usd?.toFixed(4) || '?'})`, 'success');
      Characters.characters = await App.api('GET', `/api/${Characters.slug}/characters`);
      Characters._render();
    } catch (e) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Regenerate Selected';
      }
    }
  },

  // ── Editable input helpers ──

  _input(id, value, placeholder, opts = {}) {
    const type = opts.type || 'text';
    const cls = 'w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-babylon-500';
    if (type === 'textarea') {
      const rows = opts.rows || 2;
      return `<textarea id="${id}" rows="${rows}" placeholder="${App.escapeHtml(placeholder)}" class="${cls} resize-y">${App.escapeHtml(value || '')}</textarea>`;
    }
    if (type === 'number') {
      return `<input id="${id}" type="number" value="${App.escapeHtml(String(value || ''))}" placeholder="${App.escapeHtml(placeholder)}" class="${cls}" />`;
    }
    return `<input id="${id}" type="text" value="${App.escapeHtml(value || '')}" placeholder="${App.escapeHtml(placeholder)}" class="${cls}" />`;
  },

  _field(label, inputHtml) {
    return `
      <div class="mb-3">
        <label class="text-xs text-gray-500 uppercase mb-1 block">${label}</label>
        ${inputHtml}
      </div>
    `;
  },

  // ── Save all character edits ──

  async saveCharacter(characterId) {
    const val = (id) => {
      const el = document.getElementById(id);
      return el ? el.value.trim() : '';
    };
    const numVal = (id) => {
      const v = val(id);
      return v ? Number(v) : undefined;
    };

    const updates = {
      display_name: val('ce-display-name') || undefined,
      visual_tag: val('ce-visual-tag') || undefined,
      costume_default: val('ce-costume-default') || undefined,
      description: {
        age: numVal('ce-age'),
        role: val('ce-role') || undefined,
        archetype: val('ce-archetype') || undefined,
        physical_appearance: val('ce-physical') || undefined,
        height_cm: numVal('ce-height'),
        personality_traits: val('ce-personality') || undefined,
      },
      narrative: {
        arc: val('ce-arc') || undefined,
      },
      voice: {
        tone: val('ce-voice-tone') || undefined,
        pace: val('ce-voice-pace') || undefined,
        emotion_range: val('ce-voice-emotion') || undefined,
        direction_notes: val('ce-voice-notes') || undefined,
      },
      animation: {
        motion_style: val('ce-anim-motion') || undefined,
        posture: val('ce-anim-posture') || undefined,
        facial_expression_hints: val('ce-anim-facial') || undefined,
      },
    };

    // personality_traits: convert comma-separated string to array
    if (typeof updates.description.personality_traits === 'string') {
      updates.description.personality_traits = updates.description.personality_traits
        .split(',').map(s => s.trim()).filter(Boolean);
      if (updates.description.personality_traits.length === 0) delete updates.description.personality_traits;
    }

    // emotion_range: convert comma-separated string to array
    if (typeof updates.voice.emotion_range === 'string') {
      updates.voice.emotion_range = updates.voice.emotion_range
        .split(',').map(s => s.trim()).filter(Boolean);
      if (updates.voice.emotion_range.length === 0) delete updates.voice.emotion_range;
    }

    // Clean out undefined values from nested objects
    for (const key of ['description', 'narrative', 'voice', 'animation']) {
      const obj = updates[key];
      for (const k in obj) {
        if (obj[k] === undefined) delete obj[k];
      }
      if (Object.keys(obj).length === 0) delete updates[key];
    }
    for (const k in updates) {
      if (updates[k] === undefined) delete updates[k];
    }

    const btn = document.getElementById('ce-save-btn');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Saving...';
    }

    try {
      await App.api('PUT', `/api/${Characters.slug}/character/${characterId}`, updates);
      App.showToast('Character saved', 'success');
      // Refresh cards + keep detail panel open
      Characters.characters = await App.api('GET', `/api/${Characters.slug}/characters`);
      Characters._render();
      Characters.expand(characterId);
    } catch (e) {
      // Error toast from App.api
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Save All Changes';
      }
    }
  },

  // ── Detail panel (fully editable) ──

  // ── Training image gallery ──

  async _loadTrainingImages(characterId) {
    const grid = document.getElementById('char-sheet-grid');
    const countEl = document.getElementById('char-sheet-count');
    const gallery = document.getElementById('char-sheet-gallery');
    if (!grid) return;

    try {
      const images = await App.api('GET', `/api/${Characters.slug}/character/${characterId}/training-images`);
      if (!images || images.length === 0) {
        if (gallery) gallery.classList.add('hidden');
        return;
      }

      if (countEl) countEl.textContent = `${images.length} images`;

      // Group images by category
      const groups = {};
      for (const img of images) {
        let group = 'other';
        if (img.label.startsWith('full body'))       group = 'Full Body Poses';
        else if (img.label.startsWith('closeup'))    group = 'Close-ups';
        else if (img.label.startsWith('medium'))     group = 'Medium Shots';
        if (!groups[group]) groups[group] = [];
        groups[group].push(img);
      }

      // Store for lightbox navigation
      Characters._sheetImages = images;
      Characters._sheetCharId = characterId;

      grid.innerHTML = Object.entries(groups).map(([groupName, imgs]) => `
        <div class="mb-4">
          <div class="text-xs text-gray-500 uppercase mb-2">${App.escapeHtml(groupName)}</div>
          <div class="grid grid-cols-4 sm:grid-cols-6 md:grid-cols-8 lg:grid-cols-10 gap-2">
            ${imgs.map((img, i) => {
              const globalIdx = images.indexOf(img);
              return `
                <div class="group cursor-pointer" onclick="Characters._openLightbox(${globalIdx})">
                  <div class="aspect-square rounded overflow-hidden bg-gray-800 border border-gray-700/50
                              group-hover:border-babylon-500 transition-colors">
                    <img src="/api/${Characters.slug}/character/${characterId}/training-image/${img.filename}"
                         alt="${App.escapeHtml(img.label)}"
                         class="w-full h-full object-cover"
                         loading="lazy" />
                  </div>
                  <div class="text-xs text-gray-600 mt-1 truncate text-center">${App.escapeHtml(img.label)}</div>
                </div>
              `;
            }).join('')}
          </div>
        </div>
      `).join('');

    } catch (e) {
      if (gallery) gallery.classList.add('hidden');
    }
  },

  _sheetImages: [],
  _sheetCharId: null,
  _lightboxIdx: 0,

  _openLightbox(idx) {
    Characters._lightboxIdx = idx;
    Characters._renderLightbox();
  },

  _renderLightbox() {
    const imgs = Characters._sheetImages;
    const idx = Characters._lightboxIdx;
    if (!imgs || idx < 0 || idx >= imgs.length) return;
    const img = imgs[idx];
    const charId = Characters._sheetCharId;

    // Remove existing lightbox
    const existing = document.getElementById('sheet-lightbox');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'sheet-lightbox';
    overlay.className = 'fixed inset-0 z-50 bg-black/90 flex items-center justify-center';
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

    overlay.innerHTML = `
      <div class="relative max-w-4xl w-full mx-4" onclick="event.stopPropagation()">
        <!-- Close -->
        <button onclick="document.getElementById('sheet-lightbox').remove()"
                class="absolute -top-10 right-0 text-gray-400 hover:text-white text-2xl z-10">&times;</button>

        <!-- Image -->
        <div class="bg-gray-900 rounded-lg overflow-hidden border border-gray-700">
          <img src="/api/${Characters.slug}/character/${charId}/training-image/${img.filename}"
               class="w-full max-h-[75vh] object-contain" />
        </div>

        <!-- Caption bar -->
        <div class="mt-3 flex items-center justify-between">
          <button onclick="Characters._lightboxNav(-1)"
                  class="text-gray-400 hover:text-white px-3 py-1 rounded ${idx === 0 ? 'opacity-30 pointer-events-none' : ''}">&larr; Prev</button>
          <div class="text-center">
            <div class="text-sm text-gray-200">${App.escapeHtml(img.label)}</div>
            <div class="text-xs text-gray-600 mt-1">${idx + 1} / ${imgs.length}</div>
            ${img.caption ? `<div class="text-xs text-gray-500 mt-1 max-w-md truncate">${App.escapeHtml(img.caption.substring(0, 120))}</div>` : ''}
          </div>
          <button onclick="Characters._lightboxNav(1)"
                  class="text-gray-400 hover:text-white px-3 py-1 rounded ${idx === imgs.length - 1 ? 'opacity-30 pointer-events-none' : ''}">Next &rarr;</button>
        </div>
      </div>
    `;

    document.body.appendChild(overlay);

    // Keyboard navigation
    const keyHandler = (e) => {
      if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', keyHandler); }
      else if (e.key === 'ArrowLeft') Characters._lightboxNav(-1);
      else if (e.key === 'ArrowRight') Characters._lightboxNav(1);
    };
    document.addEventListener('keydown', keyHandler);
    overlay._keyHandler = keyHandler;
  },

  _lightboxNav(delta) {
    const newIdx = Characters._lightboxIdx + delta;
    if (newIdx < 0 || newIdx >= Characters._sheetImages.length) return;
    Characters._lightboxIdx = newIdx;
    // Remove old keyboard handler
    const old = document.getElementById('sheet-lightbox');
    if (old && old._keyHandler) document.removeEventListener('keydown', old._keyHandler);
    Characters._renderLightbox();
  },

  _renderDetail(panel, char) {
    const desc = char.description || {};
    const narrative = char.narrative || {};
    const voice = char.voice || {};
    const animation = char.animation || {};
    const assets = char.assets || {};
    const cid = char.character_id;

    // Flatten arrays to comma strings for editing
    const personalityStr = Array.isArray(desc.personality_traits) ? desc.personality_traits.join(', ') : (desc.personality_traits || '');
    const emotionStr = Array.isArray(voice.emotion_range) ? voice.emotion_range.join(', ') : (voice.emotion_range || '');

    panel.innerHTML = `
      <div class="card p-5">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-base font-semibold text-gray-100">
            ${App.escapeHtml(char.display_name || cid)}
            <span class="text-xs text-gray-600 ml-2 font-normal">${App.escapeHtml(cid)}</span>
          </h3>
          <div class="flex items-center gap-3">
            <button onclick="Characters.saveCharacter('${App.escapeHtml(cid)}')"
                    id="ce-save-btn"
                    class="bg-babylon-600 hover:bg-babylon-500 text-white text-xs px-4 py-1.5 rounded transition-colors">
              Save All Changes
            </button>
            <button onclick="document.getElementById('character-detail').classList.add('hidden')"
                    class="text-gray-500 hover:text-gray-300 text-sm">Close</button>
          </div>
        </div>

        <!-- AI Feedback Bar -->
        <div class="mb-5 p-3 bg-gray-800/60 rounded-lg border border-gray-700">
          <label class="text-xs text-gray-500 uppercase mb-1.5 block">AI Director &mdash; describe changes in plain language</label>
          <div class="flex gap-2">
            <input type="text" id="ce-ai-feedback"
                   placeholder="e.g., Kobbi is a musician not a craftsman, same age as Bansir, make costume period-accurate to 600 BCE..."
                   class="flex-1 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-gray-200
                          placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-babylon-500" />
            <button onclick="Characters.aiRevise('${App.escapeHtml(cid)}')"
                    id="ce-ai-btn"
                    class="bg-purple-700 hover:bg-purple-600 text-white text-xs px-4 py-2 rounded
                           transition-colors whitespace-nowrap flex items-center gap-1.5">
              &#9889; Ask Claude
            </button>
          </div>
          <div class="text-xs text-gray-600 mt-1.5">Claude sees this character's full profile, the world bible, and scenes they appear in. ~$0.02 per revision.</div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">

          <!-- Column 1: Image + Identity -->
          <div class="space-y-4">
            <!-- Reference Image -->
            <div>
              <div class="flex items-center justify-between mb-2">
                <h4 class="text-xs font-semibold text-gray-500 uppercase">Reference Image</h4>
                <button onclick="Characters.generateReference('${App.escapeHtml(cid)}')"
                        class="text-xs text-babylon-400 hover:text-babylon-300">
                  ${assets.reference_image ? 'Regenerate' : 'Generate'}
                </button>
              </div>
              ${assets.reference_image ? `
                <img src="/api/${Characters.slug}/character/${cid}/reference-image?t=${Date.now()}"
                     class="w-full rounded border border-gray-700" />
              ` : `
                <div class="aspect-square bg-gray-800/50 rounded border border-dashed border-gray-700
                            flex items-center justify-center cursor-pointer hover:border-babylon-500 transition-colors"
                     onclick="Characters.generateReference('${App.escapeHtml(cid)}')">
                  <span class="text-gray-600 text-sm">Click to generate</span>
                </div>
              `}
            </div>

            ${Characters._field('Display Name', Characters._input('ce-display-name', char.display_name, 'Display name'))}
            ${Characters._field('Visual Tag (image gen)', Characters._input('ce-visual-tag', char.visual_tag, 'Concise visual descriptor...', { type: 'textarea', rows: 3 }))}
            ${Characters._field('Default Costume', Characters._input('ce-costume-default', char.costume_default, 'Default costume description...', { type: 'textarea', rows: 2 }))}
          </div>

          <!-- Column 2: Description + Narrative -->
          <div class="space-y-4">
            <h4 class="text-xs font-semibold text-gray-500 uppercase">Description</h4>
            <div class="grid grid-cols-2 gap-2">
              ${Characters._field('Age', Characters._input('ce-age', desc.age, 'Age', { type: 'number' }))}
              ${Characters._field('Height (cm)', Characters._input('ce-height', desc.height_cm, 'Height', { type: 'number' }))}
            </div>
            ${Characters._field('Role', Characters._input('ce-role', desc.role, 'Character role in the story...'))}
            ${Characters._field('Archetype', Characters._input('ce-archetype', desc.archetype, 'Character archetype...'))}
            ${Characters._field('Physical Appearance', Characters._input('ce-physical', desc.physical_appearance, 'Physical description...', { type: 'textarea', rows: 3 }))}
            ${Characters._field('Personality Traits (comma-separated)', Characters._input('ce-personality', personalityStr, 'brave, loyal, stubborn...'))}

            <div class="pt-2 border-t border-gray-800">
              <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Narrative</h4>
              ${Characters._field('Character Arc', Characters._input('ce-arc', narrative.arc, 'Character arc description...', { type: 'textarea', rows: 2 }))}
              ${(narrative.chapters || []).length > 0 ? `
                <div class="text-xs text-gray-500 mb-2">Chapters: ${narrative.chapters.join(', ')}</div>
              ` : ''}
              ${narrative.relationships ? `
                <div class="mb-2">
                  <label class="text-xs text-gray-500 uppercase mb-1 block">Relationships</label>
                  <div class="space-y-1">
                    ${Object.entries(narrative.relationships).map(([k, v]) => `
                      <div class="text-xs"><span class="text-gray-500">${App.escapeHtml(k)}:</span> <span class="text-gray-400">${App.escapeHtml(v)}</span></div>
                    `).join('')}
                  </div>
                </div>
              ` : ''}
            </div>
          </div>

          <!-- Column 3: Voice + Animation + Assets -->
          <div class="space-y-4">
            <h4 class="text-xs font-semibold text-gray-500 uppercase">Voice Direction</h4>
            ${Characters._field('Tone', Characters._input('ce-voice-tone', voice.tone, 'Warm, authoritative...'))}
            ${Characters._field('Pace', Characters._input('ce-voice-pace', voice.pace, 'Measured, deliberate...'))}
            ${Characters._field('Emotion Range (comma-separated)', Characters._input('ce-voice-emotion', emotionStr, 'gentle wisdom, firm conviction...'))}
            ${Characters._field('Direction Notes', Characters._input('ce-voice-notes', voice.direction_notes, 'Voice acting notes...', { type: 'textarea', rows: 2 }))}

            <div class="pt-2 border-t border-gray-800">
              <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Animation</h4>
              ${Characters._field('Motion Style', Characters._input('ce-anim-motion', animation.motion_style, 'Movement description...'))}
              ${Characters._field('Posture', Characters._input('ce-anim-posture', animation.posture, 'Posture description...'))}
              ${Characters._field('Facial Expressions', Characters._input('ce-anim-facial', animation.facial_expression_hints, 'Expression hints...', { type: 'textarea', rows: 2 }))}
            </div>

            ${(assets.costume_variants || []).length > 0 || (assets.signature_props || []).length > 0 ? `
              <div class="pt-2 border-t border-gray-800">
                <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Assets</h4>
                ${(assets.costume_variants || []).length > 0 ? `
                  <div class="mb-2">
                    <div class="text-xs text-gray-500 mb-1">Costume Variants</div>
                    <div class="flex flex-wrap gap-1">
                      ${assets.costume_variants.map(c => `<span class="badge badge-draft">${App.escapeHtml(typeof c === 'string' ? c : c.label || JSON.stringify(c))}</span>`).join('')}
                    </div>
                  </div>
                ` : ''}
                ${(assets.signature_props || []).length > 0 ? `
                  <div>
                    <div class="text-xs text-gray-500 mb-1">Signature Props</div>
                    <div class="flex flex-wrap gap-1">
                      ${assets.signature_props.map(p => `<span class="badge badge-draft">${App.escapeHtml(typeof p === 'string' ? p : p.label || JSON.stringify(p))}</span>`).join('')}
                    </div>
                  </div>
                ` : ''}
              </div>
            ` : ''}
          </div>
        </div>

        <!-- LoRA Training Section -->
        <div id="lora-section" class="mt-6 pt-4 border-t border-gray-800">
          <div class="flex items-center justify-between mb-3">
            <h4 class="text-xs font-semibold text-gray-500 uppercase">LoRA Training</h4>
            <div id="lora-status-badge" class="text-xs text-gray-600">Checking...</div>
          </div>
          <div id="lora-status-panel" class="text-xs text-gray-600">Loading status...</div>
        </div>

        <!-- Character Sheet Gallery (full width below columns) -->
        <div id="char-sheet-gallery" class="mt-6 pt-4 border-t border-gray-800">
          <div class="flex items-center justify-between mb-3">
            <h4 class="text-xs font-semibold text-gray-500 uppercase">Character Sheets</h4>
            <span id="char-sheet-count" class="text-xs text-gray-600"></span>
          </div>
          <div id="char-sheet-grid" class="text-xs text-gray-600">Loading...</div>
        </div>
      </div>
    `;

    // Load training images and LoRA status asynchronously
    Characters._loadTrainingImages(cid);
    Characters._loadLoraStatus(cid);
  },

  // ── LoRA training ──

  async _loadLoraStatus(characterId) {
    const panel = document.getElementById('lora-status-panel');
    const badge = document.getElementById('lora-status-badge');
    if (!panel) return;

    try {
      const s = await App.api('GET', `/api/${Characters.slug}/character/${characterId}/lora-status`);
      Characters._loraStatus = s;

      // Badge
      if (badge) {
        if (s.local_exists && s.in_comfyui) {
          badge.innerHTML = '<span class="badge" style="background:rgba(16,185,129,0.15);color:#34d399;border-color:rgba(16,185,129,0.3)">Ready</span>';
        } else if (s.local_exists && !s.in_comfyui) {
          badge.innerHTML = '<span class="badge" style="background:rgba(245,158,11,0.15);color:#fbbf24;border-color:rgba(245,158,11,0.3)">Not in ComfyUI</span>';
        } else if (s.ready_to_train) {
          badge.innerHTML = '<span class="badge badge-draft">Not trained</span>';
        } else {
          badge.innerHTML = '<span class="text-xs text-gray-600">Need training images first</span>';
        }
      }

      // Status panel
      let html = '<div class="grid grid-cols-2 gap-3 mb-3">';

      html += `<div class="p-2 bg-gray-800/50 rounded border border-gray-700/50">
        <div class="text-xs text-gray-500 mb-1">Local File</div>
        <div class="text-sm ${s.local_exists ? 'text-green-400' : 'text-gray-600'}">
          ${s.local_exists ? `${s.safetensors_name} (${s.local_size_mb} MB)` : 'Not found'}
        </div>
      </div>`;

      html += `<div class="p-2 bg-gray-800/50 rounded border border-gray-700/50">
        <div class="text-xs text-gray-500 mb-1">ComfyUI</div>
        <div class="text-sm ${s.in_comfyui ? 'text-green-400' : s.comfyui_available ? 'text-yellow-400' : 'text-gray-600'}">
          ${s.in_comfyui ? 'Deployed' : s.comfyui_available ? 'Not deployed' : 'ComfyUI offline'}
        </div>
      </div>`;

      html += `<div class="p-2 bg-gray-800/50 rounded border border-gray-700/50">
        <div class="text-xs text-gray-500 mb-1">Training Images</div>
        <div class="text-sm ${s.ready_to_train ? 'text-gray-200' : 'text-yellow-400'}">
          ${s.training_images_count} images ${s.ready_to_train ? '' : '(need 10+)'}
        </div>
      </div>`;

      html += `<div class="p-2 bg-gray-800/50 rounded border border-gray-700/50">
        <div class="text-xs text-gray-500 mb-1">Trigger Word</div>
        <div class="text-sm text-babylon-300 font-mono">${s.trigger_word || '—'}</div>
      </div>`;

      if (s.trained_at) {
        html += `<div class="col-span-2 p-2 bg-gray-800/50 rounded border border-gray-700/50">
          <div class="text-xs text-gray-500 mb-1">Last Trained</div>
          <div class="text-sm text-gray-300">${new Date(s.trained_at).toLocaleString()}</div>
        </div>`;
      }

      html += '</div>';

      // Action buttons
      html += '<div class="flex items-center gap-2">';

      if (s.ready_to_train) {
        const trainLabel = s.local_exists ? 'Retrain LoRA' : 'Train LoRA';
        const trainClass = s.local_exists
          ? 'bg-amber-700 hover:bg-amber-600'
          : 'bg-babylon-600 hover:bg-babylon-500';

        html += `<button onclick="Characters.trainLora('${App.escapeHtml(characterId)}', ${s.local_exists})"
                         id="train-lora-btn"
                         class="${trainClass} text-white text-xs px-4 py-1.5 rounded transition-colors">
          ${trainLabel}
        </button>`;
      }

      if (s.local_exists && !s.in_comfyui && s.comfyui_available) {
        html += `<span class="text-xs text-yellow-400">Training will redeploy automatically</span>`;
      }

      if (!s.ready_to_train) {
        html += `<span class="text-xs text-gray-500">Run Character Sheets stage first to generate training images</span>`;
      }

      html += '</div>';
      panel.innerHTML = html;

    } catch (e) {
      panel.innerHTML = '<div class="text-gray-600 text-xs">Could not load LoRA status</div>';
    }
  },

  _loraStatus: null,

  async trainLora(characterId, isRetrain) {
    const action = isRetrain ? 'retrain' : 'train';
    const msg = isRetrain
      ? `Retrain LoRA for this character? This will overwrite the existing LoRA and takes 15-30 minutes.`
      : `Train a LoRA for this character? This takes 15-30 minutes and requires GPU memory.`;

    App.openModal(`
      <h3 class="text-base font-semibold text-gray-100 mb-3">
        ${isRetrain ? 'Retrain' : 'Train'} LoRA &mdash; ${App.escapeHtml(characterId)}
      </h3>
      <p class="text-sm text-gray-400 mb-4">${msg}</p>
      <div class="text-xs text-gray-500 mb-4">
        The trained LoRA will be automatically deployed to ComfyUI for use in storyboard generation.
        Make sure no other GPU-heavy tasks are running.
      </div>
      <div class="flex justify-end gap-2">
        <button onclick="App.closeModal()" class="text-sm text-gray-400 hover:text-gray-200 px-3 py-1">Cancel</button>
        <button onclick="Characters._doTrainLora('${App.escapeHtml(characterId)}', ${isRetrain})"
                id="train-lora-confirm-btn"
                class="${isRetrain ? 'bg-amber-700 hover:bg-amber-600' : 'bg-babylon-600 hover:bg-babylon-500'} text-white text-sm px-4 py-1.5 rounded transition-colors">
          ${isRetrain ? 'Retrain' : 'Train'} (Free / Local GPU)
        </button>
      </div>
    `);
  },

  async _doTrainLora(characterId, force) {
    const btn = document.getElementById('train-lora-confirm-btn');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Starting...';
    }

    try {
      const res = await App.api('POST', `/api/${Characters.slug}/stages/run`, {
        stage: 'lora_training',
        character_id: characterId,
        force: force,
      });
      App.closeModal();
      App.showToast(`LoRA training started (job ${res.job_id}). This takes 15-30 min.`, 'success');

      // Update the train button to show in-progress
      const trainBtn = document.getElementById('train-lora-btn');
      if (trainBtn) {
        trainBtn.disabled = true;
        trainBtn.innerHTML = '<span class="animate-pulse">Training...</span>';
        trainBtn.className = 'bg-gray-700 text-gray-400 text-xs px-4 py-1.5 rounded cursor-not-allowed';
      }

      // Listen to SSE stream for completion
      Characters._watchTrainingJob(res.job_id, characterId);
    } catch (e) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = force ? 'Retrain' : 'Train';
      }
    }
  },

  _watchTrainingJob(jobId, characterId) {
    const evtSource = new EventSource(`/api/stream/${jobId}`);
    evtSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.heartbeat) return;

        // Update button with progress
        const trainBtn = document.getElementById('train-lora-btn');
        if (trainBtn && data.progress !== undefined) {
          trainBtn.innerHTML = `<span class="animate-pulse">Training... ${data.progress}%</span>`;
        }

        if (data.done) {
          evtSource.close();
          if (data.status === 'complete') {
            App.showToast('LoRA training complete!', 'success');
          } else {
            App.showToast(`LoRA training failed: ${data.error || 'unknown error'}`, 'error');
          }
          // Refresh LoRA status and character list
          Characters._loadLoraStatus(characterId);
          Characters.init(Characters.slug);
        }
      } catch (e) { /* ignore parse errors */ }
    };
    evtSource.onerror = () => {
      evtSource.close();
      // Refresh anyway in case it completed
      Characters._loadLoraStatus(characterId);
    };
  },
};
