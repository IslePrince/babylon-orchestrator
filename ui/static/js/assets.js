/**
 * assets.js — Asset manifest management with category tabs,
 * approve toggles, and batch generation controls.
 */

const Assets = {

  slug: null,
  manifest: null,
  currentTab: 'environments',

  async init(slug) {
    Assets.slug = slug;
    await Assets.loadManifest();
  },

  async loadManifest() {
    try {
      Assets.manifest = await App.api('GET', `/api/${Assets.slug}/assets/manifest`);
      Assets._renderTab();
      Assets._renderBatches();
      Assets._updateCounts();
    } catch (e) {
      document.getElementById('asset-grid').innerHTML =
        '<div class="col-span-full text-gray-600 text-sm">No asset manifest found. Run the Asset Manifest stage first.</div>';
    }
  },

  switchTab(tab) {
    Assets.currentTab = tab;
    // Update tab styling
    document.querySelectorAll('.asset-tab').forEach(el => {
      el.classList.toggle('active', el.dataset.tab === tab);
    });
    Assets._renderTab();
  },

  _renderTab() {
    const container = document.getElementById('asset-grid');
    if (!container || !Assets.manifest) return;

    const assets = (Assets.manifest.assets || {})[Assets.currentTab] || [];

    if (assets.length === 0) {
      container.innerHTML = `
        <div class="col-span-full text-center py-8">
          <div class="text-gray-600 text-sm">No ${Assets.currentTab.replace(/_/g, ' ')} in manifest.</div>
        </div>
      `;
      return;
    }

    container.innerHTML = assets.map(a => {
      const approved = a.approved_for_generation === true;
      const meshStatus = (a.meshy || {}).status || 'pending';
      const detailLevel = a.detail_level || 'medium';

      return `
        <div class="card p-3">
          <div class="flex items-start justify-between mb-2">
            <div class="flex-1 min-w-0">
              <div class="text-sm font-medium text-gray-200 truncate">${App.escapeHtml(a.display_name || a.asset_id)}</div>
              <div class="text-xs text-gray-500">${App.escapeHtml(a.asset_id)}</div>
            </div>
            <div class="flex items-center gap-2">
              ${App.statusBadge(meshStatus)}
            </div>
          </div>

          ${a.description ? `<p class="text-xs text-gray-400 mb-2 line-clamp-2">${App.escapeHtml(a.description)}</p>` : ''}

          <div class="flex items-center justify-between mt-2">
            <div class="flex items-center gap-2 text-xs text-gray-500">
              <span class="badge ${detailLevel === 'hero' ? 'badge-approved' : detailLevel === 'low' ? 'badge-locked' : 'badge-draft'}">${detailLevel}</span>
            </div>
            <label class="flex items-center gap-1.5 cursor-pointer">
              <input type="checkbox" ${approved ? 'checked' : ''}
                     onchange="Assets.toggleApproval('${a.asset_id}')"
                     class="rounded bg-gray-800 border-gray-600 text-babylon-500 focus:ring-babylon-500" />
              <span class="text-xs text-gray-400">Approved</span>
            </label>
          </div>
        </div>
      `;
    }).join('');
  },

  _renderBatches() {
    const container = document.getElementById('batch-list');
    if (!container || !Assets.manifest) return;

    const batches = Assets.manifest.generation_batches || [];
    if (batches.length === 0) {
      container.innerHTML = '<div class="text-xs text-gray-600">No generation batches defined.</div>';
      return;
    }

    container.innerHTML = `
      <table class="w-full text-sm">
        <thead><tr class="text-xs text-gray-500 border-b border-gray-800">
          <th class="text-left py-2 pr-4">Batch</th>
          <th class="text-left py-2 pr-4">Label</th>
          <th class="text-right py-2 pr-4">Assets</th>
          <th class="text-left py-2 pr-4">Status</th>
          <th class="text-left py-2 pr-4">Approved</th>
          <th class="text-right py-2">Action</th>
        </tr></thead>
        <tbody>
          ${batches.map(b => `
            <tr class="border-b border-gray-800/50">
              <td class="py-2 pr-4 text-gray-400">${App.escapeHtml(b.batch_id)}</td>
              <td class="py-2 pr-4 text-gray-300">${App.escapeHtml(b.label || '')}</td>
              <td class="py-2 pr-4 text-right text-gray-400">${(b.asset_ids || []).length}</td>
              <td class="py-2 pr-4">${App.statusBadge(b.status || 'pending')}</td>
              <td class="py-2 pr-4 text-gray-400">${b.approved ? 'Yes' : 'No'}</td>
              <td class="py-2 text-right">
                ${b.status === 'pending' ? `
                  <button onclick="Assets.runBatch('${b.batch_id}')"
                          class="text-xs text-babylon-400 hover:text-babylon-300">Run</button>
                ` : ''}
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  },

  _updateCounts() {
    const countEl = document.getElementById('asset-count');
    if (!countEl || !Assets.manifest) return;

    let total = 0;
    let approved = 0;
    for (const [, assets] of Object.entries(Assets.manifest.assets || {})) {
      for (const a of assets) {
        total++;
        if (a.approved_for_generation) approved++;
      }
    }
    countEl.textContent = `${approved}/${total} approved`;
  },

  async toggleApproval(assetId) {
    try {
      const result = await App.api('POST', `/api/${Assets.slug}/asset/${assetId}/approve`);
      App.showToast(
        `${assetId}: ${result.approved ? 'approved' : 'unapproved'}`,
        result.approved ? 'success' : 'info'
      );
      await Assets.loadManifest();
    } catch (e) {
      // Error toast already shown
    }
  },

  async batchApproveAll() {
    if (!Assets.manifest) return;
    const allAssets = Assets.manifest.assets || {};
    let count = 0;

    for (const [, assets] of Object.entries(allAssets)) {
      for (const a of assets) {
        if (!a.approved_for_generation && (a.meshy || {}).status === 'pending') {
          try {
            await App.api('POST', `/api/${Assets.slug}/asset/${a.asset_id}/approve`);
            count++;
          } catch (e) { /* continue */ }
        }
      }
    }

    App.showToast(`Approved ${count} assets`, 'success');
    await Assets.loadManifest();
  },

  runBatch(batchId) {
    StageRunner.run('meshes', { batch_id: batchId }).catch(() => {});
    App.showToast('Mesh generation started — check Active Jobs', 'info');
  },

  runMeshBatch() {
    StageRunner.run('meshes', {}).catch(() => {});
    App.showToast('Running next mesh batch — check Active Jobs', 'info');
  },
};
