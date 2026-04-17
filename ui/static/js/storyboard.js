/**
 * storyboard.js — Shot-by-shot storyboard review with keyboard navigation.
 *
 * Keyboard shortcuts:
 *   ← / → : Previous / Next shot
 *   A     : Approve current shot
 *   R     : Reject current shot (prompts for notes)
 *   G     : Regenerate storyboard
 *   P     : Play all audio for current shot
 *   V     : Toggle 16:9 / 9:16 aspect ratio
 *   Space : Play / Pause / Resume chapter playback
 *   Escape: Stop chapter playback
 */

const Storyboard = {

  slug: null,
  chapterId: null,
  shots: [],       // [{chapter_id, scene_id, shot_id, ...}]
  currentIdx: 0,
  showVertical: false,  // Toggle between 16:9 and 9:16
  commonSeed: null,     // Project-level common seed (null = disabled)

  async init(slug) {
    Storyboard.slug = slug;

    // Load common seed setting
    Storyboard._loadCommonSeed();

    // Populate chapter dropdown
    try {
      const status = await App.api('GET', `/api/${slug}/status`);
      const sel = document.getElementById('sb-chapter-select');
      const chapters = status.chapters || [];
      sel.innerHTML = '<option value="">Select chapter...</option>' +
        chapters.map(ch =>
          `<option value="${ch.chapter_id}">${App.escapeHtml(ch.title || ch.chapter_id)}</option>`
        ).join('');
    } catch (e) {
      // leave default
    }

    // Auto-select chapter from URL param
    const urlChapter = new URLSearchParams(window.location.search).get('chapter');
    if (urlChapter) {
      const sel = document.getElementById('sb-chapter-select');
      if (sel && [...sel.options].some(o => o.value === urlChapter)) {
        sel.value = urlChapter;
        Storyboard.load();
      }
    }

    // Keyboard listener
    document.addEventListener('keydown', Storyboard._onKey);
  },

  async load() {
    const chapterId = document.getElementById('sb-chapter-select').value;
    if (!chapterId) return;

    Storyboard.chapterId = chapterId;
    Storyboard.shots = [];
    Storyboard.currentIdx = 0;

    try {
      const detail = await App.api('GET', `/api/${Storyboard.slug}/chapter/${chapterId}`);
      const scenes = detail.scenes || [];

      // Populate scene dropdown
      const sceneSel = document.getElementById('sb-scene-select');
      sceneSel.innerHTML = '<option value="">All scenes</option>' +
        scenes.map(sc =>
          `<option value="${sc.scene_id}">${App.escapeHtml(sc.scene_id)} — ${App.escapeHtml(sc.title || '')}</option>`
        ).join('');

      // Gather all shots with per-shot status
      Storyboard.shots = [];
      for (const sc of scenes) {
        const shotIds = (sc.shots && sc.shots.shot_ids) || [];
        const shotStatus = (sc.shots && sc.shots.shot_status) || {};
        for (const sid of shotIds) {
          const st = shotStatus[sid] || {};
          Storyboard.shots.push({
            chapter_id: chapterId,
            scene_id: sc.scene_id,
            shot_id: sid,
            storyboard_approved: st.storyboard_approved || false,
            has_flags: (st.flags && st.flags.length > 0) || false,
          });
        }
      }

      Storyboard._renderFilmstrip();
      Storyboard._showShot(0);
    } catch (e) {
      document.getElementById('sb-image-container').innerHTML =
        '<div class="text-red-400">Failed to load chapter data</div>';
    }
  },

  async loadScene() {
    const sceneId = document.getElementById('sb-scene-select').value;
    if (!sceneId) {
      // Reload all scenes
      Storyboard.load();
      return;
    }

    // Reload full chapter, then filter to the selected scene
    await Storyboard.load();
    Storyboard.shots = Storyboard.shots.filter(s => s.scene_id === sceneId);
    Storyboard.currentIdx = 0;
    Storyboard._renderFilmstrip();
    if (Storyboard.shots.length > 0) {
      Storyboard._showShot(0);
    }
  },

  async _showShot(idx) {
    if (idx < 0 || idx >= Storyboard.shots.length) return;
    Storyboard.currentIdx = idx;

    const s = Storyboard.shots[idx];
    const container = document.getElementById('sb-image-container');
    const infoContainer = document.getElementById('sb-shot-info');
    const counter = document.getElementById('sb-counter');

    if (counter) counter.textContent = `${idx + 1} / ${Storyboard.shots.length}`;

    // Highlight filmstrip
    document.querySelectorAll('.sb-thumb').forEach((el, i) => {
      el.classList.toggle('ring-2', i === idx);
      el.classList.toggle('ring-babylon-500', i === idx);
    });

    // Show image — path matches chapters/{ch}/shots/{shot_id}/storyboard.png or storyboard_vertical.png
    const imgFile = Storyboard.showVertical ? 'storyboard_vertical.png' : 'storyboard.png';
    const imgUrl = `/api/${Storyboard.slug}/image/chapters/${s.chapter_id}/shots/${s.shot_id}/${imgFile}?t=${Date.now()}`;
    container.innerHTML = `<img src="${imgUrl}" alt="${s.shot_id}" class="w-full h-full object-contain"
      onerror="this.parentNode.innerHTML='<div class=\\'flex items-center justify-center h-full text-gray-600\\'>No storyboard image</div>'" />`;

    // Update aspect ratio of container
    Storyboard._updateAspectContainer();

    // Scroll filmstrip thumb into view
    const thumb = document.querySelector(`.sb-thumb[data-idx="${idx}"]`);
    if (thumb) thumb.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

    // Load shot details
    try {
      const shot = await App.api('GET', `/api/${Storyboard.slug}/shot/${s.chapter_id}/${s.scene_id}/${s.shot_id}`);
      Storyboard._renderShotInfo(shot, s);
    } catch (e) {
      infoContainer.innerHTML = '<div class="text-xs text-red-400">Failed to load shot details</div>';
    }
  },

  _renderShotInfo(shot, ref) {
    const container = document.getElementById('sb-shot-info');
    if (!container) return;

    const sb = shot.storyboard || {};
    const cin = shot.cinematic || {};
    const isApproved = sb.approved === true;
    const isReviewed = sb.reviewed === true;
    const dialogue = shot.dialogue_in_shot || [];
    const chars = shot.characters_in_frame || [];
    const prompt = sb.storyboard_prompt || '';
    const compNotes = cin.composition_notes || '';

    // --- Header: shot ID, label, approve/reject ---
    let html = `
      <div class="card p-3 mb-2">
        <div class="flex items-center justify-between mb-2">
          <div>
            <span class="text-sm font-medium text-gray-200">${App.escapeHtml(ref.shot_id)}</span>
            <span class="text-xs text-gray-500 ml-2">${App.escapeHtml(ref.scene_id)}</span>
          </div>
          <div class="flex gap-2">
            <button onclick="Storyboard.review('approve')"
                    class="${isApproved ? 'bg-green-600' : 'bg-green-800 hover:bg-green-700'} text-white text-xs px-3 py-1 rounded transition-colors">
              ${isApproved ? '&#10003; Approved' : 'Approve (A)'}
            </button>
            <button onclick="Storyboard.review('reject')"
                    class="${!isApproved && isReviewed ? 'bg-red-600' : 'bg-red-800 hover:bg-red-700'} text-white text-xs px-3 py-1 rounded transition-colors">
              Reject (R)
            </button>
          </div>
        </div>
        ${shot.label ? `<p class="text-sm text-gray-300">${App.escapeHtml(shot.label)}</p>` : ''}
        <div class="flex gap-3 text-xs text-gray-500 mt-1">
          ${cin.shot_type ? `<span class="bg-gray-800 px-1.5 py-0.5 rounded">${App.escapeHtml(cin.shot_type)}</span>` : ''}
          ${cin.framing ? `<span class="bg-gray-800 px-1.5 py-0.5 rounded">${App.escapeHtml(cin.framing)}</span>` : ''}
          ${cin.camera_movement ? `<span class="bg-gray-800 px-1.5 py-0.5 rounded">${App.escapeHtml(cin.camera_movement.type || '')}</span>` : ''}
          ${cin.lens_mm_equiv ? `<span class="bg-gray-800 px-1.5 py-0.5 rounded">${cin.lens_mm_equiv}mm</span>` : ''}
          ${shot.duration_sec ? `<span class="bg-gray-800 px-1.5 py-0.5 rounded">${shot.duration_sec}s</span>` : ''}
        </div>
      </div>
    `;

    // --- Screenplay Context Panel ---
    html += '<div class="card p-3">';
    html += '<h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Screenplay Context</h4>';

    // Characters in frame
    if (chars.length > 0) {
      html += `<div class="flex items-center gap-2 mb-3">
        <span class="text-xs text-gray-500">In frame:</span>
        ${chars.map(c => `<span class="text-xs bg-blue-900/40 text-blue-300 px-2 py-0.5 rounded-full">${App.escapeHtml(c.character_id || c)}</span>`).join('')}
      </div>`;
    }

    // Dialogue lines — formatted like screenplay
    if (dialogue.length > 0) {
      html += '<div class="screenplay-container bg-gray-800/50 rounded p-3 mb-3 text-sm">';
      for (const line of dialogue) {
        const escaped = App.escapeHtml(line);
        // Try to split "Character - dialogue" or "Character V.O. - dialogue"
        const dashIdx = escaped.indexOf(' - ');
        if (dashIdx > 0 && dashIdx < 40) {
          const speaker = escaped.substring(0, dashIdx).trim();
          const text = escaped.substring(dashIdx + 3).trim();
          html += `<div class="sp-character text-xs" style="margin-top:0.25rem">${speaker}</div>`;
          html += `<div class="sp-dialogue text-sm" style="padding:0 2rem">${text}</div>`;
        } else {
          // Plain dialogue line — show as dialogue
          html += `<div class="sp-dialogue text-sm" style="padding:0 2rem">${escaped}</div>`;
        }
      }
      html += '</div>';
    } else {
      html += '<div class="text-xs text-gray-600 italic mb-3">No dialogue in this shot</div>';
    }

    // Audio player — rendered async after we know the actual files
    const audioData = shot.audio || {};
    if (audioData.status === 'generated') {
      html += '<div id="sb-audio-panel" class="mb-3"><div class="text-xs text-gray-500">Loading audio...</div></div>';
    }

    // Visual direction (storyboard prompt) — collapsible
    if (prompt) {
      html += `
        <details>
          <summary class="text-xs text-gray-500 cursor-pointer hover:text-gray-400 select-none">
            &#9656; Visual Direction (image prompt)
          </summary>
          <div class="mt-2 text-xs text-gray-400 bg-gray-800/50 rounded p-2 leading-relaxed">
            ${App.escapeHtml(prompt)}
          </div>
        </details>
      `;
    }

    // Composition notes
    if (compNotes) {
      html += `<div class="mt-2 text-xs text-gray-500"><span class="text-gray-600">Composition:</span> ${App.escapeHtml(compNotes)}</div>`;
    }

    // Generation metadata
    const meta = sb.generation_meta;
    if (meta) {
      const provLabel = meta.provider === 'comfyui' ? 'ComfyUI Local'
                      : meta.provider === 'stabilityai' ? 'Stability AI'
                      : meta.provider === 'google_imagen' ? 'Google Imagen'
                      : meta.provider || 'Unknown';
      const modelLabel = meta.model || 'unknown';
      const genDate = meta.generated_at ? new Date(meta.generated_at).toLocaleString() : '';
      const provColor = meta.provider === 'comfyui' ? 'bg-green-900/40 text-green-300'
                       : meta.provider === 'stabilityai' ? 'bg-purple-900/40 text-purple-300'
                       : 'bg-blue-900/40 text-blue-300';

      html += `
        <div class="mt-3 pt-2 border-t border-gray-800/50">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="text-[10px] px-1.5 py-0.5 rounded font-mono ${provColor}">
              ${App.escapeHtml(provLabel)} / ${App.escapeHtml(modelLabel)}
            </span>
            ${meta.seed != null ? `<span class="text-[10px] px-1.5 py-0.5 rounded font-mono bg-yellow-900/30 text-yellow-400 cursor-pointer select-all" title="Click to copy seed" onclick="navigator.clipboard.writeText('${meta.seed}');App.showToast('Seed copied','success')">seed: ${meta.seed}</span>` : ''}
            ${meta.cost_usd ? `<span class="text-[10px] text-gray-600">$${meta.cost_usd.toFixed(3)}</span>` : ''}
            ${genDate ? `<span class="text-[10px] text-gray-600">${genDate}</span>` : ''}
          </div>
          <details class="mt-1">
            <summary class="text-[10px] text-gray-600 cursor-pointer hover:text-gray-500 select-none">
              &#9656; Full Generation Prompt
            </summary>
            <div class="mt-1 text-[10px] text-gray-400 bg-gray-800/50 rounded p-2 leading-relaxed font-mono whitespace-pre-wrap max-h-40 overflow-y-auto">
              ${App.escapeHtml(meta.final_prompt || '(not recorded)')}
            </div>
            ${meta.negative_prompt ? `
              <div class="mt-1 text-[10px] text-gray-600">
                <span class="text-gray-500">Negative:</span> ${App.escapeHtml(meta.negative_prompt)}
              </div>` : ''}
            ${meta.visual_style ? `
              <div class="mt-1 text-[10px] text-gray-600">
                <span class="text-gray-500">Style:</span> ${App.escapeHtml(meta.visual_style)}
              </div>` : ''}
          </details>
        </div>
      `;
    } else if (sb.generated) {
      html += `<div class="mt-2 text-[10px] text-gray-600 italic">No generation metadata (generated before tracking was added)</div>`;
    }

    // Feedback + Regenerate with provider/style selection
    html += `
      <div class="mt-3 pt-3 border-t border-gray-800">
        <div class="flex items-center gap-2 mb-2">
          <input type="text" id="sb-feedback"
                 placeholder="Feedback: e.g., warmer lighting, different angle..."
                 value="${App.escapeHtml(sb.regeneration_feedback || '')}"
                 class="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200
                        placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-babylon-500" />
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <select id="sb-provider-select"
                  class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300
                         focus:outline-none focus:ring-1 focus:ring-babylon-500">
            <option value="auto">Auto</option>
          </select>
          <select id="sb-style-select"
                  class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300
                         focus:outline-none focus:ring-1 focus:ring-babylon-500 max-w-[180px]">
            <option value="">Project default</option>
          </select>
          <div class="flex items-center gap-1">
            <select id="sb-seed-mode"
                    onchange="Storyboard._onSeedModeChange()"
                    class="bg-gray-800 border border-gray-700 rounded px-1 py-1 text-xs text-gray-300
                           focus:outline-none focus:ring-1 focus:ring-yellow-600">
              <option value="common" ${Storyboard.commonSeed != null ? '' : 'disabled'}>Common seed${Storyboard.commonSeed != null ? ' (' + Storyboard.commonSeed + ')' : ''}</option>
              <option value="custom">Custom seed</option>
              <option value="random">Random</option>
            </select>
            <input type="number" id="sb-seed-input"
                   placeholder="Enter seed..."
                   value="${meta && meta.seed != null ? meta.seed : ''}"
                   style="display:none"
                   class="w-28 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-yellow-300
                          placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-yellow-600
                          [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none" />
          </div>
          <div class="flex-1"></div>
          <button onclick="Storyboard.regenerate()"
                  id="sb-regen-btn"
                  class="bg-babylon-700 hover:bg-babylon-600 text-white text-xs px-3 py-1.5 rounded
                         transition-colors whitespace-nowrap flex items-center gap-1">
            &#x21bb; Regenerate ($0.08)
          </button>
        </div>
        ${sb.regenerated_at ? `
          <div class="text-xs text-gray-600 mt-1">Last regenerated: ${new Date(sb.regenerated_at).toLocaleString()}</div>
        ` : ''}
      </div>
    `;

    html += '</div>';

    container.innerHTML = html;

    // Populate provider/style dropdowns + seed mode
    Storyboard._loadProviders();
    Storyboard._initSeedMode();

    // Load audio panel after DOM is set
    if (audioData.status === 'generated') {
      Storyboard._loadAudioPanel(ref.chapter_id, ref.shot_id);
    }
  },

  _providersCache: null,

  async _loadProviders() {
    if (Storyboard._providersCache) {
      Storyboard._populateProviderDropdowns(Storyboard._providersCache);
      return;
    }
    try {
      const data = await App.api('GET', `/api/${Storyboard.slug}/storyboard/providers`);
      Storyboard._providersCache = data;
      Storyboard._populateProviderDropdowns(data);
    } catch (e) {
      // Silently fail — dropdowns will show defaults
    }
  },

  _populateProviderDropdowns(data) {
    const provSel = document.getElementById('sb-provider-select');
    if (provSel && data.providers) {
      provSel.innerHTML = '<option value="auto">Auto</option>' +
        data.providers.map(p =>
          `<option value="${p.id}">${App.escapeHtml(p.label)}</option>`
        ).join('');
    }
    const styleSel = document.getElementById('sb-style-select');
    if (styleSel && data.visual_style_presets) {
      styleSel.innerHTML = data.visual_style_presets.map(s =>
        `<option value="${s.value}">${App.escapeHtml(s.label)}</option>`
      ).join('');
    }
  },

  // ----------------------------------------------------------------
  // Common seed controls
  // ----------------------------------------------------------------

  async _loadCommonSeed() {
    try {
      const data = await App.api('GET', `/api/${Storyboard.slug}/comfyui/common-seed`);
      Storyboard.commonSeed = data.common_seed;
      Storyboard._syncCommonSeedUI();
    } catch (e) {
      // API not available — leave disabled
    }
  },

  _syncCommonSeedUI() {
    const toggle = document.getElementById('sb-common-seed-toggle');
    const input = document.getElementById('sb-common-seed-value');
    const saveBtn = document.getElementById('sb-common-seed-save');
    const randomBtn = document.getElementById('sb-common-seed-random');
    const status = document.getElementById('sb-common-seed-status');
    if (!toggle || !input) return;

    const enabled = Storyboard.commonSeed != null;
    toggle.checked = enabled;
    input.disabled = !enabled;
    saveBtn.disabled = !enabled;
    randomBtn.disabled = !enabled;
    input.value = enabled ? Storyboard.commonSeed : '';
    status.textContent = enabled
      ? `Active — all generations use seed ${Storyboard.commonSeed}`
      : 'Off — each generation uses a random seed';
  },

  toggleCommonSeed() {
    const toggle = document.getElementById('sb-common-seed-toggle');
    if (toggle.checked) {
      // Turning ON — generate a random seed as starting point
      const seed = Math.floor(Math.random() * 2147483647);
      Storyboard.commonSeed = seed;
      Storyboard._syncCommonSeedUI();
      Storyboard.saveCommonSeed();
    } else {
      // Turning OFF — clear the common seed
      Storyboard.commonSeed = null;
      Storyboard._syncCommonSeedUI();
      Storyboard.saveCommonSeed();
    }
  },

  async saveCommonSeed() {
    const input = document.getElementById('sb-common-seed-value');
    const val = input?.value?.trim();
    const seed = Storyboard.commonSeed != null
      ? (val ? parseInt(val, 10) : Storyboard.commonSeed)
      : null;

    if (seed != null && isNaN(seed)) {
      App.showToast('Seed must be a number', 'error');
      return;
    }

    try {
      await App.api('POST', `/api/${Storyboard.slug}/comfyui/common-seed`, { common_seed: seed });
      Storyboard.commonSeed = seed;
      Storyboard._syncCommonSeedUI();
      App.showToast(seed != null ? `Common seed saved: ${seed}` : 'Common seed cleared', 'success');
    } catch (e) {
      App.showToast('Failed to save common seed', 'error');
    }
  },

  randomizeCommonSeed() {
    const seed = Math.floor(Math.random() * 2147483647);
    Storyboard.commonSeed = seed;
    const input = document.getElementById('sb-common-seed-value');
    if (input) input.value = seed;
    Storyboard.saveCommonSeed();
  },

  _initSeedMode() {
    const mode = document.getElementById('sb-seed-mode');
    if (!mode) return;
    // Default: common if active, otherwise random
    mode.value = Storyboard.commonSeed != null ? 'common' : 'random';
    Storyboard._onSeedModeChange();
  },

  _onSeedModeChange() {
    const mode = document.getElementById('sb-seed-mode');
    const input = document.getElementById('sb-seed-input');
    if (!mode || !input) return;

    if (mode.value === 'custom') {
      input.style.display = '';
      input.focus();
    } else {
      input.style.display = 'none';
    }
  },

  /** Resolve the effective seed for the current regeneration based on the dropdown. */
  _getRegenSeed() {
    const mode = document.getElementById('sb-seed-mode');
    if (!mode) return undefined;

    if (mode.value === 'common' && Storyboard.commonSeed != null) {
      return Storyboard.commonSeed;
    }
    if (mode.value === 'custom') {
      const input = document.getElementById('sb-seed-input');
      const val = input?.value?.trim();
      if (val) {
        const n = parseInt(val, 10);
        if (!isNaN(n)) return n;
      }
      return undefined; // empty custom = random
    }
    return undefined; // random
  },

  toggleAspect() {
    Storyboard.showVertical = !Storyboard.showVertical;
    Storyboard._updateAspectContainer();
    // Update toggle button text
    const btn = document.getElementById('sb-aspect-btn');
    if (btn) btn.innerHTML = Storyboard.showVertical ? '&#x25af; 9:16' : '&#x25ad; 16:9';
    // Re-show current shot image
    if (Storyboard.shots.length > 0) {
      const s = Storyboard.shots[Storyboard.currentIdx];
      const imgFile = Storyboard.showVertical ? 'storyboard_vertical.png' : 'storyboard.png';
      const imgUrl = `/api/${Storyboard.slug}/image/chapters/${s.chapter_id}/shots/${s.shot_id}/${imgFile}?t=${Date.now()}`;
      const container = document.getElementById('sb-image-container');
      if (container) {
        container.innerHTML = `<img src="${imgUrl}" alt="${s.shot_id}" class="w-full h-full object-contain"
          onerror="this.parentNode.innerHTML='<div class=\\'flex items-center justify-center h-full text-gray-600\\'>No vertical image</div>'" />`;
      }
    }
  },

  _updateAspectContainer() {
    const container = document.getElementById('sb-image-container');
    if (!container) return;
    if (Storyboard.showVertical) {
      container.classList.remove('aspect-video');
      container.style.aspectRatio = '9/16';
      container.style.maxHeight = '70vh';
      container.style.width = 'auto';
      container.style.margin = '0 auto';
    } else {
      container.classList.add('aspect-video');
      container.style.aspectRatio = '';
      container.style.maxHeight = '';
      container.style.width = '';
      container.style.margin = '';
    }
  },

  _renderFilmstrip() {
    const container = document.getElementById('sb-filmstrip');
    if (!container) return;

    if (Storyboard.shots.length === 0) {
      container.innerHTML = '<div class="text-xs text-gray-600">No shots found</div>';
      return;
    }

    container.innerHTML = Storyboard.shots.map((s, i) => {
      const imgUrl = `/api/${Storyboard.slug}/image/chapters/${s.chapter_id}/shots/${s.shot_id}/storyboard.png?t=${Date.now()}`;
      const borderCls = s.storyboard_approved ? 'border-green-500' : s.has_flags ? 'border-red-500' : 'border-gray-700';
      const indicator = s.storyboard_approved
        ? '<span class="absolute top-0.5 right-0.5 w-3 h-3 bg-green-500 rounded-full flex items-center justify-center text-[8px] text-white leading-none">&#10003;</span>'
        : s.has_flags
          ? '<span class="absolute top-0.5 right-0.5 w-3 h-3 bg-red-500 rounded-full"></span>'
          : '';
      return `
        <div class="sb-thumb cursor-pointer rounded overflow-hidden border ${borderCls} transition-all relative"
             data-idx="${i}"
             onclick="Storyboard._showShot(${i})">
          ${indicator}
          <div class="w-full aspect-video bg-gray-800">
            <img src="${imgUrl}" alt="${s.shot_id}" class="w-full h-full object-cover"
                 onerror="this.style.display='none'" />
          </div>
          <div class="text-xs text-gray-500 px-1 py-0.5 truncate bg-gray-900">${s.shot_id}</div>
        </div>
      `;
    }).join('');
  },

  async regenerate() {
    if (Storyboard.shots.length === 0) return;
    const s = Storyboard.shots[Storyboard.currentIdx];
    const feedback = document.getElementById('sb-feedback')?.value || '';
    const provider = document.getElementById('sb-provider-select')?.value || 'auto';
    const styleSelect = document.getElementById('sb-style-select');
    const visualStyleOverride = (styleSelect && styleSelect.selectedIndex > 0) ? styleSelect.value : undefined;
    const seedValue = Storyboard._getRegenSeed();

    const providerLabel = provider === 'auto' ? 'Auto (local first)'
      : provider === 'comfyui' ? 'ComfyUI Local'
      : provider === 'stabilityai' ? 'Stability AI'
      : provider === 'google_imagen' ? 'Google Imagen' : provider;

    const costEst = (provider === 'comfyui' || provider === 'auto') ? 'Free (local)' : '~$0.08';
    const seedNote = seedValue != null ? `\nSeed: ${seedValue}` : '\nSeed: random';
    if (!confirm(`Regenerate storyboard for ${s.shot_id}?\nProvider: ${providerLabel}\nCost: ${costEst}${seedNote}${feedback ? '\nFeedback: ' + feedback : ''}`)) return;

    const btn = document.getElementById('sb-regen-btn');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="animate-pulse">Generating...</span>';
    }

    try {
      const body = {
        chapter_id: s.chapter_id,
        scene_id: s.scene_id,
        feedback,
        provider,
      };
      if (visualStyleOverride !== undefined) {
        body.visual_style_override = visualStyleOverride;
      }
      if (seedValue != null) {
        body.seed = seedValue;
      }
      const result = await App.api('POST', `/api/${Storyboard.slug}/shot/${s.shot_id}/regenerate`, body);
      const usedProvider = result.provider === 'comfyui' ? 'ComfyUI Local'
        : result.provider === 'stabilityai' ? 'Stability AI'
        : result.provider === 'google_imagen' ? 'Google Imagen' : result.provider || providerLabel;
      const seedMsg = result.seed != null ? ` | seed: ${result.seed}` : '';
      App.showToast(`Storyboard regenerated (${usedProvider}${seedMsg})`, 'success');
      Storyboard._showShot(Storyboard.currentIdx);
      Storyboard._renderFilmstrip();
    } catch (e) {
      // Error toast handled by App.api
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '&#x21bb; Regenerate ($0.08)';
      }
    }
  },

  async review(action) {
    if (Storyboard.shots.length === 0) return;
    const s = Storyboard.shots[Storyboard.currentIdx];

    let notes = '';
    if (action === 'reject') {
      notes = prompt('Rejection notes (optional):') || '';
    }

    try {
      await App.api('POST', `/api/${Storyboard.slug}/shot/${s.shot_id}/review`, {
        action,
        chapter_id: s.chapter_id,
        scene_id: s.scene_id,
        notes,
      });
      App.showToast(`Shot ${action}d`, action === 'approve' ? 'success' : 'warning');

      // Update local shot status and re-render filmstrip indicator
      s.storyboard_approved = (action === 'approve');
      if (action === 'reject') s.has_flags = true;
      Storyboard._renderFilmstrip();

      // Refresh current shot info
      Storyboard._showShot(Storyboard.currentIdx);

      // Auto-advance to next shot on approve
      if (action === 'approve' && Storyboard.currentIdx < Storyboard.shots.length - 1) {
        setTimeout(() => Storyboard._showShot(Storyboard.currentIdx + 1), 300);
      }
    } catch (e) {
      // Error toast already shown
    }
  },

  _onKey(e) {
    // Don't capture when typing in inputs
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
    // Don't capture when modal is open
    if (document.getElementById('app-modal')) return;

    switch (e.key) {
      case 'ArrowLeft':
        e.preventDefault();
        Storyboard._showShot(Storyboard.currentIdx - 1);
        break;
      case 'ArrowRight':
        e.preventDefault();
        Storyboard._showShot(Storyboard.currentIdx + 1);
        break;
      case 'a':
      case 'A':
        e.preventDefault();
        Storyboard.review('approve');
        break;
      case 'r':
      case 'R':
        e.preventDefault();
        Storyboard.review('reject');
        break;
      case 'g':
      case 'G':
        e.preventDefault();
        Storyboard.regenerate();
        break;
      case 'p':
      case 'P':
        e.preventDefault();
        Storyboard.playAllAudio();
        break;
      case 'v':
      case 'V':
        e.preventDefault();
        Storyboard.toggleAspect();
        break;
      case ' ':
        e.preventDefault();
        Storyboard.playChapter();
        break;
      case 'Escape':
        e.preventDefault();
        if (Storyboard._timelinePlaying || Storyboard._timelinePaused) {
          Storyboard.stopChapter();
        }
        break;
    }
  },

  // ----------------------------------------------------------------
  // Audio playback — play all lines in a shot sequentially
  // ----------------------------------------------------------------

  _shotAudioUrls: [],
  _currentAudio: null,
  _audioIdx: 0,

  async _loadAudioPanel(chapterId, shotId) {
    const panel = document.getElementById('sb-audio-panel');
    if (!panel) return;

    try {
      const data = await App.api('GET', `/api/${Storyboard.slug}/audio-lines/${chapterId}/${shotId}`);
      const lines = data.lines || [];
      if (lines.length === 0) {
        panel.innerHTML = '<div class="text-xs text-gray-600 italic">No audio files found</div>';
        Storyboard._shotAudioUrls = [];
        return;
      }

      const audioUrls = lines.map(l => l.url);
      Storyboard._shotAudioUrls = audioUrls;

      let html = `<div class="flex items-center gap-2 mb-2">
        <span class="text-xs text-gray-500">Audio</span>
        <span class="badge badge-approved text-[10px]">${lines.length} line${lines.length > 1 ? 's' : ''}</span>
        <button onclick="Storyboard.playAllAudio()"
                id="sb-play-all-btn"
                class="ml-auto px-2 py-0.5 bg-babylon-700 hover:bg-babylon-600 text-white text-xs rounded transition-colors flex items-center gap-1">
          &#9654; Play All (P)
        </button>
        <button onclick="Storyboard.stopAudio()"
                class="px-2 py-0.5 bg-gray-700 hover:bg-gray-600 text-gray-300 text-xs rounded transition-colors">
          &#9632; Stop
        </button>
      </div>`;
      html += `<details>
        <summary class="text-xs text-gray-500 cursor-pointer hover:text-gray-400 select-none mb-1">
          &#9656; Individual lines
        </summary>
        <div class="space-y-1 mt-1">`;
      for (const line of lines) {
        html += `<div class="flex items-center gap-2">
          <audio controls preload="none" class="h-7 flex-1" style="max-width:100%">
            <source src="${line.url}" type="audio/mpeg" />
          </audio>
          <span class="text-[10px] text-gray-600 truncate max-w-[120px]">${App.escapeHtml(line.filename)}</span>
        </div>`;
      }
      html += '</div></details>';
      panel.innerHTML = html;
    } catch (e) {
      panel.innerHTML = '<div class="text-xs text-gray-600 italic">Could not load audio</div>';
      Storyboard._shotAudioUrls = [];
    }
  },


  playAllAudio() {
    const urls = Storyboard._shotAudioUrls;
    if (!urls || urls.length === 0) {
      App.showToast('No audio for this shot', 'warning');
      return;
    }
    Storyboard.stopAudio();
    Storyboard._audioIdx = 0;
    Storyboard._playNext();
  },

  _playNext() {
    const urls = Storyboard._shotAudioUrls;
    const idx = Storyboard._audioIdx;
    if (idx >= urls.length) {
      Storyboard._currentAudio = null;
      const btn = document.getElementById('sb-play-all-btn');
      if (btn) btn.innerHTML = '&#9654; Play All (P)';
      return;
    }

    const btn = document.getElementById('sb-play-all-btn');
    if (btn) btn.innerHTML = `&#9654; ${idx + 1}/${urls.length}`;

    const audio = new Audio(urls[idx]);
    Storyboard._currentAudio = audio;
    audio.play();
    audio.onended = () => {
      Storyboard._audioIdx++;
      Storyboard._playNext();
    };
    audio.onerror = () => {
      // Skip broken files
      Storyboard._audioIdx++;
      Storyboard._playNext();
    };
  },

  stopAudio() {
    if (Storyboard._currentAudio) {
      Storyboard._currentAudio.pause();
      Storyboard._currentAudio = null;
    }
    Storyboard._audioIdx = 0;
    const btn = document.getElementById('sb-play-all-btn');
    if (btn) btn.innerHTML = '&#9654; Play All (P)';
  },

  // ----------------------------------------------------------------
  // Chapter playback — walk entire storyboard with synced audio
  // ----------------------------------------------------------------

  _timeline: [],        // [{shot_id, image_url, audio_urls, duration_sec, ...}]
  _timelineIdx: 0,
  _timelineAudioIdx: 0,
  _timelineAudio: null,
  _timelineTimer: null,
  _timelinePlaying: false,
  _timelinePaused: false,

  async playChapter() {
    if (Storyboard._timelinePaused) {
      Storyboard._resumeChapter();
      return;
    }
    if (Storyboard._timelinePlaying) {
      Storyboard._pauseChapter();
      return;
    }

    const chapterId = Storyboard.chapterId;
    if (!chapterId) {
      App.showToast('Select a chapter first', 'warning');
      return;
    }

    // Show loading state
    const btn = document.getElementById('sb-play-chapter-btn');
    if (btn) btn.innerHTML = '<span class="animate-pulse">Loading...</span>';

    try {
      const data = await App.api('GET', `/api/${Storyboard.slug}/chapter-timeline/${chapterId}`);
      Storyboard._timeline = data.timeline || [];
      if (Storyboard._timeline.length === 0) {
        App.showToast('No shots in timeline', 'warning');
        Storyboard._resetChapterBtn();
        return;
      }
    } catch (e) {
      Storyboard._resetChapterBtn();
      return;
    }

    Storyboard._timelinePlaying = true;
    Storyboard._timelinePaused = false;
    Storyboard._timelineIdx = 0;
    Storyboard._timelineAudioIdx = 0;
    Storyboard._updateChapterBtn();
    Storyboard._showProgressBar(true);
    Storyboard._playTimelineShot();
  },

  _playTimelineShot() {
    const tl = Storyboard._timeline;
    const idx = Storyboard._timelineIdx;

    if (idx >= tl.length) {
      Storyboard.stopChapter();
      return;
    }

    const entry = tl[idx];

    // Navigate storyboard to this shot (lightweight — no async API calls)
    Storyboard._showTimelineEntry(entry);

    // Update progress bar
    Storyboard._updateProgress(idx, tl.length);
    Storyboard._updateChapterBtn();

    if (entry.audio_urls && entry.audio_urls.length > 0) {
      // Play audio lines sequentially, then advance
      Storyboard._timelineAudioIdx = 0;
      Storyboard._playTimelineAudio(entry);
    } else {
      // No audio — hold for duration_sec then advance
      const holdMs = (entry.duration_sec || 3) * 1000;
      Storyboard._timelineTimer = setTimeout(() => {
        Storyboard._timelineIdx++;
        Storyboard._playTimelineShot();
      }, holdMs);
    }
  },

  _playTimelineAudio(entry) {
    const aIdx = Storyboard._timelineAudioIdx;
    if (aIdx >= entry.audio_urls.length) {
      // All audio for this shot done — brief pause then next shot
      Storyboard._timelineTimer = setTimeout(() => {
        Storyboard._timelineIdx++;
        Storyboard._playTimelineShot();
      }, 400);
      return;
    }

    const audio = new Audio(entry.audio_urls[aIdx]);
    Storyboard._timelineAudio = audio;

    audio.onended = () => {
      Storyboard._timelineAudioIdx++;
      Storyboard._playTimelineAudio(entry);
    };
    audio.onerror = () => {
      // Skip broken file
      Storyboard._timelineAudioIdx++;
      Storyboard._playTimelineAudio(entry);
    };
    audio.play().catch(() => {
      Storyboard._timelineAudioIdx++;
      Storyboard._playTimelineAudio(entry);
    });
  },

  _pauseChapter() {
    Storyboard._timelinePaused = true;

    // Pause current audio
    if (Storyboard._timelineAudio && !Storyboard._timelineAudio.paused) {
      Storyboard._timelineAudio.pause();
    }

    // Clear any hold timer
    if (Storyboard._timelineTimer) {
      clearTimeout(Storyboard._timelineTimer);
      Storyboard._timelineTimer = null;
    }

    Storyboard._updateChapterBtn();
  },

  _resumeChapter() {
    Storyboard._timelinePaused = false;
    Storyboard._updateChapterBtn();

    // Resume audio if it was paused mid-play
    if (Storyboard._timelineAudio && Storyboard._timelineAudio.paused && Storyboard._timelineAudio.currentTime > 0) {
      Storyboard._timelineAudio.play();
      return;
    }

    // Otherwise restart from current position
    Storyboard._playTimelineShot();
  },

  _showTimelineEntry(entry) {
    // Lightweight shot display for timeline playback — no async API calls.
    // Updates image, filmstrip highlight, and counter directly.
    const shotIdx = Storyboard.shots.findIndex(s => s.shot_id === entry.shot_id);
    if (shotIdx < 0) return;

    Storyboard.currentIdx = shotIdx;

    const container = document.getElementById('sb-image-container');
    const counter = document.getElementById('sb-counter');

    if (counter) counter.textContent = `${shotIdx + 1} / ${Storyboard.shots.length}`;

    // Highlight filmstrip
    document.querySelectorAll('.sb-thumb').forEach((el, i) => {
      el.classList.toggle('ring-2', i === shotIdx);
      el.classList.toggle('ring-babylon-500', i === shotIdx);
    });

    // Set image — swap filename if vertical mode is active
    let baseUrl = entry.image_url;
    if (Storyboard.showVertical) {
      baseUrl = baseUrl.replace('storyboard.png', 'storyboard_vertical.png');
    }
    const imgUrl = `${baseUrl}?t=${Date.now()}`;
    container.innerHTML = `<img src="${imgUrl}" alt="${entry.shot_id}" class="w-full h-full object-contain"
      onerror="this.parentNode.innerHTML='<div class=\\'flex items-center justify-center h-full text-gray-600\\'>No storyboard image</div>'" />`;

    // Enforce aspect ratio (16:9 or 9:16)
    Storyboard._updateAspectContainer();

    // Scroll filmstrip thumb into view
    const thumb = document.querySelector(`.sb-thumb[data-idx="${shotIdx}"]`);
    if (thumb) thumb.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

    // Show lightweight info panel during playback
    const infoContainer = document.getElementById('sb-shot-info');
    if (infoContainer) {
      const audioCount = (entry.audio_urls && entry.audio_urls.length) || 0;
      infoContainer.innerHTML = `
        <div class="card p-3">
          <div class="flex items-center gap-2 mb-1">
            <span class="text-sm font-medium text-gray-200">${App.escapeHtml(entry.shot_id)}</span>
            <span class="text-xs text-gray-500">${App.escapeHtml(entry.scene_id)}</span>
            ${entry.shot_type ? `<span class="text-xs bg-gray-800 px-1.5 py-0.5 rounded text-gray-500">${App.escapeHtml(entry.shot_type)}</span>` : ''}
            ${audioCount > 0 ? `<span class="badge badge-approved text-[10px]">${audioCount} audio</span>` : '<span class="text-[10px] text-gray-600">silent</span>'}
          </div>
          ${entry.label ? `<p class="text-sm text-gray-400">${App.escapeHtml(entry.label)}</p>` : ''}
        </div>`;
    }
  },

  stopChapter() {
    // Stop audio
    if (Storyboard._timelineAudio) {
      Storyboard._timelineAudio.pause();
      Storyboard._timelineAudio = null;
    }

    // Clear timer
    if (Storyboard._timelineTimer) {
      clearTimeout(Storyboard._timelineTimer);
      Storyboard._timelineTimer = null;
    }

    Storyboard._timelinePlaying = false;
    Storyboard._timelinePaused = false;
    Storyboard._timelineIdx = 0;
    Storyboard._timelineAudioIdx = 0;
    Storyboard._timeline = [];
    Storyboard._resetChapterBtn();
    Storyboard._showProgressBar(false);

    // Reload full shot details for the current shot
    if (Storyboard.shots.length > 0) {
      Storyboard._showShot(Storyboard.currentIdx);
    }
  },

  _updateChapterBtn() {
    const btn = document.getElementById('sb-play-chapter-btn');
    if (!btn) return;
    const tl = Storyboard._timeline;
    const idx = Storyboard._timelineIdx;
    const total = tl.length;

    if (Storyboard._timelinePaused) {
      btn.innerHTML = `&#9654; Resume (${idx + 1}/${total})`;
      btn.className = btn.className.replace(/bg-\S+/g, '').trim() + ' bg-yellow-700 hover:bg-yellow-600';
    } else if (Storyboard._timelinePlaying) {
      btn.innerHTML = `&#10074;&#10074; Pause (${idx + 1}/${total})`;
      btn.className = btn.className.replace(/bg-\S+/g, '').trim() + ' bg-babylon-700 hover:bg-babylon-600';
    }
  },

  _resetChapterBtn() {
    const btn = document.getElementById('sb-play-chapter-btn');
    if (!btn) return;
    btn.innerHTML = '&#9654; Play Chapter (Space)';
    btn.className = btn.className.replace(/bg-\S+/g, '').trim() + ' bg-babylon-700 hover:bg-babylon-600';
  },

  _showProgressBar(show) {
    const bar = document.getElementById('sb-chapter-progress');
    if (bar) bar.style.display = show ? 'block' : 'none';
    if (!show) {
      const fill = document.getElementById('sb-chapter-progress-fill');
      if (fill) fill.style.width = '0%';
    }
  },

  _updateProgress(idx, total) {
    const fill = document.getElementById('sb-chapter-progress-fill');
    if (fill) {
      const pct = total > 0 ? ((idx + 1) / total) * 100 : 0;
      fill.style.width = `${pct}%`;
    }
    const label = document.getElementById('sb-chapter-progress-label');
    if (label) {
      const entry = Storyboard._timeline[idx];
      label.textContent = `${idx + 1}/${total} — ${entry ? entry.shot_id : ''}`;
    }
  },
};
