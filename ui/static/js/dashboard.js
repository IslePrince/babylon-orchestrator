/**
 * dashboard.js — Production board logic.
 *
 * Renders pipeline stepper, chapter cards, expandable detail panel,
 * and shot detail modal.
 */

const Dashboard = {

  slug: null,
  status: null,

  async load(slug) {
    Dashboard.slug = slug;
    try {
      Dashboard.status = await App.api('GET', `/api/${slug}/status`);
      Dashboard.renderStepper(Dashboard.status);
      Dashboard.renderChapterGrid(Dashboard.status);
      Dashboard.renderVoiceCasting(Dashboard.status);
      Dashboard._populateChapterSelect(Dashboard.status);
    } catch (e) {
      document.getElementById('chapter-grid').innerHTML =
        '<div class="text-gray-500">Failed to load dashboard. Is a project selected?</div>';
    }
  },

  _populateChapterSelect(status) {
    const sel = document.getElementById('stage-chapter-select');
    if (!sel) return;
    const chapters = status.chapters || [];
    sel.innerHTML = '<option value="">All chapters</option>' +
      chapters.map(ch =>
        `<option value="${ch.chapter_id}">${App.escapeHtml(ch.title || ch.chapter_id)}</option>`
      ).join('');
  },

  // Map chapter status → page name for clickable badges
  _statusToPage(status) {
    const map = {
      screenplay: 'screenplay',
      cinematographer: 'storyboard',
      storyboard: 'storyboard',
      audio: 'voices',
      draft: 'screenplay',
      approved: 'storyboard',
    };
    return map[status] || null;
  },

  // Stages that require a chapter_id to be selected
  _chapterRequiredStages: ['screenplay', 'cinematographer', 'storyboard', 'voice_recording', 'sound_fx', 'audio_score'],

  async runStage() {
    const stage = document.getElementById('stage-select').value;
    const chapterId = document.getElementById('stage-chapter-select').value;
    const dryRun = document.getElementById('stage-dry-run').checked;
    const sourcePath = document.getElementById('stage-source-path').value.trim();

    // Validate required inputs
    if (stage === 'ingest' && !sourcePath) {
      App.showToast('Enter the path to your source text file', 'warning');
      return;
    }

    // "All chapters" → run sequentially for each chapter
    if (Dashboard._chapterRequiredStages.includes(stage) && !chapterId) {
      const chapters = (Dashboard.status && Dashboard.status.chapters) || [];
      if (chapters.length === 0) {
        App.showToast('No chapters found. Run Ingest first.', 'warning');
        return;
      }
      const forceSeq = document.getElementById('stage-force') && document.getElementById('stage-force').checked;
      App.showToast(`Running ${stage} for ${chapters.length} chapters sequentially...`, 'info');
      for (const ch of chapters) {
        try {
          const seqOpts = { chapter_id: ch.chapter_id, dry_run: dryRun };
          if (stage === 'storyboard' && forceSeq) seqOpts.force = true;
          await StageRunner.run(stage, seqOpts);
        } catch (e) {
          // Continue to next chapter on failure
        }
      }
      return;
    }

    const forceRegen = document.getElementById('stage-force') && document.getElementById('stage-force').checked;

    const opts = { dry_run: dryRun };
    if (chapterId) opts.chapter_id = chapterId;
    if (stage === 'ingest' && sourcePath) opts.source = sourcePath;
    if (['storyboard', 'character_sheets'].includes(stage) && forceRegen) opts.force = true;

    StageRunner.run(stage, opts).catch(() => {});
  },

  _onStageChange() {
    const stage = document.getElementById('stage-select').value;
    const sourceInput = document.getElementById('stage-source-path');
    const chapterSelect = document.getElementById('stage-chapter-select');
    // Show source input only for ingest; hide chapter select for project-wide stages
    const noChapterStages = ['ingest', 'characters', 'character_sheets', 'lora_training', 'assets', 'meshes'];
    if (sourceInput) sourceInput.classList.toggle('hidden', stage !== 'ingest');
    if (chapterSelect) chapterSelect.classList.toggle('hidden', noChapterStages.includes(stage));
    const forceLabel = document.getElementById('stage-force-label');
    if (forceLabel) forceLabel.classList.toggle('hidden', !['storyboard', 'character_sheets'].includes(stage));
  },

  // ----------------------------------------------------------------
  // Pipeline Stepper
  // ----------------------------------------------------------------

  renderStepper(status) {
    const container = document.getElementById('pipeline-stepper');
    if (!container) return;

    const stages = [
      'ingest', 'world_bible', 'screenplay', 'characters', 'character_sheets',
      'voice_recording', 'screenplay_review',
      'cinematographer', 'storyboard',
      'editing_room', 'sound_fx', 'audio_score', 'asset_manifest',
      'mesh_generation', 'animation', 'scene_assembly',
      'preview_render', 'final_render', 'post_export'
    ];
    const current = status.pipeline_stage || 'not_started';
    const currentIdx = stages.indexOf(current);

    const gates = status.gates || {};
    const gatePositions = {
      'screenplay_to_voice_recording': { after: 'character_sheets' },
      'cut_to_sound': { after: 'editing_room' },
      'sound_to_assets': { after: 'audio_score' },
      'assets_to_scene': { after: 'animation' },
      'preview_to_final': { after: 'preview_render' },
    };

    // Map stepper stage names → page URLs (null = no dedicated page yet)
    const stageLinks = {
      'ingest': 'ingest', 'world_bible': 'ingest',
      'screenplay': 'screenplay', 'characters': 'characters',
      'character_sheets': 'characters',
      'voice_recording': 'voices', 'screenplay_review': 'screenplay-review',
      'cinematographer': 'storyboard', 'storyboard': 'storyboard',
      'editing_room': 'editing-room', 'sound_fx': 'editing-room',
      'audio_score': 'editing-room',
      'asset_manifest': 'assets', 'mesh_generation': 'assets',
      'animation': 'assets',
    };

    let html = '<div class="flex items-center gap-1 overflow-x-auto pb-2">';
    stages.forEach((stage, i) => {
      const label = stage.replace(/_/g, ' ');
      const isComplete = i < currentIdx;
      const isCurrent = stage === current;
      const isPending = i > currentIdx;

      let cls = 'bg-gray-800 text-gray-500';
      if (isComplete) cls = 'bg-green-900/50 text-green-400';
      if (isCurrent) cls = 'bg-babylon-900 text-babylon-300 ring-1 ring-babylon-500';

      const page = stageLinks[stage];
      const tag = page ? 'a' : 'div';
      const href = page ? ` href="/project/${Dashboard.slug}/${page}"` : '';
      const hoverCls = page ? ' hover:ring-1 hover:ring-gray-500 transition-all' : '';
      html += `<${tag}${href} class="px-2 py-1 rounded text-xs whitespace-nowrap cursor-pointer ${cls}${hoverCls}">${label}</${tag}>`;

      // Gate icon after this stage?
      for (const [gateName, pos] of Object.entries(gatePositions)) {
        if (pos.after === stage) {
          const gate = gates[gateName];
          const isApproved = gate && gate.approved;
          const gateLabel = gateName.replace(/_/g, ' ');
          if (isApproved) {
            html += `<div class="flex-shrink-0" title="${gateLabel} (approved)">
              <svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 11V7a4 4 0 118 0m-4 8v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2z"/></svg>
            </div>`;
          } else {
            html += `<div class="flex-shrink-0 cursor-pointer group" title="Click to approve: ${gateLabel}"
                          onclick="App.approveGate('${Dashboard.slug}', '${gateName}')">
              <svg class="w-4 h-4 text-amber-500 group-hover:text-amber-300 transition-colors" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
            </div>`;
          }
        }
      }

      if (i < stages.length - 1) {
        html += '<div class="w-3 h-px bg-gray-700 flex-shrink-0"></div>';
      }
    });
    html += '</div>';
    container.innerHTML = html;
  },

  // ----------------------------------------------------------------
  // Chapter Cards Grid
  // ----------------------------------------------------------------

  renderChapterGrid(status) {
    const container = document.getElementById('chapter-grid');
    if (!container) return;

    const chapters = status.chapters || [];
    if (chapters.length === 0) {
      container.innerHTML = `
        <div class="col-span-full text-center py-12">
          <div class="text-gray-600 text-lg mb-2">No chapters yet</div>
          <div class="text-gray-700 text-sm">Run the Ingest stage to parse your source text into chapters.</div>
        </div>
      `;
      return;
    }

    container.innerHTML = chapters.map(ch => {
      const production = ch.production || {};
      const totalShots = production.total_shots || 0;
      const sbApproved = production.storyboard_approved || production.shots_approved || 0;
      const shotPct = totalShots > 0 ? Math.round(sbApproved / totalShots * 100) : 0;
      const audioTotal = production.audio_lines_total || 0;
      const audioGen = production.audio_lines_generated || 0;
      const audioPct = audioTotal > 0 ? Math.round(audioGen / audioTotal * 100) : 0;
      const cost = (ch.costs || {}).chapter_total_usd || 0;

      const statusPage = Dashboard._statusToPage(ch.status);
      const statusLink = statusPage
        ? `/project/${Dashboard.slug}/${statusPage}?chapter=${ch.chapter_id}`
        : null;
      const sbLink = `/project/${Dashboard.slug}/storyboard?chapter=${ch.chapter_id}`;
      const audioLink = `/project/${Dashboard.slug}/voices`;

      return `
        <div class="card p-4 cursor-pointer" onclick="Dashboard.expandChapter('${ch.chapter_id}')">
          <div class="flex items-start justify-between mb-2">
            <h3 class="text-sm font-medium text-gray-200 truncate">${App.escapeHtml(ch.title || ch.chapter_id)}</h3>
            ${statusLink
              ? `<a href="${statusLink}" onclick="event.stopPropagation()" class="hover:opacity-80 transition-opacity">${App.statusBadge(ch.status || 'pending')}</a>`
              : App.statusBadge(ch.status || 'pending')}
          </div>
          <div class="space-y-2 mt-3">
            <a href="${sbLink}" onclick="event.stopPropagation()" class="block group">
              <div class="flex justify-between text-xs text-gray-500 mb-1">
                <span class="group-hover:text-babylon-400 transition-colors">Storyboard</span>
                <span>${sbApproved}/${totalShots}</span>
              </div>
              <div class="progress-bar">
                <div class="progress-fill bg-babylon-500" style="width:${shotPct}%"></div>
              </div>
            </a>
            <a href="${audioLink}" onclick="event.stopPropagation()" class="block group">
              <div class="flex justify-between text-xs text-gray-500 mb-1">
                <span class="group-hover:text-green-400 transition-colors">Audio</span>
                <span>${audioGen}/${audioTotal}</span>
              </div>
              <div class="progress-bar">
                <div class="progress-fill bg-green-500" style="width:${audioPct}%"></div>
              </div>
            </a>
          </div>
          <div class="flex justify-between items-center mt-3">
            <span class="text-xs text-gray-500">${App.formatCost(cost)}</span>
            <span class="text-xs text-gray-600">${ch.chapter_id}</span>
          </div>
        </div>
      `;
    }).join('');
  },

  // ----------------------------------------------------------------
  // Voice Casting Summary (right sidebar)
  // ----------------------------------------------------------------

  renderVoiceCasting(status) {
    const container = document.getElementById('voice-casting-summary');
    if (!container) return;
    const chars = status.voice_casting || [];
    if (chars.length === 0) { container.innerHTML = ''; return; }

    const cast = chars.filter(c => c.has_voice).length;
    let html = `<h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Voice Casting</h3>`;
    html += `<div class="text-xs text-gray-400 mb-2">${cast}/${chars.length} assigned</div>`;
    html += '<div class="space-y-1 max-h-48 overflow-y-auto">';
    // Unassigned first, then assigned
    const sorted = [...chars].sort((a, b) => a.has_voice - b.has_voice);
    for (const c of sorted) {
      const color = c.has_voice ? 'text-green-400' : 'text-amber-400';
      const link = `/project/${Dashboard.slug}/voices`;
      html += `<a href="${link}" class="flex items-center gap-2 text-xs hover:bg-gray-800 rounded px-1 py-0.5">
        <span class="${color}">${c.has_voice ? '\u25CF' : '\u25CB'}</span>
        <span class="text-gray-300 truncate">${App.escapeHtml(c.display_name)}</span>
      </a>`;
    }
    html += '</div>';
    container.innerHTML = html;
  },

  // ----------------------------------------------------------------
  // Chapter Detail Panel
  // ----------------------------------------------------------------

  async expandChapter(chapterId) {
    const panel = document.getElementById('chapter-detail');
    if (!panel) return;

    // Toggle if same chapter clicked
    if (panel.dataset.chapter === chapterId && !panel.classList.contains('hidden')) {
      panel.classList.add('hidden');
      return;
    }

    panel.dataset.chapter = chapterId;
    panel.classList.remove('hidden');
    panel.innerHTML = '<div class="skeleton h-24 w-full"></div>';

    try {
      const detail = await App.api('GET', `/api/${Dashboard.slug}/chapter/${chapterId}`);
      Dashboard.renderChapterDetail(panel, detail);
    } catch (e) {
      panel.innerHTML = '<div class="text-red-400 text-sm">Failed to load chapter detail.</div>';
    }
  },

  renderChapterDetail(panel, detail) {
    const scenes = detail.scenes || [];
    let html = `
      <div class="card p-4 overflow-hidden">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-base font-semibold text-gray-100">
            ${App.escapeHtml(detail.title || detail.chapter_id)}
          </h3>
          <button onclick="document.getElementById('chapter-detail').classList.add('hidden')"
                  class="text-gray-500 hover:text-gray-300 text-sm">Close</button>
        </div>
    `;

    if (scenes.length === 0) {
      html += '<div class="text-gray-600 text-sm">No scenes generated yet. Run the Cinematographer stage.</div>';
    } else {
      // Chapter-level audio summary
      const prod = detail.production || {};
      const audioGen = prod.audio_lines_generated || 0;
      const audioTotal = prod.audio_lines_total || 0;
      if (audioTotal > 0) {
        html += `<div class="text-xs text-gray-400 mb-3">Audio lines: ${audioGen}/${audioTotal} generated</div>`;
      }

      html += '<div class="overflow-x-auto"><table class="w-full text-sm">';
      html += `<thead><tr class="text-xs text-gray-500 border-b border-gray-800">
        <th class="text-left py-2 pr-4">Scene</th>
        <th class="text-left py-2 pr-4">Title</th>
        <th class="text-right py-2 pr-4">Shots</th>
        <th class="text-right py-2 pr-4">SB Approved</th>
        <th class="text-right py-2 pr-4">Dialogue</th>
        <th class="text-right py-2 pr-4">Audio OK</th>
        <th class="text-right py-2">Flagged</th>
      </tr></thead><tbody>`;

      scenes.forEach(sc => {
        const shots = sc.shots || {};
        const flagged = (shots.flagged || []).length;
        const dialogueLines = sc.dialogue_lines || 0;
        const chId = detail.chapter_id;
        const scId = sc.scene_id;
        html += `<tr class="border-b border-gray-800/50 hover:bg-gray-800/30 cursor-pointer"
                     onclick="Dashboard.toggleShotFilmstrip('${chId}', '${scId}', this)">
          <td class="py-2 pr-4 text-gray-400">${App.escapeHtml(scId)}</td>
          <td class="py-2 pr-4 text-gray-300">${App.escapeHtml(sc.title || '')}</td>
          <td class="py-2 pr-4 text-right text-gray-400">${shots.total || 0}</td>
          <td class="py-2 pr-4 text-right text-gray-400">${shots.storyboard_approved || 0}</td>
          <td class="py-2 pr-4 text-right text-gray-400">${dialogueLines}</td>
          <td class="py-2 pr-4 text-right text-gray-400">${shots.audio_approved || 0}</td>
          <td class="py-2 text-right ${flagged > 0 ? 'text-red-400' : 'text-gray-600'}">${flagged}</td>
        </tr>
        <tr class="filmstrip-row hidden" data-scene="${scId}">
          <td colspan="7" class="py-2" style="max-width:0">
            <div class="filmstrip-container flex gap-2 overflow-x-auto py-2 px-1" id="filmstrip-${scId}">
              <div class="text-xs text-gray-600">Click to load shots...</div>
            </div>
          </td>
        </tr>`;
      });

      html += '</tbody></table></div>';
    }

    html += '</div>';
    panel.innerHTML = html;
  },

  // ----------------------------------------------------------------
  // Shot Filmstrip (inline under scene row)
  // ----------------------------------------------------------------

  async toggleShotFilmstrip(chapterId, sceneId, rowEl) {
    const filmstripRow = rowEl.nextElementSibling;
    if (!filmstripRow) return;

    if (!filmstripRow.classList.contains('hidden')) {
      filmstripRow.classList.add('hidden');
      return;
    }

    filmstripRow.classList.remove('hidden');
    const container = document.getElementById(`filmstrip-${sceneId}`);
    if (!container || container.dataset.loaded) return;

    container.innerHTML = '<div class="skeleton h-20 w-32"></div>';

    try {
      const detail = await App.api('GET', `/api/${Dashboard.slug}/chapter/${chapterId}`);
      const scene = (detail.scenes || []).find(s => s.scene_id === sceneId);
      const shotIds = (scene && scene.shots && scene.shots.shot_ids) || [];

      if (shotIds.length === 0) {
        container.innerHTML = '<div class="text-xs text-gray-600">No shots in this scene. Run the Cinematographer stage.</div>';
        container.dataset.loaded = '1';
        return;
      }

      container.innerHTML = shotIds.map(sid => {
        // Image path: chapters/{ch}/shots/{shot_id}/storyboard.png
        const imgUrl = `/api/${Dashboard.slug}/image/chapters/${chapterId}/shots/${sid}/storyboard.png`;
        return `
          <div class="flex-shrink-0 cursor-pointer group" onclick="Dashboard.openShotModal('${chapterId}', '${sceneId}', '${sid}')">
            <div class="w-32 h-20 bg-gray-800 rounded overflow-hidden border border-gray-700 group-hover:border-babylon-500 transition-colors">
              <img src="${imgUrl}" alt="${sid}" class="w-full h-full object-cover"
                   onerror="this.parentNode.innerHTML='<div class=\\'flex items-center justify-center h-full text-xs text-gray-600\\'>No image</div>'" />
            </div>
            <div class="text-xs text-gray-500 mt-1 text-center truncate w-32">${sid}</div>
          </div>
        `;
      }).join('');

      container.dataset.loaded = '1';
    } catch (e) {
      container.innerHTML = '<div class="text-xs text-red-400">Failed to load shots</div>';
    }
  },

  // ----------------------------------------------------------------
  // Shot Detail Modal
  // ----------------------------------------------------------------

  async openShotModal(chapterId, sceneId, shotId) {
    App.openModal('<div class="skeleton h-64 w-full"></div>');

    try {
      const shot = await App.api('GET', `/api/${Dashboard.slug}/shot/${chapterId}/${sceneId}/${shotId}`);
      Dashboard._renderShotModal(shot, chapterId, sceneId, shotId);
    } catch (e) {
      App.openModal('<div class="text-red-400">Failed to load shot details.</div>');
    }
  },

  _renderShotModal(shot, chapterId, sceneId, shotId) {
    const sb = shot.storyboard || {};
    const cinematic = shot.cinematic || {};
    const cameraMove = cinematic.camera_movement || {};
    const audio = shot.audio || {};
    const meta = shot.meta || {};
    const flags = meta.flags || [];
    // Image path matches storyboard output: chapters/{ch}/shots/{shot_id}/storyboard.png
    const imgUrl = `/api/${Dashboard.slug}/image/chapters/${chapterId}/shots/${shotId}/storyboard.png`;

    const isApproved = sb.approved === true;
    const isReviewed = sb.reviewed === true;
    const approvedCls = isApproved ? 'bg-green-600 hover:bg-green-500' : 'bg-green-800 hover:bg-green-700';
    const rejectedCls = !isApproved && isReviewed ? 'bg-red-600 hover:bg-red-500' : 'bg-red-800 hover:bg-red-700';

    let html = `
      <div class="flex gap-6">
        <!-- Left: Image -->
        <div class="flex-shrink-0">
          <div class="w-80 h-48 bg-gray-800 rounded overflow-hidden border border-gray-700">
            <img src="${imgUrl}" alt="${shotId}" class="w-full h-full object-cover"
                 onerror="this.parentNode.innerHTML='<div class=\\'flex items-center justify-center h-full text-gray-600\\'>No storyboard image</div>'" />
          </div>
          <!-- Actions -->
          <div class="flex gap-2 mt-3">
            <button onclick="Dashboard.reviewShot('${chapterId}','${sceneId}','${shotId}','approve')"
                    class="${approvedCls} text-white text-sm px-3 py-1.5 rounded transition-colors flex-1">
              ${isApproved ? 'Approved' : 'Approve'}
            </button>
            <button onclick="Dashboard.reviewShot('${chapterId}','${sceneId}','${shotId}','reject')"
                    class="${rejectedCls} text-white text-sm px-3 py-1.5 rounded transition-colors flex-1">
              Reject
            </button>
          </div>
        </div>

        <!-- Right: Details -->
        <div class="flex-1 min-w-0">
          <h3 class="text-lg font-semibold text-gray-100 mb-1">${App.escapeHtml(shotId)}</h3>
          <div class="text-xs text-gray-500 mb-3">${App.escapeHtml(chapterId)} / ${App.escapeHtml(sceneId)}</div>

          ${shot.label ? `<p class="text-sm text-gray-300 mb-3">${App.escapeHtml(shot.label)}</p>` : ''}

          <div class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm mb-4">
            ${cinematic.shot_type ? `<div class="text-gray-500">Shot type</div><div class="text-gray-300">${App.escapeHtml(cinematic.shot_type)}</div>` : ''}
            ${cinematic.framing ? `<div class="text-gray-500">Framing</div><div class="text-gray-300">${App.escapeHtml(cinematic.framing)}</div>` : ''}
            ${cameraMove.type ? `<div class="text-gray-500">Movement</div><div class="text-gray-300">${App.escapeHtml(cameraMove.type)}</div>` : ''}
            ${cinematic.lens_mm_equiv ? `<div class="text-gray-500">Lens</div><div class="text-gray-300">${cinematic.lens_mm_equiv}mm</div>` : ''}
            ${cinematic.composition_notes ? `<div class="text-gray-500">Composition</div><div class="text-gray-300 text-xs">${App.escapeHtml(cinematic.composition_notes)}</div>` : ''}
            <div class="text-gray-500">Storyboard</div><div>${App.statusBadge(isApproved ? 'approved' : isReviewed ? 'flagged' : 'pending')}</div>
          </div>

          ${flags.length > 0 ? `
            <div class="mb-3">
              <h4 class="text-xs font-semibold text-gray-500 uppercase mb-1">Flags</h4>
              ${flags.map(f => `
                <div class="text-xs text-red-400 mb-1">${App.escapeHtml(typeof f === 'string' ? f : f.reason || 'Flagged')}</div>
              `).join('')}
            </div>
          ` : ''}

          ${(shot.characters_in_frame || []).length > 0 ? `
            <div class="mb-3">
              <h4 class="text-xs font-semibold text-gray-500 uppercase mb-1">Characters</h4>
              <div class="flex flex-wrap gap-1">
                ${shot.characters_in_frame.map(c => `<span class="badge badge-draft">${App.escapeHtml(typeof c === 'string' ? c : c.character_id || '')}</span>`).join('')}
              </div>
            </div>
          ` : ''}

          ${(shot.dialogue_in_shot || []).length > 0 ? `
            <div>
              <h4 class="text-xs font-semibold text-gray-500 uppercase mb-1">Dialogue</h4>
              ${shot.dialogue_in_shot.map(d => `
                <div class="text-xs text-gray-400 mb-1">${App.escapeHtml(d)}</div>
              `).join('')}
            </div>
          ` : ''}
        </div>
      </div>
    `;

    App.openModal(html);
  },

  async reviewShot(chapterId, sceneId, shotId, action) {
    let notes = '';
    if (action === 'reject') {
      notes = prompt('Rejection notes (optional):') || '';
    }

    try {
      await App.api('POST', `/api/${Dashboard.slug}/shot/${shotId}/review`, {
        action,
        chapter_id: chapterId,
        scene_id: sceneId,
        notes,
      });
      App.showToast(`Shot ${action}d`, action === 'approve' ? 'success' : 'warning');
      // Refresh the modal
      Dashboard.openShotModal(chapterId, sceneId, shotId);
      // Refresh dashboard data
      Dashboard.load(Dashboard.slug);
    } catch (e) {
      App.showToast(`Failed to ${action} shot: ${e.message}`, 'error');
    }
  },

};
