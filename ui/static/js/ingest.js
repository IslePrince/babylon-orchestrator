/**
 * ingest.js — Ingest & World Bible page.
 * Shows source text info, world bible summary, and chapter index.
 */

const Ingest = {

  slug: null,

  async init(slug) {
    Ingest.slug = slug;
    // Load world bible and chapters in parallel
    const [wbResult, chaptersResult] = await Promise.allSettled([
      App.api('GET', `/api/${slug}/world-bible`),
      App.api('GET', `/api/${slug}/chapters`),
    ]);

    if (wbResult.status === 'fulfilled' && !wbResult.value.error) {
      Ingest._renderWorldBible(wbResult.value);
    } else {
      document.getElementById('world-bible-section').innerHTML =
        '<div class="card p-6 text-center text-gray-600">No world bible yet. Run the Ingest stage to get started.</div>';
    }

    if (chaptersResult.status === 'fulfilled') {
      Ingest._renderChapters(chaptersResult.value);
    } else {
      document.getElementById('chapter-table').innerHTML =
        '<div class="text-gray-600 text-sm">No chapters found.</div>';
    }
  },

  _renderWorldBible(wb) {
    const container = document.getElementById('world-bible-section');
    if (!container) return;

    const bible = wb.world_bible || wb;
    const setting = bible.setting || {};
    // visual_palette has color arrays + material_textures
    const palette = bible.visual_palette || {};
    const lighting = bible.lighting_rules || {};
    // anachronism_watchlist has forbidden_items, forbidden_clothing, etc.
    const anachronisms = bible.anachronism_watchlist || {};
    const allForbidden = [
      ...(anachronisms.forbidden_items || []),
      ...(anachronisms.forbidden_clothing || []),
      ...(anachronisms.forbidden_architecture || []),
      ...(anachronisms.forbidden_concepts || []),
    ];
    // Combine all palette colors into swatches
    const colorSwatches = {};
    for (const [label, colors] of Object.entries(palette)) {
      if (Array.isArray(colors)) {
        colors.forEach(c => { if (typeof c === 'string' && c.startsWith('#')) colorSwatches[c] = label; });
      }
    }
    const colorMeanings = palette.color_meanings || {};

    container.innerHTML = `
      <div class="card p-5">
        <div class="flex items-start justify-between mb-4">
          <div>
            <h3 class="text-base font-semibold text-gray-100">${App.escapeHtml(bible.title || 'World Bible')}</h3>
            <p class="text-sm text-gray-500 mt-1">${App.escapeHtml(setting.time_period || setting.period || '')} &mdash; ${App.escapeHtml(setting.location || '')}</p>
          </div>
          ${App.statusBadge(wb.meta && wb.meta.approved ? 'approved' : 'draft')}
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
          <!-- Setting -->
          <div>
            <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Setting</h4>
            <dl class="space-y-1 text-sm">
              ${(setting.time_period || setting.period) ? `<div class="flex"><dt class="text-gray-500 w-20">Period</dt><dd class="text-gray-300">${App.escapeHtml(setting.time_period || setting.period)}</dd></div>` : ''}
              ${setting.location ? `<div class="flex"><dt class="text-gray-500 w-20">Location</dt><dd class="text-gray-300">${App.escapeHtml(setting.location)}</dd></div>` : ''}
              ${setting.cultural_context ? `<div class="flex"><dt class="text-gray-500 w-20">Context</dt><dd class="text-gray-300">${App.escapeHtml(setting.cultural_context)}</dd></div>` : ''}
            </dl>
          </div>

          <!-- Visual Language -->
          <div>
            <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Visual Language</h4>
            ${Object.keys(colorSwatches).length > 0 ? `
              <div class="flex flex-wrap gap-1.5 mb-2">
                ${Object.entries(colorSwatches).map(([hex, label]) => `
                  <span class="inline-flex items-center gap-1 text-xs text-gray-400" title="${App.escapeHtml(colorMeanings[hex] || label)}">
                    <span class="w-3 h-3 rounded-full border border-gray-600" style="background:${App.escapeHtml(hex)}"></span>
                  </span>
                `).join('')}
              </div>
              ${Object.entries(colorMeanings).slice(0, 3).map(([hex, meaning]) => `
                <div class="flex items-center gap-1.5 text-xs text-gray-500 mb-0.5">
                  <span class="w-2 h-2 rounded-full" style="background:${App.escapeHtml(hex)}"></span>
                  ${App.escapeHtml(meaning)}
                </div>
              `).join('')}
            ` : ''}
            ${(palette.material_textures || []).length > 0 ? `
              <div class="mt-2">
                <div class="text-xs text-gray-600 mb-1">Materials</div>
                <div class="flex flex-wrap gap-1">
                  ${palette.material_textures.slice(0, 5).map(m => `<span class="badge badge-draft">${App.escapeHtml(m)}</span>`).join('')}
                </div>
              </div>
            ` : ''}
          </div>

          <!-- Anachronism Guards -->
          <div>
            <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Anachronism Guards</h4>
            ${allForbidden.length > 0 ? `
              <div class="flex flex-wrap gap-1">
                ${allForbidden.slice(0, 12).map(a => `<span class="badge badge-flagged">${App.escapeHtml(a)}</span>`).join('')}
                ${allForbidden.length > 12 ? `<span class="text-xs text-gray-600">+${allForbidden.length - 12} more</span>` : ''}
              </div>
            ` : '<div class="text-xs text-gray-600">None defined</div>'}
          </div>
        </div>

        ${Object.keys(lighting).length > 0 ? `
          <div class="mt-4 pt-4 border-t border-gray-800">
            <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Lighting Rules</h4>
            <div class="grid grid-cols-2 md:grid-cols-3 gap-2">
              ${Object.entries(lighting).slice(0, 6).map(([k, v]) => `
                <div class="text-xs">
                  <span class="text-gray-400 font-medium">${App.escapeHtml(k.replace(/_/g, ' '))}</span>
                  <div class="text-gray-600">${App.escapeHtml(typeof v === 'string' ? v.slice(0, 80) : '')}</div>
                </div>
              `).join('')}
            </div>
          </div>
        ` : ''}

        <div class="mt-4 pt-4 border-t border-gray-800">
          <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Visual Style (used for storyboard generation)</h4>
          <div class="flex gap-2 items-start">
            <textarea id="visual-style-input"
              class="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-babylon-500 resize-y"
              rows="2" placeholder="e.g. Photorealistic cinematic film still, warm golden-hour lighting...">${App.escapeHtml(bible.visual_style || '')}</textarea>
            <button onclick="Ingest.saveVisualStyle()"
              class="px-3 py-2 bg-babylon-600 hover:bg-babylon-500 text-white text-xs rounded transition-colors whitespace-nowrap">
              Save Style
            </button>
          </div>
        </div>

        ${(bible.key_locations || []).length > 0 ? `
          <div class="mt-4 pt-4 border-t border-gray-800">
            <h4 class="text-xs font-semibold text-gray-500 uppercase mb-2">Key Locations (${bible.key_locations.length})</h4>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-2">
              ${bible.key_locations.map(loc => `
                <div class="p-2 bg-gray-800/50 rounded border border-gray-700/50">
                  <div class="text-xs text-gray-300 font-medium">${App.escapeHtml(loc.name)}</div>
                  <div class="text-xs text-gray-600 mt-0.5">${App.escapeHtml((loc.description || '').slice(0, 60))}</div>
                </div>
              `).join('')}
            </div>
          </div>
        ` : ''}
      </div>
    `;
  },

  _renderChapters(chapters) {
    const container = document.getElementById('chapter-table');
    if (!container) return;

    if (!chapters || chapters.length === 0) {
      container.innerHTML = '<div class="text-gray-600 text-sm py-4 text-center">No chapters yet. Run the Ingest stage.</div>';
      return;
    }

    container.innerHTML = `
      <table class="w-full text-sm">
        <thead><tr class="text-xs text-gray-500 border-b border-gray-800">
          <th class="text-left py-2 pr-3">ID</th>
          <th class="text-left py-2 pr-3">Title</th>
          <th class="text-left py-2 pr-3">Characters</th>
          <th class="text-center py-2 pr-3">Source Text</th>
          <th class="text-center py-2 pr-3">Screenplay</th>
          <th class="text-left py-2">Summary</th>
        </tr></thead>
        <tbody>
          ${chapters.map(ch => `
            <tr class="border-b border-gray-800/50 hover:bg-gray-800/30">
              <td class="py-2 pr-3 text-gray-400 text-xs">${App.escapeHtml(ch.chapter_id)}</td>
              <td class="py-2 pr-3 text-gray-200 font-medium">${App.escapeHtml(ch.title || '')}</td>
              <td class="py-2 pr-3">
                <div class="flex flex-wrap gap-1">
                  ${(ch.characters || []).slice(0, 4).map(c =>
                    `<span class="badge badge-draft">${App.escapeHtml(c)}</span>`
                  ).join('')}
                  ${(ch.characters || []).length > 4 ? `<span class="text-xs text-gray-600">+${ch.characters.length - 4}</span>` : ''}
                </div>
              </td>
              <td class="py-2 pr-3 text-center">
                ${ch.has_source_text
                  ? `<span class="text-green-400 text-xs">${Ingest._formatSize(ch.source_text_size)}</span>`
                  : '<span class="text-gray-600 text-xs">none</span>'}
              </td>
              <td class="py-2 pr-3 text-center">
                ${ch.has_screenplay
                  ? '<span class="text-green-400 text-xs">yes</span>'
                  : '<span class="text-gray-600 text-xs">no</span>'}
              </td>
              <td class="py-2 text-xs text-gray-500 max-w-sm truncate">${App.escapeHtml((ch.summary || '').slice(0, 100))}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  },

  async runIngest() {
    const sourcePath = document.getElementById('ingest-source-path').value.trim();
    if (!sourcePath) {
      App.showToast('Enter the path to your source text file', 'warning');
      return;
    }
    const dryRun = document.getElementById('ingest-dry-run').checked;
    try {
      await StageRunner.run('ingest', { source: sourcePath, dry_run: dryRun });
      // Reload page data after ingest completes
      if (!dryRun) {
        setTimeout(() => Ingest.init(Ingest.slug), 1000);
      }
    } catch (e) {
      // StageRunner handles toast errors
    }
  },

  async saveVisualStyle() {
    const input = document.getElementById('visual-style-input');
    if (!input) return;
    const style = input.value.trim();
    if (!style) {
      App.showToast('Enter a visual style description', 'warning');
      return;
    }
    try {
      await App.api('PUT', `/api/${Ingest.slug}/world-bible/visual-style`, { visual_style: style });
      App.showToast('Visual style saved', 'success');
    } catch (e) {
      App.showToast(`Failed to save: ${e.message}`, 'error');
    }
  },

  _formatSize(bytes) {
    if (!bytes) return '0';
    if (bytes < 1024) return bytes + ' B';
    return (bytes / 1024).toFixed(1) + ' KB';
  },
};
