/**
 * voices.js — Voice casting management.
 *
 * Lists characters, shows current voice assignment,
 * browses ElevenLabs voice library, and allows selecting a voice.
 */

const Voices = {

  slug: null,
  characters: [],
  selectedCharId: null,
  voiceLibrary: null,  // cached voice list

  async init(slug) {
    Voices.slug = slug;
    Voices._initSliderListeners();
    await Voices.loadCharacters();
  },

  async loadCharacters() {
    try {
      Voices.characters = await App.api('GET', `/api/${Voices.slug}/characters`);
      Voices._renderCharacterList();
    } catch (e) {
      document.getElementById('voice-character-list').innerHTML =
        '<div class="text-red-400 text-xs">Failed to load characters</div>';
    }
  },

  _renderCharacterList() {
    const container = document.getElementById('voice-character-list');
    if (!container) return;

    const chars = Voices.characters;
    if (chars.length === 0) {
      container.innerHTML = '<div class="text-gray-600 text-xs">No characters found. Run the Ingest stage first.</div>';
      return;
    }

    const cast = chars.filter(c => c.has_voice).length;
    const countEl = document.getElementById('voice-cast-count');
    if (countEl) countEl.textContent = `${cast}/${chars.length} cast`;

    container.innerHTML = chars.map(c => {
      const isSelected = c.character_id === Voices.selectedCharId;
      const voiceIcon = c.has_voice
        ? '<span class="w-2 h-2 rounded-full bg-green-500 flex-shrink-0"></span>'
        : '<span class="w-2 h-2 rounded-full bg-gray-600 flex-shrink-0"></span>';

      return `
        <div class="flex items-center gap-2 px-3 py-2 rounded cursor-pointer transition-colors
                    ${isSelected ? 'bg-babylon-900/50 text-babylon-300' : 'hover:bg-gray-800 text-gray-300'}"
             onclick="Voices.selectCharacter('${c.character_id}')">
          ${voiceIcon}
          <div class="flex-1 min-w-0">
            <div class="text-sm truncate">${App.escapeHtml(c.display_name || c.character_id)}</div>
            <div class="text-xs text-gray-600">${App.escapeHtml(c.tier || '')} ${App.escapeHtml(c.role || '')}</div>
          </div>
        </div>
      `;
    }).join('');
  },

  async selectCharacter(charId) {
    Voices.selectedCharId = charId;
    Voices._renderCharacterList();

    const panel = document.getElementById('voice-detail');
    panel.innerHTML = '<div class="skeleton h-32 w-full"></div>';

    try {
      const char = await App.api('GET', `/api/${Voices.slug}/character/${charId}`);
      Voices._renderCharacterDetail(char);
    } catch (e) {
      panel.innerHTML = '<div class="text-red-400 text-sm">Failed to load character</div>';
    }
  },

  _renderCharacterDetail(char) {
    const panel = document.getElementById('voice-detail');
    if (!panel) return;

    const voice = char.voice || {};
    const voiceId = voice.voice_id;
    const charId = char.character_id || Voices.selectedCharId;

    let html = `
      <div class="mb-4">
        <h3 class="text-base font-semibold text-gray-100">${App.escapeHtml(char.display_name || charId)}</h3>
        <div class="text-xs text-gray-500 mt-1">${App.escapeHtml(char.tier || '')} ${App.escapeHtml(char.role || '')}</div>
      </div>
    `;

    // Voice description from character schema
    const voiceDesc = voice.description || char.voice_description || '';
    if (voiceDesc) {
      html += `
        <div class="mb-4">
          <h4 class="text-xs font-semibold text-gray-500 uppercase mb-1">Voice Description</h4>
          <p class="text-sm text-gray-300">${App.escapeHtml(voiceDesc)}</p>
        </div>
      `;
    }

    // Current assignment
    const settings = voice.settings || {};
    const stability = settings.stability ?? 0.75;
    const simBoost = settings.similarity_boost ?? 0.85;
    const style = settings.style ?? 0.35;

    html += `
      <div class="mb-4">
        <h4 class="text-xs font-semibold text-gray-500 uppercase mb-1">Current Voice</h4>
        ${voiceId
          ? `<div class="flex items-center gap-2">
              <span class="badge badge-approved">${App.escapeHtml(voiceId)}</span>
              ${voice.assigned_at ? `<span class="text-xs text-gray-600">assigned ${voice.assigned_at.split('T')[0]}</span>` : ''}
            </div>`
          : '<div class="text-sm text-gray-600">No voice assigned</div>'
        }
      </div>
    `;

    // Voice generation settings
    if (voiceId) {
      html += `
        <div class="mb-4">
          <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Voice Settings</h4>
          <div class="space-y-2">
            <div class="flex items-center gap-2">
              <label class="text-xs text-gray-400 w-20">Stability</label>
              <input type="range" id="vs-stability" min="0" max="1" step="0.05" value="${stability}"
                     class="flex-1 h-1 accent-babylon-500" />
              <span id="vs-stability-val" class="text-xs text-gray-500 w-8">${stability}</span>
            </div>
            <div class="flex items-center gap-2">
              <label class="text-xs text-gray-400 w-20">Similarity</label>
              <input type="range" id="vs-similarity" min="0" max="1" step="0.05" value="${simBoost}"
                     class="flex-1 h-1 accent-babylon-500" />
              <span id="vs-similarity-val" class="text-xs text-gray-500 w-8">${simBoost}</span>
            </div>
            <div class="flex items-center gap-2">
              <label class="text-xs text-gray-400 w-20">Style</label>
              <input type="range" id="vs-style" min="0" max="1" step="0.05" value="${style}"
                     class="flex-1 h-1 accent-babylon-500" />
              <span id="vs-style-val" class="text-xs text-gray-500 w-8">${style}</span>
            </div>
            <div class="flex justify-between items-center">
              <span class="text-[10px] text-gray-600">Lower stability = more expressive</span>
              <button onclick="Voices.saveVoiceSettings('${charId}')"
                      class="px-2 py-1 bg-babylon-700 hover:bg-babylon-600 text-xs text-white rounded transition-colors">
                Save Settings
              </button>
            </div>
          </div>
        </div>
      `;
    }

    // Manual voice ID assignment
    html += `
      <div class="mb-4">
        <h4 class="text-xs font-semibold text-gray-500 uppercase mb-1">Assign Voice ID</h4>
        <div class="flex gap-2">
          <input type="text" id="voice-id-input" placeholder="e.g. ElevenLabs voice ID"
                 value="${App.escapeHtml(voiceId || '')}"
                 class="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-babylon-500" />
          <button onclick="Voices.assignVoice('${charId}')"
                  class="px-3 py-1.5 bg-babylon-600 hover:bg-babylon-500 text-white text-sm rounded transition-colors">
            Save
          </button>
        </div>
      </div>
    `;

    // Recorded lines section
    html += `
      <div class="border-t border-gray-800 pt-4 mt-4">
        <div class="flex items-center justify-between mb-2">
          <h4 class="text-xs font-semibold text-gray-500 uppercase">Recorded Lines</h4>
          <div class="flex gap-2">
            <button onclick="Voices.loadRecordedLines('${charId}')"
                    class="px-2 py-1 bg-gray-700 hover:bg-gray-600 text-xs text-gray-300 rounded transition-colors">
              Load Lines
            </button>
            ${voiceId ? `<button id="rerecord-btn" onclick="Voices.rerecordLines('${charId}')"
                    class="px-2 py-1 bg-red-700 hover:bg-red-600 text-xs text-white rounded transition-colors"
                    title="Re-record all lines with the current voice">
              Re-record All
            </button>` : ''}
          </div>
        </div>
        <div id="recorded-lines-container" class="text-xs text-gray-600">Click "Load Lines" to see recorded audio</div>
      </div>
    `;

    // Voice matching + library
    html += `
      <div class="border-t border-gray-800 pt-4 mt-4">
        <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Find Voice</h4>
        <div class="flex gap-2 mb-2">
          <button id="voice-match-btn" onclick="Voices.matchVoices('${charId}')"
                  class="px-3 py-1.5 bg-babylon-700 hover:bg-babylon-600 text-white text-sm rounded transition-colors">
            AI Match (~$0.02)
          </button>
          <button id="voice-browse-btn" onclick="Voices.browseVoices('${charId}')"
                  class="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white text-sm rounded transition-colors">
            My Voices
          </button>
          <button id="voice-search-lib-btn" onclick="Voices.searchLibrary('${charId}')"
                  class="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white text-sm rounded transition-colors">
            Search Library
          </button>
        </div>
        <div id="voice-library-container" class="mt-3"></div>
      </div>
    `;

    panel.innerHTML = html;
  },

  async assignVoice(charId) {
    const input = document.getElementById('voice-id-input');
    const voiceId = input ? input.value.trim() : '';
    if (!voiceId) {
      App.showToast('Enter a voice ID', 'warning');
      return;
    }

    try {
      await App.api('POST', `/api/${Voices.slug}/voice/${charId}/select`, { voice_id: voiceId });
      App.showToast(`Voice assigned to ${charId}`, 'success');
      // Refresh
      await Voices.loadCharacters();
      Voices.selectCharacter(charId);
    } catch (e) {
      // Error toast already shown
    }
  },

  async matchVoices(charId) {
    const container = document.getElementById('voice-library-container');
    const btn = document.getElementById('voice-match-btn');
    if (!container) return;

    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="animate-pulse">Matching...</span>'; }
    container.innerHTML = '<div class="skeleton h-20 w-full"></div>';

    try {
      const result = await App.api('POST', `/api/${Voices.slug}/voice/${charId}/match`);
      const matches = result.matches || [];
      if (matches.length === 0) {
        container.innerHTML = '<div class="text-gray-500 text-xs">No matches found</div>';
        return;
      }

      let html = `<div class="text-xs text-gray-600 mb-2">${matches.length} AI-recommended voices</div>`;
      html += '<div class="space-y-2 max-h-96 overflow-y-auto pr-1">';

      for (const m of matches) {
        const labels = m.labels || {};
        const labelTags = Object.entries(labels)
          .map(([k, val]) => `<span class="inline-block px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-400">${App.escapeHtml(val || k)}</span>`)
          .join(' ');

        html += `
          <div class="p-2 rounded border border-babylon-800/50 bg-babylon-900/20">
            <div class="flex items-center justify-between gap-2 mb-1">
              <span class="text-sm font-medium text-gray-200 truncate">${App.escapeHtml(m.name || m.voice_id)}</span>
              <div class="flex gap-1 flex-shrink-0">
                ${m.preview_url ? `<button onclick="Voices._playPreview(this, '${App.escapeHtml(m.preview_url)}')"
                  class="px-2 py-0.5 bg-gray-700 hover:bg-gray-600 text-xs text-gray-300 rounded transition-colors">Play</button>` : ''}
                <button onclick="Voices._selectFromLibrary('${charId}', '${App.escapeHtml(m.voice_id)}')"
                  class="px-2 py-0.5 bg-babylon-700 hover:bg-babylon-600 text-xs text-white rounded transition-colors">Select</button>
              </div>
            </div>
            ${m.reason ? `<div class="text-xs text-babylon-400 mb-1">${App.escapeHtml(m.reason)}</div>` : ''}
            <div class="flex flex-wrap gap-1">${labelTags}</div>
          </div>
        `;
      }
      html += '</div>';
      container.innerHTML = html;
    } catch (e) {
      container.innerHTML = '<div class="text-red-400 text-xs">Failed to match voices</div>';
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = 'AI Match (~$0.02)'; }
    }
  },

  async browseVoices(charId) {
    const container = document.getElementById('voice-library-container');
    const btn = document.getElementById('voice-browse-btn');
    if (!container) return;

    // Show loading
    if (btn) { btn.disabled = true; btn.textContent = 'Loading...'; }
    container.innerHTML = '<div class="skeleton h-20 w-full"></div>';

    try {
      // Cache the voice library
      if (!Voices.voiceLibrary) {
        Voices.voiceLibrary = await App.api('GET', `/api/${Voices.slug}/voices/library`);
      }
      Voices._renderVoiceLibrary(container, charId, '');
    } catch (e) {
      container.innerHTML = '<div class="text-red-400 text-xs">Failed to load voice library</div>';
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Browse Voices'; }
    }
  },

  _renderVoiceLibrary(container, charId, filter) {
    const voices = Voices.voiceLibrary || [];
    const lowerFilter = filter.toLowerCase();
    const filtered = lowerFilter
      ? voices.filter(v => {
          const labels = Object.values(v.labels || {}).join(' ').toLowerCase();
          return v.name.toLowerCase().includes(lowerFilter)
            || (v.description || '').toLowerCase().includes(lowerFilter)
            || labels.includes(lowerFilter);
        })
      : voices;

    let html = `
      <input type="text" id="voice-search" placeholder="Filter by name, gender, age, accent..."
             value="${App.escapeHtml(filter)}"
             oninput="Voices._renderVoiceLibrary(document.getElementById('voice-library-container'), '${charId}', this.value)"
             class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 mb-3
                    focus:outline-none focus:ring-1 focus:ring-babylon-500" />
      <div class="text-xs text-gray-600 mb-2">${filtered.length} voices</div>
      <div class="space-y-2 max-h-96 overflow-y-auto pr-1">
    `;

    for (const v of filtered.slice(0, 50)) {
      const labels = v.labels || {};
      const labelTags = Object.entries(labels)
        .map(([k, val]) => `<span class="inline-block px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-400">${App.escapeHtml(val || k)}</span>`)
        .join(' ');

      html += `
        <div class="p-2 rounded border border-gray-800 hover:border-gray-600 transition-colors">
          <div class="flex items-center justify-between gap-2 mb-1">
            <span class="text-sm font-medium text-gray-200 truncate">${App.escapeHtml(v.name)}</span>
            <div class="flex gap-1 flex-shrink-0">
              ${v.preview_url ? `<button onclick="Voices._playPreview(this, '${App.escapeHtml(v.preview_url)}')"
                class="px-2 py-0.5 bg-gray-700 hover:bg-gray-600 text-xs text-gray-300 rounded transition-colors">Play</button>` : ''}
              <button onclick="Voices._selectFromLibrary('${charId}', '${App.escapeHtml(v.voice_id)}')"
                class="px-2 py-0.5 bg-babylon-700 hover:bg-babylon-600 text-xs text-white rounded transition-colors">Select</button>
            </div>
          </div>
          ${v.description ? `<div class="text-xs text-gray-500 mb-1">${App.escapeHtml(v.description).substring(0, 120)}</div>` : ''}
          <div class="flex flex-wrap gap-1">
            <span class="inline-block px-1.5 py-0.5 bg-gray-800/50 rounded text-[10px] text-gray-500">${App.escapeHtml(v.category)}</span>
            ${labelTags}
          </div>
        </div>
      `;
    }

    if (filtered.length > 50) {
      html += `<div class="text-xs text-gray-600 py-2">Showing first 50 — use the filter to narrow results</div>`;
    }

    html += '</div>';
    container.innerHTML = html;

    // Restore focus to search input
    const searchInput = document.getElementById('voice-search');
    if (searchInput) {
      searchInput.focus();
      searchInput.setSelectionRange(searchInput.value.length, searchInput.value.length);
    }
  },

  // ------------------------------------------------------------------
  // Shared voice library search
  // ------------------------------------------------------------------

  _sharedSearchCharId: null,
  _sharedDebounce: null,

  searchLibrary(charId) {
    Voices._sharedSearchCharId = charId;
    const container = document.getElementById('voice-library-container');
    if (!container) return;
    Voices._renderSharedSearch(container, charId);
  },

  _renderSharedSearch(container, charId) {
    container.innerHTML = `
      <div class="space-y-2 mb-3">
        <input type="text" id="shared-search-input" placeholder="Search thousands of voices..."
               class="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200
                      focus:outline-none focus:ring-1 focus:ring-babylon-500" />
        <div class="flex gap-2">
          <select id="shared-filter-gender"
                  class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 focus:outline-none">
            <option value="">Any gender</option>
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="neutral">Neutral</option>
          </select>
          <select id="shared-filter-age"
                  class="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 focus:outline-none">
            <option value="">Any age</option>
            <option value="young">Young</option>
            <option value="middle_aged">Middle-aged</option>
            <option value="old">Old</option>
          </select>
        </div>
      </div>
      <div id="shared-results" class="text-xs text-gray-600">Type to search the ElevenLabs voice library</div>
    `;

    const searchInput = document.getElementById('shared-search-input');
    const genderSelect = document.getElementById('shared-filter-gender');
    const ageSelect = document.getElementById('shared-filter-age');

    const triggerSearch = () => {
      clearTimeout(Voices._sharedDebounce);
      Voices._sharedDebounce = setTimeout(() => Voices._executeSharedSearch(charId), 500);
    };

    searchInput.addEventListener('input', triggerSearch);
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        clearTimeout(Voices._sharedDebounce);
        Voices._executeSharedSearch(charId);
      }
    });
    genderSelect.addEventListener('change', triggerSearch);
    ageSelect.addEventListener('change', triggerSearch);

    searchInput.focus();
  },

  async _executeSharedSearch(charId) {
    const resultsEl = document.getElementById('shared-results');
    if (!resultsEl) return;

    const search = (document.getElementById('shared-search-input') || {}).value || '';
    const gender = (document.getElementById('shared-filter-gender') || {}).value || '';
    const age = (document.getElementById('shared-filter-age') || {}).value || '';

    if (!search && !gender && !age) {
      resultsEl.innerHTML = '<div class="text-xs text-gray-600">Type to search the ElevenLabs voice library</div>';
      return;
    }

    resultsEl.innerHTML = '<div class="skeleton h-20 w-full"></div>';

    const params = new URLSearchParams();
    if (search) params.set('search', search);
    if (gender) params.set('gender', gender);
    if (age) params.set('age', age);
    params.set('page_size', '50');

    try {
      const voices = await App.api('GET', `/api/${Voices.slug}/voices/shared?${params.toString()}`);

      if (!voices.length) {
        resultsEl.innerHTML = '<div class="text-xs text-gray-500">No voices found — try different search terms</div>';
        return;
      }

      let html = `<div class="text-xs text-gray-600 mb-2">${voices.length} shared voices</div>`;
      html += '<div class="space-y-2 max-h-96 overflow-y-auto pr-1">';

      for (const v of voices) {
        const labels = v.labels || {};
        const labelTags = Object.entries(labels)
          .map(([k, val]) => `<span class="inline-block px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-400">${App.escapeHtml(val || k)}</span>`)
          .join(' ');

        html += `
          <div class="p-2 rounded border border-gray-800 hover:border-gray-600 transition-colors">
            <div class="flex items-center justify-between gap-2 mb-1">
              <span class="text-sm font-medium text-gray-200 truncate">${App.escapeHtml(v.name || v.voice_id)}</span>
              <div class="flex gap-1 flex-shrink-0">
                ${v.preview_url ? `<button onclick="Voices._playPreview(this, '${App.escapeHtml(v.preview_url)}')"
                  class="px-2 py-0.5 bg-gray-700 hover:bg-gray-600 text-xs text-gray-300 rounded transition-colors">Play</button>` : ''}
                <button onclick="Voices._selectFromLibrary('${charId}', '${App.escapeHtml(v.voice_id)}')"
                  class="px-2 py-0.5 bg-babylon-700 hover:bg-babylon-600 text-xs text-white rounded transition-colors">Select</button>
              </div>
            </div>
            ${v.description ? `<div class="text-xs text-gray-500 mb-1">${App.escapeHtml(v.description).substring(0, 120)}</div>` : ''}
            <div class="flex flex-wrap gap-1">
              <span class="inline-block px-1.5 py-0.5 bg-gray-800/50 rounded text-[10px] text-gray-500">${App.escapeHtml(v.category || 'shared')}</span>
              ${labelTags}
            </div>
          </div>
        `;
      }

      html += '</div>';
      resultsEl.innerHTML = html;
    } catch (e) {
      resultsEl.innerHTML = '<div class="text-red-400 text-xs">Search failed — check console</div>';
    }
  },

  _playPreview(btn, url) {
    // Stop any existing preview
    if (Voices._currentAudio) {
      Voices._currentAudio.pause();
      Voices._currentAudio = null;
      if (Voices._currentPlayBtn) Voices._currentPlayBtn.textContent = 'Play';
    }

    const audio = new Audio(url);
    Voices._currentAudio = audio;
    Voices._currentPlayBtn = btn;
    btn.textContent = 'Stop';

    audio.play();
    audio.onended = () => {
      btn.textContent = 'Play';
      Voices._currentAudio = null;
    };

    btn.onclick = () => {
      audio.pause();
      btn.textContent = 'Play';
      Voices._currentAudio = null;
      btn.onclick = () => Voices._playPreview(btn, url);
    };
  },

  async _selectFromLibrary(charId, voiceId) {
    try {
      await App.api('POST', `/api/${Voices.slug}/voice/${charId}/select`, { voice_id: voiceId });
      App.showToast(`Voice assigned to ${charId}`, 'success');
      await Voices.loadCharacters();
      Voices.selectCharacter(charId);
    } catch (e) {
      // Error toast already shown
    }
  },

  _currentAudio: null,
  _currentPlayBtn: null,

  // ------------------------------------------------------------------
  // Recorded lines & re-record
  // ------------------------------------------------------------------

  async loadRecordedLines(charId) {
    const container = document.getElementById('recorded-lines-container');
    if (!container) return;
    container.innerHTML = '<div class="skeleton h-16 w-full"></div>';

    try {
      const data = await App.api('GET', `/api/${Voices.slug}/voice/${charId}/lines`);
      const lines = data.lines || [];
      if (lines.length === 0) {
        container.innerHTML = '<div class="text-xs text-gray-600">No recorded audio found for this character</div>';
        return;
      }

      let html = `<div class="text-xs text-gray-500 mb-2">${lines.length} recorded line(s)</div>`;
      html += '<div class="space-y-2 max-h-80 overflow-y-auto pr-1">';

      for (const line of lines) {
        const shotLabel = line.shot_id || '';
        const chLabel = line.chapter_id || '';
        const lineText = line.text || '(text unavailable)';
        const hasFile = line.file_exists && line.audio_url;
        const directionTag = line.direction
          ? `<em class="text-babylon-400">(${App.escapeHtml(line.direction)})</em> `
          : '';

        html += `
          <div class="p-2 rounded border border-gray-800 bg-gray-900/40">
            <div class="flex items-center gap-2 mb-1">
              <span class="inline-block px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">${App.escapeHtml(chLabel)}</span>
              <span class="inline-block px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">${App.escapeHtml(shotLabel)}</span>
              <span class="text-[10px] text-gray-600 truncate flex-1">${App.escapeHtml(line.line_id)}</span>
              ${line.recorded_at
                ? `<span class="text-[10px] text-gray-600 flex-shrink-0" title="${App.escapeHtml(line.recorded_at)}">${new Date(line.recorded_at).toLocaleDateString()} ${new Date(line.recorded_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</span>`
                : '<span class="text-[10px] text-yellow-600 flex-shrink-0">not recorded</span>'
              }
            </div>
            <p class="text-xs text-gray-300 mb-1.5 leading-relaxed">${directionTag}${App.escapeHtml(lineText)}</p>
            <div class="flex items-center gap-2">
              ${hasFile
                ? `<audio controls preload="none" class="h-7 flex-1" style="min-width:160px">
                    <source src="${App.escapeHtml(line.audio_url)}" type="audio/mpeg" />
                  </audio>`
                : '<span class="text-[10px] text-gray-700 flex-1">Audio file missing</span>'
              }
              <button onclick="Voices.rerecordSingleLine('${App.escapeHtml(line.line_id)}', this)"
                      data-ref="${App.escapeHtml(line.audio_ref || '')}"
                      data-text="${App.escapeHtml(line.text || '')}"
                      data-char="${App.escapeHtml(charId)}"
                      class="px-1.5 py-0.5 bg-gray-700 hover:bg-gray-600 text-[10px] text-gray-400 rounded transition-colors flex-shrink-0"
                      title="Re-record this line">Redo</button>
            </div>
          </div>
        `;
      }

      html += '</div>';
      container.innerHTML = html;
    } catch (e) {
      container.innerHTML = '<div class="text-red-400 text-xs">Failed to load recorded lines</div>';
    }
  },

  async rerecordLines(charId) {
    const btn = document.getElementById('rerecord-btn');
    if (!confirm('Re-record ALL lines for this character with the current voice?\nThis will overwrite existing audio files and costs ElevenLabs credits.')) {
      return;
    }

    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="animate-pulse">Re-recording...</span>'; }

    try {
      const result = await App.api('POST', `/api/${Voices.slug}/voice/${charId}/rerecord`);
      App.showToast(`Re-recorded ${result.lines_rerecorded}/${result.total_lines} lines ($${result.cost_usd.toFixed(4)})`, 'success');
      Voices.loadRecordedLines(charId);
    } catch (e) {
      // Error toast already shown by App.api
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Re-record All'; }
    }
  },

  async rerecordSingleLine(lineId, btn) {
    const charId = btn.dataset.char;
    const audioRef = btn.dataset.ref;
    const text = btn.dataset.text;
    if (!charId || !audioRef || !text) {
      App.showToast('Missing line data', 'warning');
      return;
    }

    const origText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="animate-pulse">...</span>';

    try {
      const result = await App.api('POST', `/api/${Voices.slug}/voice/${charId}/rerecord-line`, {
        line_id: lineId,
        audio_ref: audioRef,
        text: text,
      });
      App.showToast(`Re-recorded ${lineId} ($${result.cost_usd.toFixed(4)})`, 'success');
      // Refresh the audio player — find the parent card and replace audio src
      const card = btn.closest('.p-2');
      if (card) {
        const audio = card.querySelector('audio');
        if (audio) {
          // Cache-bust by adding timestamp
          const src = audio.querySelector('source');
          if (src) {
            src.src = src.src.split('?')[0] + '?t=' + Date.now();
            audio.load();
          }
        }
      }
    } catch (e) {
      // Error toast already shown
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  },

  async saveVoiceSettings(charId) {
    const stability = document.getElementById('vs-stability');
    const similarity = document.getElementById('vs-similarity');
    const style = document.getElementById('vs-style');
    if (!stability || !similarity || !style) return;

    try {
      await App.api('POST', `/api/${Voices.slug}/voice/${charId}/settings`, {
        stability: parseFloat(stability.value),
        similarity_boost: parseFloat(similarity.value),
        style: parseFloat(style.value),
      });
      App.showToast('Voice settings saved', 'success');
    } catch (e) {
      // Error toast already shown
    }
  },

  _initSliderListeners() {
    document.addEventListener('input', (e) => {
      if (e.target.id === 'vs-stability') {
        const el = document.getElementById('vs-stability-val');
        if (el) el.textContent = e.target.value;
      } else if (e.target.id === 'vs-similarity') {
        const el = document.getElementById('vs-similarity-val');
        if (el) el.textContent = e.target.value;
      } else if (e.target.id === 'vs-style') {
        const el = document.getElementById('vs-style-val');
        if (el) el.textContent = e.target.value;
      }
    });
  },
};
