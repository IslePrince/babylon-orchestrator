/**
 * app.js — Babylon Studio common utilities.
 *
 * Provides: api(), showToast(), formatCost(), escapeHtml(),
 * statusBadge(), and right-panel data loaders.
 */

const App = {

  slug: null,  // set by page scripts for global access

  // ----------------------------------------------------------------
  // Fetch wrapper — all API calls go through this
  // ----------------------------------------------------------------

  async api(method, url, body = null) {
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);

    try {
      const res = await fetch(url, opts);
      const data = await res.json();
      if (!res.ok) {
        const msg = data.error || `Request failed (${res.status})`;
        App.showToast(msg, 'error');
        throw new Error(msg);
      }
      return data;
    } catch (err) {
      if (err.message === 'Failed to fetch') {
        App.showToast('Connection lost — is the server running?', 'error');
      }
      throw err;
    }
  },

  // ----------------------------------------------------------------
  // Toast notifications
  // ----------------------------------------------------------------

  showToast(message, type = 'info', duration = null) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    // Error/warning toasts stay visible much longer
    if (duration === null) {
      duration = (type === 'error') ? 15000
               : (type === 'warning') ? 10000
               : 5000;
    }

    const icons = {
      success: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>',
      error: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>',
      warning: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>',
      info: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
    };

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `${icons[type] || icons.info}<span>${App.escapeHtml(message)}</span>`;
    toast.onclick = () => App._dismissToast(toast);
    container.appendChild(toast);

    if (duration > 0) {
      setTimeout(() => App._dismissToast(toast), duration);
    }
  },

  _dismissToast(el) {
    if (el.classList.contains('dismissing')) return;
    el.classList.add('dismissing');
    el.addEventListener('animationend', () => el.remove());
  },

  // ----------------------------------------------------------------
  // Formatting helpers
  // ----------------------------------------------------------------

  formatCost(usd) {
    return '$' + Number(usd || 0).toFixed(2);
  },

  escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  },

  statusBadge(status) {
    const map = {
      pending: 'badge-pending',
      draft: 'badge-draft',
      approved: 'badge-approved',
      complete: 'badge-complete',
      flagged: 'badge-flagged',
      locked: 'badge-locked',
      not_started: 'badge-pending',
      in_progress: 'badge-active',
    };
    const cls = map[status] || 'badge-pending';
    return `<span class="badge ${cls}">${App.escapeHtml(status)}</span>`;
  },

  budgetColor(pct) {
    if (pct >= 90) return 'budget-red';
    if (pct >= 70) return 'budget-yellow';
    return 'budget-green';
  },

  // ----------------------------------------------------------------
  // Right panel loaders
  // ----------------------------------------------------------------

  async loadRightPanel(slug) {
    try {
      // Load costs for budget bars
      const costs = await App.api('GET', `/api/${slug}/costs`);
      App._renderBudgetBars(costs);

      // Load settings for gates + auto-advance
      const settings = await App.api('GET', `/api/${slug}/settings`);
      App._renderGates(settings.gates, slug);
      App._renderAutoAdvance(settings.auto_advance);

      // Load status for top bar
      const status = await App.api('GET', `/api/${slug}/status`);
      App._renderTopBar(status, costs);
    } catch (e) {
      // Silently fail — panels show "Loading..."
    }
  },

  _renderBudgetBars(costs) {
    const totalSpent = (costs.totals || {}).total || 0;
    const budgets = costs.api_budgets || {};
    let totalBudget = 0;
    for (const [, cfg] of Object.entries(budgets)) {
      totalBudget += cfg.budget_usd || 0;
    }

    const pct = totalBudget > 0 ? (totalSpent / totalBudget * 100) : 0;
    const totalLabel = document.getElementById('budget-total-label');
    const totalFill = document.getElementById('budget-total-fill');
    if (totalLabel) totalLabel.textContent = `${App.formatCost(totalSpent)} / ${App.formatCost(totalBudget)}`;
    if (totalFill) {
      totalFill.style.width = Math.min(pct, 100) + '%';
      totalFill.className = `h-full rounded-full transition-all ${App.budgetColor(pct)}`;
    }

    // Update top bar spend
    const topSpend = document.getElementById('top-total-spend');
    if (topSpend) topSpend.textContent = App.formatCost(totalSpent);

    // Per-API bars
    const container = document.getElementById('budget-api-bars');
    if (!container) return;
    container.innerHTML = '';

    const byApi = costs.by_api || {};
    for (const [api, cfg] of Object.entries(budgets)) {
      if (!cfg.enabled) continue;
      const spent = (byApi[api] || {}).spent || 0;
      const budget = cfg.budget_usd || 0;
      const apiPct = budget > 0 ? (spent / budget * 100) : 0;

      container.innerHTML += `
        <div>
          <div class="flex justify-between text-xs text-gray-500 mb-0.5">
            <span>${App.escapeHtml(api)}</span>
            <span>${App.formatCost(spent)} / ${App.formatCost(budget)}</span>
          </div>
          <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">
            <div class="h-full rounded-full transition-all ${App.budgetColor(apiPct)}" style="width:${Math.min(apiPct, 100)}%"></div>
          </div>
        </div>
      `;
    }
  },

  _renderGates(gates, slug) {
    const container = document.getElementById('gate-list');
    if (!container) return;
    container.innerHTML = '';

    for (const [name, gate] of Object.entries(gates || {})) {
      const label = name.replace(/_/g, ' ');
      if (gate.approved) {
        container.innerHTML += `
          <div class="gate-open text-xs">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
            ${App.escapeHtml(label)}
          </div>
        `;
      } else {
        container.innerHTML += `
          <div class="gate-locked text-xs cursor-pointer hover:text-gray-400"
               onclick="App.approveGate('${slug}', '${name}')">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
            ${App.escapeHtml(label)}
          </div>
        `;
      }
    }
  },

  _renderAutoAdvance(autoAdvance) {
    const container = document.getElementById('auto-advance-toggle');
    if (!container) return;
    const enabled = autoAdvance && autoAdvance.enabled;
    container.innerHTML = `
      <span class="text-xs ${enabled ? 'text-babylon-400' : 'text-gray-600'}">
        ${enabled ? 'ON' : 'OFF'}
      </span>
      <span class="text-xs text-gray-600">
        ${enabled ? 'Stages chain automatically' : 'Manual stage runs only'}
      </span>
    `;
  },

  _renderTopBar(status, costs) {
    const badge = document.getElementById('top-pipeline-badge');
    if (badge) {
      const stage = status.pipeline_stage || 'not_started';
      badge.textContent = stage.replace(/_/g, ' ');
    }
  },

  // ----------------------------------------------------------------
  // Chapter list in sidebar
  // ----------------------------------------------------------------

  async loadChapterList(slug) {
    const container = document.getElementById('chapter-list');
    if (!container) return;

    try {
      const status = await App.api('GET', `/api/${slug}/status`);
      const chapters = status.chapters || [];
      if (chapters.length === 0) {
        container.innerHTML = '<div class="text-gray-600 text-xs">No chapters yet</div>';
        return;
      }

      container.innerHTML = chapters.map(ch => {
        const dotClass = ch.status === 'approved' || ch.status === 'complete' ? 'complete'
          : ch.status === 'pending' ? 'pending'
          : 'in-progress';
        return `
          <a href="/project/${slug}/dashboard#${ch.chapter_id}"
             class="flex items-center gap-2 px-2 py-1 rounded hover:bg-gray-800 text-gray-400 hover:text-gray-200">
            <span class="chapter-dot ${dotClass}"></span>
            <span class="truncate">${App.escapeHtml(ch.title || ch.chapter_id)}</span>
          </a>
        `;
      }).join('');
    } catch (e) {
      container.innerHTML = '<div class="text-gray-600 text-xs">Error loading chapters</div>';
    }
  },

  // ----------------------------------------------------------------
  // Gate approval
  // ----------------------------------------------------------------

  async approveGate(slug, gateName) {
    const label = gateName.replace(/_/g, ' ');
    if (!confirm(`Approve gate: ${label}?\n\nThis unlocks the next pipeline stage.`)) return;

    try {
      await App.api('POST', `/api/${slug}/gates/approve`, { gate_name: gateName });
      App.showToast(`Gate "${label}" approved`, 'success');
      App.loadRightPanel(slug);
      // Refresh dashboard stepper if on dashboard page
      if (typeof Dashboard !== 'undefined' && Dashboard.slug) {
        Dashboard.loadStatus();
      }
    } catch (e) {
      // Error toast already shown by api()
    }
  },

  // ----------------------------------------------------------------
  // Modal helpers
  // ----------------------------------------------------------------

  openModal(html) {
    App.closeModal();
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'app-modal';
    overlay.onclick = (e) => { if (e.target === overlay) App.closeModal(); };
    overlay.innerHTML = `<div class="modal-content">${html}</div>`;
    document.body.appendChild(overlay);
  },

  closeModal() {
    const modal = document.getElementById('app-modal');
    if (modal) modal.remove();
  },

  // ----------------------------------------------------------------
  // Version check & update
  // ----------------------------------------------------------------

  async checkForUpdates() {
    try {
      const data = await App.api('GET', '/api/version');
      if (data.update_available) {
        const banner = document.getElementById('update-banner');
        const text = document.getElementById('update-version-text');
        if (banner && text) {
          text.textContent = `v${data.latest} available (current: v${data.current})`;
          banner.classList.remove('hidden');
        }
      }
    } catch (e) {
      // Silently fail — version check is non-critical
    }
  },

  async performUpdate() {
    const btn = document.getElementById('update-btn');
    if (!btn) return;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Updating...';

    try {
      await App.api('POST', '/api/update');
    } catch (e) {
      // Expected — server will die mid-response
    }

    btn.textContent = 'Restarting...';
    App.showToast('Server is restarting — reconnecting...', 'info', 0);

    // Poll until server comes back up
    const poll = setInterval(async () => {
      try {
        const res = await fetch('/api/version', { signal: AbortSignal.timeout(2000) });
        if (res.ok) {
          clearInterval(poll);
          App.showToast('Updated successfully! Reloading...', 'success');
          setTimeout(() => window.location.reload(), 500);
        }
      } catch (e) {
        // Server still restarting — keep polling
      }
    }, 2000);
  },

};

// Global keyboard shortcuts
document.addEventListener('keydown', (e) => {
  // Don't capture when typing in inputs
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

  // Escape closes modals
  if (e.key === 'Escape') {
    App.closeModal();
    return;
  }

  // ? opens help
  if (e.key === '?') {
    e.preventDefault();
    App.showHelp();
    return;
  }

  // 1-5 navigate to pages
  if (!e.ctrlKey && !e.altKey && !e.metaKey && App.slug) {
    const navMap = {
      '1': 'dashboard',
      '2': 'storyboard',
      '3': 'voices',
      '4': 'assets',
      '5': 'costs',
    };
    if (navMap[e.key]) {
      window.location.href = `/project/${App.slug}/${navMap[e.key]}`;
    }
  }
});

App.showHelp = function() {
  const html = `
    <h3 class="text-base font-semibold text-gray-100 mb-4">Keyboard Shortcuts</h3>
    <table class="w-full text-sm">
      <tbody>
        <tr class="border-b border-gray-800/50">
          <td class="py-2 pr-4"><kbd class="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 text-xs">?</kbd></td>
          <td class="py-2 text-gray-300">Show this help</td>
        </tr>
        <tr class="border-b border-gray-800/50">
          <td class="py-2 pr-4"><kbd class="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 text-xs">Esc</kbd></td>
          <td class="py-2 text-gray-300">Close modal / panel</td>
        </tr>
        <tr class="border-b border-gray-800/50">
          <td class="py-2 pr-4"><kbd class="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 text-xs">1</kbd>-<kbd class="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 text-xs">5</kbd></td>
          <td class="py-2 text-gray-300">Navigate pages (Dashboard / Storyboard / Voices / Assets / Costs)</td>
        </tr>
      </tbody>
    </table>
    <h4 class="text-sm font-semibold text-gray-300 mt-4 mb-2">Storyboard Review</h4>
    <table class="w-full text-sm">
      <tbody>
        <tr class="border-b border-gray-800/50">
          <td class="py-2 pr-4"><kbd class="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 text-xs">&larr;</kbd> <kbd class="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 text-xs">&rarr;</kbd></td>
          <td class="py-2 text-gray-300">Previous / Next shot</td>
        </tr>
        <tr class="border-b border-gray-800/50">
          <td class="py-2 pr-4"><kbd class="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 text-xs">A</kbd></td>
          <td class="py-2 text-gray-300">Approve current shot</td>
        </tr>
        <tr>
          <td class="py-2 pr-4"><kbd class="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 text-xs">R</kbd></td>
          <td class="py-2 text-gray-300">Reject current shot</td>
        </tr>
      </tbody>
    </table>
  `;
  App.openModal(html);
};
