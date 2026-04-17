/**
 * editing_room.js — Editing Room / First Cut
 *
 * Lets the filmmaker disable shots, adjust durations, and hear
 * dialogue before locking the cut for SFX and score stages.
 */

const EditingRoom = {
  slug: null,
  chapterId: null,
  shots: [],
  summary: {},
  currentIdx: -1,
  playing: false,
  playTimer: null,

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  async init(slug) {
    EditingRoom.slug = slug;

    // Populate chapter select
    try {
      const status = await App.api('GET', `/api/${slug}/status`);
      const sel = document.getElementById('er-chapter-select');
      (status.chapters || []).forEach(ch => {
        const opt = document.createElement('option');
        opt.value = ch.chapter_id;
        opt.textContent = ch.title || ch.chapter_id;
        sel.appendChild(opt);
      });
      // Auto-select first chapter
      if (status.chapters && status.chapters.length) {
        sel.value = status.chapters[0].chapter_id;
        EditingRoom.load();
      }
    } catch (e) {
      console.error('Failed to load project status', e);
    }

    // Keyboard shortcuts
    document.addEventListener('keydown', EditingRoom._onKey);
  },

  // ------------------------------------------------------------------
  // Load chapter data
  // ------------------------------------------------------------------

  async load() {
    const sel = document.getElementById('er-chapter-select');
    const chapterId = sel.value;
    if (!chapterId) return;

    EditingRoom.chapterId = chapterId;

    try {
      const data = await App.api('GET', `/api/${EditingRoom.slug}/editing-room/${chapterId}`);
      EditingRoom.shots = data.shots || [];
      EditingRoom.summary = data.summary || {};
      EditingRoom._renderTimeline();
      EditingRoom._renderStats();

      // Show lock-cut section
      const lockEl = document.getElementById('er-lock-cut');
      if (lockEl) lockEl.style.display = EditingRoom.shots.length ? '' : 'none';

      // Select first shot
      if (EditingRoom.shots.length) {
        EditingRoom.selectShot(0);
      } else {
        document.getElementById('er-detail').style.display = 'none';
      }
    } catch (e) {
      App.showToast('Failed to load editing room data', 'error');
    }
  },

  // ------------------------------------------------------------------
  // Timeline rendering
  // ------------------------------------------------------------------

  _renderTimeline() {
    const container = document.getElementById('er-timeline');
    if (!EditingRoom.shots.length) {
      container.innerHTML = '<div class="text-gray-600 text-sm p-4">No shots found. Run cinematographer first.</div>';
      return;
    }

    let html = '';
    let lastScene = null;

    EditingRoom.shots.forEach((shot, idx) => {
      // Scene divider
      if (shot.scene_id !== lastScene) {
        if (lastScene !== null) {
          html += '<div class="flex-shrink-0 w-px bg-gray-700 mx-1 self-stretch"></div>';
        }
        html += `<div class="flex-shrink-0 flex items-end pb-1">
          <span class="text-[10px] text-gray-600 -rotate-90 whitespace-nowrap origin-bottom-left translate-y-full">${shot.scene_id}</span>
        </div>`;
        lastScene = shot.scene_id;
      }

      const enabled = shot.edit.enabled;
      const dur = shot.edit.duration_sec || shot.original_duration_sec;
      const hasDlg = shot.has_dialogue;
      const hasAudio = shot.audio_status === 'generated';
      const selected = idx === EditingRoom.currentIdx;

      const disabledCls = enabled ? '' : 'opacity-30';
      const selectedCls = selected ? 'ring-2 ring-babylon-500' : 'ring-1 ring-gray-700 hover:ring-gray-500';

      html += `<div class="er-timeline-card flex-shrink-0 w-24 cursor-pointer rounded-lg overflow-hidden ${selectedCls} ${disabledCls} transition-all"
                   data-idx="${idx}" onclick="EditingRoom.selectShot(${idx})">
        <div class="relative aspect-video bg-gray-800">
          <img src="${shot.image_url}?t=1" alt="" class="w-full h-full object-cover"
               onerror="this.style.display='none'">
          ${!enabled ? '<div class="absolute inset-0 flex items-center justify-center bg-black/50"><svg class="w-6 h-6 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg></div>' : ''}
        </div>
        <div class="px-1.5 py-1 bg-gray-900">
          <div class="flex items-center gap-1">
            ${hasDlg ? '<svg class="w-3 h-3 text-blue-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M18 10c0 3.866-3.582 7-8 7a8.841 8.841 0 01-4.083-.98L2 17l1.338-3.123C2.493 12.767 2 11.434 2 10c0-3.866 3.582-7 8-7s8 3.134 8 7zM7 9H5v2h2V9zm8 0h-2v2h2V9zM9 9h2v2H9V9z" clip-rule="evenodd"/></svg>' : ''}
            ${hasAudio ? '<svg class="w-3 h-3 text-green-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M9.383 3.076A1 1 0 0110 4v12a1 1 0 01-1.707.707L4.586 13H2a1 1 0 01-1-1V8a1 1 0 011-1h2.586l3.707-3.707a1 1 0 011.09-.217zM14.657 2.929a1 1 0 011.414 0A9.972 9.972 0 0119 10a9.972 9.972 0 01-2.929 7.071 1 1 0 01-1.414-1.414A7.971 7.971 0 0017 10c0-2.21-.894-4.208-2.343-5.657a1 1 0 010-1.414z" clip-rule="evenodd"/></svg>' : ''}
            <span class="text-[10px] text-gray-500 ml-auto">${dur.toFixed(1)}s</span>
          </div>
          <div class="text-[9px] text-gray-600 truncate mt-0.5">${shot.shot_id.split('_').pop()}</div>
        </div>
      </div>`;
    });

    container.innerHTML = html;
  },

  // ------------------------------------------------------------------
  // Stats rendering
  // ------------------------------------------------------------------

  _renderStats() {
    const s = EditingRoom.summary;
    document.getElementById('er-stat-enabled').textContent = s.enabled_shots || 0;
    document.getElementById('er-stat-total').textContent = s.total_shots || 0;
    document.getElementById('er-stat-original-dur').textContent = EditingRoom._formatDuration(s.original_duration_sec || 0);
    document.getElementById('er-stat-cut-dur').textContent = EditingRoom._formatDuration(s.cut_duration_sec || 0);

    const removed = s.original_duration_sec
      ? Math.round((1 - s.cut_duration_sec / s.original_duration_sec) * 100)
      : 0;
    document.getElementById('er-stat-removed').textContent = removed + '%';

    // Dialogue warning
    const warn = document.getElementById('er-dialogue-warning');
    const warnText = document.getElementById('er-dialogue-warning-text');
    if (s.dialogue_shots_disabled > 0) {
      warn.classList.remove('hidden');
      warnText.textContent = `${s.dialogue_shots_disabled} dialogue shot${s.dialogue_shots_disabled > 1 ? 's' : ''} disabled`;
    } else {
      warn.classList.add('hidden');
    }

    // Lock cut summary
    const lockSummary = document.getElementById('er-lock-summary');
    if (lockSummary) {
      lockSummary.textContent = `${s.enabled_shots} shots enabled, cut runtime: ${EditingRoom._formatDuration(s.cut_duration_sec || 0)}`;
    }
    const lockWarn = document.getElementById('er-lock-warning');
    if (lockWarn) {
      if (s.dialogue_shots_disabled > 0) {
        lockWarn.classList.remove('hidden');
        lockWarn.textContent = `Warning: ${s.dialogue_shots_disabled} dialogue shot(s) will be excluded from the cut`;
      } else {
        lockWarn.classList.add('hidden');
      }
    }
  },

  // ------------------------------------------------------------------
  // Shot selection & detail panel
  // ------------------------------------------------------------------

  selectShot(idx) {
    if (idx < 0 || idx >= EditingRoom.shots.length) return;
    EditingRoom.currentIdx = idx;
    const shot = EditingRoom.shots[idx];

    // Update timeline selection
    document.querySelectorAll('.er-timeline-card').forEach((el, i) => {
      if (i === idx) {
        el.classList.add('ring-2', 'ring-babylon-500');
        el.classList.remove('ring-1', 'ring-gray-700');
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
      } else {
        el.classList.remove('ring-2', 'ring-babylon-500');
        el.classList.add('ring-1', 'ring-gray-700');
      }
    });

    // Show detail panel
    const detail = document.getElementById('er-detail');
    detail.style.display = '';

    // Image (horizontal 16:9 master + vertical 9:16 reframe). If a
    // Wan+InfiniTalk preview video exists for this shot, render that
    // instead of the still so the user can audition the talking head.
    const imgContainer = document.getElementById('er-detail-image');
    if (shot.preview_video_url) {
      imgContainer.innerHTML = `<video src="${shot.preview_video_url}?t=${Date.now()}"
                                       class="w-full h-full object-cover"
                                       controls muted loop preload="metadata"></video>`;
    } else {
      imgContainer.innerHTML = `<img src="${shot.image_url}?t=${Date.now()}" alt="${shot.label}"
                                     class="w-full h-full object-cover"
                                     onerror="this.outerHTML='<span class=\\'text-gray-600 text-sm\\'>No storyboard</span>'">`;
    }
    const vContainer = document.getElementById('er-detail-image-vertical');
    if (vContainer) {
      if (shot.preview_video_url_vertical) {
        vContainer.innerHTML = `<video src="${shot.preview_video_url_vertical}?t=${Date.now()}"
                                       class="w-full h-full object-cover"
                                       controls muted loop preload="metadata"></video>`;
      } else if (shot.image_url_vertical) {
        vContainer.innerHTML = `<img src="${shot.image_url_vertical}?t=${Date.now()}" alt="${shot.label} (vertical)"
                                     class="w-full h-full object-cover"
                                     onerror="this.outerHTML='<span class=\\'text-[10px] text-gray-600\\'>No 9:16</span>'">`;
      }
    }

    // Info
    document.getElementById('er-detail-shot-id').textContent = shot.shot_id;
    document.getElementById('er-detail-shot-type').textContent = shot.shot_type || 'unknown';
    document.getElementById('er-detail-label').textContent = shot.label;

    // Characters
    const charsEl = document.getElementById('er-detail-characters');
    charsEl.innerHTML = (shot.characters_in_frame || []).map(c =>
      `<span class="px-2 py-0.5 rounded-full text-xs bg-gray-800 text-gray-300">${c}</span>`
    ).join('');

    // Dialogue
    const dlgEl = document.getElementById('er-detail-dialogue');
    if (shot.dialogue_in_shot && shot.dialogue_in_shot.length) {
      dlgEl.innerHTML = `<div class="text-xs text-gray-500 mb-1">Dialogue</div>
        <div class="bg-gray-800/50 rounded p-2 space-y-1 text-sm font-mono">
          ${shot.dialogue_in_shot.map(d => {
            const parts = d.match(/^([A-Z\s]+)\s*[-—]\s*(.*)/);
            if (parts) {
              return `<div><span class="text-blue-400 font-bold">${App.escapeHtml(parts[1].trim())}</span> <span class="text-gray-300">${App.escapeHtml(parts[2])}</span></div>`;
            }
            return `<div class="text-gray-300">${App.escapeHtml(d)}</div>`;
          }).join('')}
        </div>`;
    } else {
      dlgEl.innerHTML = '';
    }

    // Audio playback — stop any prior shot's audio before rebuilding
    // so switching shots (keyboard nav, timeline clicks) doesn't layer
    // playback when a line is still playing. Also halt any in-flight
    // Preview Mix so the old shot's SFX don't trail into the new shot.
    EditingRoom.stopMix?.();
    const audioEl = document.getElementById('er-detail-audio');
    audioEl.querySelectorAll('audio').forEach(a => {
      try { a.pause(); a.removeAttribute('src'); a.load(); } catch (e) { /* noop */ }
    });
    if (shot.audio_lines && shot.audio_lines.length) {
      const dialogueHtml = shot.audio_lines.map((line, i) => {
        const src = `/api/${EditingRoom.slug}/audio/${line.audio_ref}`;
        const sliceAttrs = (typeof line.start_time_sec === 'number'
                            && typeof line.end_time_sec === 'number')
          ? `data-slice-start="${line.start_time_sec}" data-slice-end="${line.end_time_sec}"`
          : '';
        const sliceLabel = sliceAttrs
          ? `<span class="text-[10px] text-gray-600 ml-1">${line.start_time_sec.toFixed(2)}s–${line.end_time_sec.toFixed(2)}s</span>`
          : '';
        return `<div class="mb-2">
          <div class="flex items-center gap-2 text-xs mb-1">
            <span class="text-blue-400 shrink-0">${line.character_id}</span>
            <span class="text-gray-500 truncate">${App.escapeHtml(line.text).substring(0, 80)}</span>
            ${sliceLabel}
          </div>
          <audio controls preload="metadata" class="h-7 w-full er-slice-audio" ${sliceAttrs}>
            <source src="${src}" type="audio/mpeg">
          </audio>
        </div>`;
      }).join('');

      const sfxList = shot.sound_effects || [];
      const sfxHtml = sfxList.length ? `
        <div class="mt-3 pt-2 border-t border-gray-800">
          <div class="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Sound FX</div>
          ${sfxList.map(sfx => `
            <div class="mb-2">
              <div class="flex items-center gap-2 text-xs mb-1">
                <span class="text-amber-400 shrink-0">SFX</span>
                <span class="text-gray-500 truncate">${App.escapeHtml(sfx.prompt).substring(0, 80)}</span>
                <span class="text-[10px] text-gray-600 ml-1">${sfx.duration_sec}s</span>
              </div>
              <audio controls preload="metadata" class="h-7 w-full">
                <source src="/api/${EditingRoom.slug}/audio/${sfx.audio_ref}" type="audio/mpeg">
              </audio>
            </div>
          `).join('')}
        </div>` : '';

      audioEl.innerHTML = dialogueHtml + sfxHtml;
      EditingRoom._wireSliceAudio(audioEl);

      // Show the Preview Mix controls whenever this shot has dialogue
      // AND/OR any SFX — they're the only shots where layered playback
      // is meaningful.
      const mixCtrls = document.getElementById('er-detail-mix-controls');
      if (mixCtrls) {
        const hasAnything = (shot.audio_lines?.length || 0) > 0 || sfxList.length > 0;
        mixCtrls.style.display = hasAnything ? '' : 'none';
      }
    } else {
      audioEl.innerHTML = shot.has_dialogue
        ? '<div class="text-xs text-amber-500">Dialogue exists but audio not yet generated</div>'
        : '';
    }

    // Enable toggle
    document.getElementById('er-detail-enabled').checked = shot.edit.enabled;

    // Duration slider
    const origDur = shot.original_duration_sec;
    const curDur = shot.edit.duration_sec || origDur;
    const slider = document.getElementById('er-detail-duration');
    slider.max = Math.max(20, origDur * 2);
    slider.value = curDur;
    document.getElementById('er-detail-duration-val').textContent = curDur.toFixed(1) + 's';
    document.getElementById('er-detail-original-dur').textContent = `Original: ${origDur.toFixed(1)}s`;

    // Notes
    document.getElementById('er-detail-notes').value = shot.edit.notes || '';
  },

  // Wire per-shot audio slices: seek to start_time_sec on play/seek,
  // pause at end_time_sec so shots sharing one recording each play
  // only their portion.
  _wireSliceAudio(container) {
    container.querySelectorAll('audio.er-slice-audio').forEach(audio => {
      const startAttr = audio.getAttribute('data-slice-start');
      const endAttr = audio.getAttribute('data-slice-end');
      if (startAttr === null || endAttr === null) return;
      const start = parseFloat(startAttr);
      const end = parseFloat(endAttr);
      if (!isFinite(start) || !isFinite(end) || end <= start) return;

      const seekToStartIfBeforeSlice = () => {
        if (audio.currentTime < start || audio.currentTime > end) {
          try { audio.currentTime = start; } catch (e) { /* pre-metadata */ }
        }
      };

      audio.addEventListener('loadedmetadata', () => {
        try { audio.currentTime = start; } catch (e) { /* noop */ }
      });
      audio.addEventListener('play', seekToStartIfBeforeSlice);
      audio.addEventListener('timeupdate', () => {
        if (audio.currentTime >= end) {
          audio.pause();
          try { audio.currentTime = start; } catch (e) { /* noop */ }
        }
      });
    });
  },

  // ------------------------------------------------------------------
  // Edit actions
  // ------------------------------------------------------------------

  async toggleEnable() {
    const shot = EditingRoom.shots[EditingRoom.currentIdx];
    if (!shot) return;

    const checkbox = document.getElementById('er-detail-enabled');
    const newEnabled = checkbox.checked;

    // Warn if disabling dialogue shot
    if (!newEnabled && shot.has_dialogue) {
      if (!confirm('This shot contains dialogue. Disabling it will exclude the dialogue from the cut. Continue?')) {
        checkbox.checked = true;
        return;
      }
    }

    shot.edit.enabled = newEnabled;
    await EditingRoom._saveEdit(shot);
    EditingRoom._recomputeStats();
    EditingRoom._renderTimeline();
    EditingRoom.selectShot(EditingRoom.currentIdx);
  },

  async updateDuration() {
    const shot = EditingRoom.shots[EditingRoom.currentIdx];
    if (!shot) return;

    const val = parseFloat(document.getElementById('er-detail-duration').value);
    shot.edit.duration_sec = val;
    await EditingRoom._saveEdit(shot);
    EditingRoom._recomputeStats();
    EditingRoom._renderTimeline();
  },

  async resetDuration() {
    const shot = EditingRoom.shots[EditingRoom.currentIdx];
    if (!shot) return;

    shot.edit.duration_sec = null;
    const slider = document.getElementById('er-detail-duration');
    slider.value = shot.original_duration_sec;
    document.getElementById('er-detail-duration-val').textContent = shot.original_duration_sec.toFixed(1) + 's';
    await EditingRoom._saveEdit(shot);
    EditingRoom._recomputeStats();
    EditingRoom._renderTimeline();
  },

  async updateNotes() {
    const shot = EditingRoom.shots[EditingRoom.currentIdx];
    if (!shot) return;

    shot.edit.notes = document.getElementById('er-detail-notes').value;
    await EditingRoom._saveEdit(shot);
  },

  async _saveEdit(shot) {
    try {
      await App.api('POST', `/api/${EditingRoom.slug}/editing-room/shot/${shot.shot_id}/edit`, {
        chapter_id: EditingRoom.chapterId,
        scene_id: shot.scene_id,
        enabled: shot.edit.enabled,
        duration_sec: shot.edit.duration_sec,
        notes: shot.edit.notes || '',
      });
    } catch (e) {
      App.showToast('Failed to save edit', 'error');
    }
  },

  // ------------------------------------------------------------------
  // Batch actions
  // ------------------------------------------------------------------

  async batchAction(action) {
    if (!EditingRoom.chapterId) return;

    const labels = {
      enable_all: 'Enable all shots?',
      disable_non_dialogue: 'Disable all shots without dialogue?',
      reset: 'Reset all edit decisions?',
    };
    if (!confirm(labels[action] || 'Are you sure?')) return;

    try {
      await App.api('POST', `/api/${EditingRoom.slug}/editing-room/${EditingRoom.chapterId}/batch-edit`, {
        action: action,
      });
      App.showToast('Batch update applied', 'success');
      await EditingRoom.load();
    } catch (e) {
      App.showToast('Batch update failed', 'error');
    }
  },

  // ------------------------------------------------------------------
  // Lock Cut (gate approval)
  // ------------------------------------------------------------------

  async lockCut() {
    if (!confirm('Lock the cut and approve for sound effects and score generation? This approves the cut_to_sound gate.')) {
      return;
    }
    try {
      await App.approveGate(EditingRoom.slug, 'cut_to_sound');
      App.showToast('Cut locked! Sound FX and score stages are now available.', 'success');
    } catch (e) {
      App.showToast('Failed to lock cut', 'error');
    }
  },

  // ------------------------------------------------------------------
  // Cut playback
  // ------------------------------------------------------------------

  playCut() {
    if (EditingRoom.playing) {
      EditingRoom.stopCut();
      return;
    }

    // Start from the currently-selected shot so the cut picks up where
    // you're looking. Falls back to the first enabled shot when nothing
    // is selected yet.
    const startIdx = Math.max(0, EditingRoom.currentIdx);
    const enabledShots = EditingRoom.shots
      .map((s, i) => ({ ...s, _idx: i }))
      .filter(s => s._idx >= startIdx && s.edit.enabled);

    if (!enabledShots.length) {
      App.showToast('No enabled shots to play from here', 'warning');
      return;
    }

    EditingRoom.playing = true;
    const progressEl = document.getElementById('er-playback-progress');
    const fillEl = document.getElementById('er-playback-fill');
    const labelEl = document.getElementById('er-playback-label');
    progressEl.style.display = '';

    // Dedicated audio element for cut playback — separate from the
    // detail panel's preview player so selectShot's rebuild doesn't
    // interrupt the cut. Reused across shots so adjacent shots that
    // share a recording can play continuously without re-buffering.
    let cutAudio = document.getElementById('er-cut-audio');
    if (!cutAudio) {
      cutAudio = document.createElement('audio');
      cutAudio.id = 'er-cut-audio';
      cutAudio.preload = 'auto';
      cutAudio.style.display = 'none';
      document.body.appendChild(cutAudio);
    }
    EditingRoom._cutAudio = cutAudio;
    EditingRoom._cutSliceEnd = Infinity;
    EditingRoom._cutCurrentRef = null;

    // Single persistent clamp handler; we update _cutSliceEnd per shot
    // so the audio pauses at its slice end if the visual is longer.
    if (!EditingRoom._cutClampHandler) {
      EditingRoom._cutClampHandler = () => {
        if (cutAudio.currentTime >= EditingRoom._cutSliceEnd) {
          cutAudio.pause();
        }
      };
      cutAudio.addEventListener('timeupdate', EditingRoom._cutClampHandler);
    }

    let i = 0;
    const playNext = () => {
      if (i >= enabledShots.length || !EditingRoom.playing) {
        EditingRoom.stopCut();
        return;
      }

      const shot = enabledShots[i];
      EditingRoom.selectShot(shot._idx);
      const pct = Math.round(((i + 1) / enabledShots.length) * 100);
      fillEl.style.width = pct + '%';
      labelEl.textContent = `Playing ${i + 1}/${enabledShots.length}: ${shot.shot_id}`;

      const line = (shot.audio_lines || [])[0];
      const visualDurMs = (shot.edit.duration_sec || shot.original_duration_sec) * 1000;

      // Trigger SFX layered over the dialogue track. Each SFX starts at
      // the top of the shot and runs for its own duration; it's fine if
      // they bleed into the next shot a little.
      EditingRoom._startShotSfx(shot);

      if (line && line.audio_ref
          && typeof line.start_time_sec === 'number'
          && typeof line.end_time_sec === 'number') {
        const src = `/api/${EditingRoom.slug}/audio/${line.audio_ref}`;
        const start = line.start_time_sec;
        const end = line.end_time_sec;
        EditingRoom._cutSliceEnd = end;

        if (line.audio_ref !== EditingRoom._cutCurrentRef) {
          // New recording — load it, then seek + play on metadata.
          EditingRoom._cutCurrentRef = line.audio_ref;
          cutAudio.src = src;
          cutAudio.addEventListener('loadedmetadata', function onMeta() {
            cutAudio.removeEventListener('loadedmetadata', onMeta);
            try { cutAudio.currentTime = start; } catch (e) { /* noop */ }
            cutAudio.play().catch(() => {});
          }, { once: true });
        } else {
          // Same recording — if audio already sits at this slice's
          // start (bridge-tiled adjacent shots), just resume; else seek.
          if (Math.abs(cutAudio.currentTime - start) > 0.15) {
            try { cutAudio.currentTime = start; } catch (e) { /* noop */ }
          }
          if (cutAudio.paused) cutAudio.play().catch(() => {});
        }
      } else {
        // No audio for this shot — pause any ongoing playback.
        EditingRoom._cutSliceEnd = Infinity;
        if (!cutAudio.paused) cutAudio.pause();
      }

      i++;
      EditingRoom.playTimer = setTimeout(playNext, visualDurMs);
    };

    playNext();
  },

  // Play the currently-selected shot as a layered mix: dialogue slice
  // + every SFX starting at its offset_sec within the shot. Meant for
  // per-shot preview so you can hear exactly what the cut will sound
  // like when it reaches this shot, without having to Play Cut from
  // the top.
  previewMix() {
    EditingRoom.stopMix();
    const shot = EditingRoom.shots[EditingRoom.currentIdx];
    if (!shot) return;

    const mixAudios = [];
    const timers = [];

    // Dialogue: honor slice, anchor visual start at t=0
    const line = (shot.audio_lines || [])[0];
    let dialogueEndsMs = 0;
    if (line && line.audio_ref
        && typeof line.start_time_sec === 'number'
        && typeof line.end_time_sec === 'number') {
      const a = new Audio(`/api/${EditingRoom.slug}/audio/${line.audio_ref}`);
      a.addEventListener('loadedmetadata', () => {
        try { a.currentTime = line.start_time_sec; } catch (e) { /* noop */ }
        a.play().catch(() => {});
      }, { once: true });
      a.addEventListener('timeupdate', () => {
        if (a.currentTime >= line.end_time_sec) a.pause();
      });
      mixAudios.push(a);
      dialogueEndsMs = Math.max(
        0, (line.end_time_sec - line.start_time_sec) * 1000
      );
    }

    // SFX: fire each at offset_sec relative to the shot's visual start.
    const sfxList = shot.sound_effects || [];
    for (const sfx of sfxList) {
      if (!sfx.audio_ref) continue;
      const offsetMs = Math.max(0, (Number(sfx.offset_sec) || 0) * 1000);
      const vol = EditingRoom._sfxVolumeFor(sfx);
      const t = setTimeout(() => {
        if (!EditingRoom._mixActive) return;
        const a = new Audio(`/api/${EditingRoom.slug}/audio/${sfx.audio_ref}`);
        a.volume = vol;
        a.play().catch(() => {});
        mixAudios.push(a);
      }, offsetMs);
      timers.push(t);
    }

    // Auto-stop when the visual duration elapses so the mix doesn't
    // bleed past the shot.
    const visualMs = Math.max(
      dialogueEndsMs,
      ((shot.edit.duration_sec || shot.original_duration_sec) * 1000),
    );
    timers.push(setTimeout(() => {
      if (EditingRoom._mixActive) EditingRoom.stopMix();
    }, visualMs + 200));

    EditingRoom._mixActive = true;
    EditingRoom._mixAudios = mixAudios;
    EditingRoom._mixTimers = timers;
    const status = document.getElementById('er-mix-status');
    if (status) status.textContent = `Mixing ${mixAudios.length + (visualMs ? 0 : 0)} sources (dialogue + ${sfxList.length} SFX)`;
  },

  stopMix() {
    EditingRoom._mixActive = false;
    for (const t of (EditingRoom._mixTimers || [])) clearTimeout(t);
    EditingRoom._mixTimers = [];
    for (const a of (EditingRoom._mixAudios || [])) {
      try { a.pause(); a.src = ''; } catch (e) { /* noop */ }
    }
    EditingRoom._mixAudios = [];
    const status = document.getElementById('er-mix-status');
    if (status) status.textContent = '';
  },

  // Decide per-SFX playback volume. Respects an explicit numeric
  // ``gain`` field on the entry when present; otherwise maps prompt
  // keywords to a tier so ambient beds don't drown dialogue.
  _sfxVolumeFor(sfx) {
    const g = Number(sfx && sfx.gain);
    if (Number.isFinite(g)) return Math.max(0, Math.min(1, g));
    const p = ((sfx && sfx.prompt) || '').toLowerCase();
    const ambient = [
      'ambient', 'ambience', 'background', 'room tone', 'distant',
      'far ', 'far-off', 'crowd', 'hubbub', 'wind', 'breeze',
      'bustle', 'murmur', 'chatter', 'rustle', 'rustling',
      'drone', 'atmosphere',
    ];
    for (const w of ambient) if (p.includes(w)) return 0.22;
    const sharp = [
      'slam', 'clang', 'crash', 'scream', 'shout', 'bang',
      'gunshot', 'impact',
    ];
    for (const w of sharp) if (p.includes(w)) return 0.45;
    return 0.55;
  },

  // Spin up a transient <audio> element per SFX, reusing any already
  // playing when the next shot references the same ``audio_ref`` so a
  // continuous bed (street murmur, wind) doesn't restart at every cut.
  _startShotSfx(shot) {
    const sfxList = shot.sound_effects || [];
    if (!EditingRoom._sfxAudios) EditingRoom._sfxAudios = [];
    if (!EditingRoom._sfxActive) EditingRoom._sfxActive = new Map();

    const wanted = new Set();
    for (const sfx of sfxList) {
      if (!sfx.audio_ref) continue;
      wanted.add(sfx.audio_ref);
      const vol = EditingRoom._sfxVolumeFor(sfx);
      const existing = EditingRoom._sfxActive.get(sfx.audio_ref);
      if (existing) {
        existing.volume = vol;  // update in case the new shot wants it louder/softer
        continue;
      }
      const a = new Audio(`/api/${EditingRoom.slug}/audio/${sfx.audio_ref}`);
      a.preload = 'auto';
      a.volume = vol;
      const offsetMs = Math.max(0, (Number(sfx.offset_sec) || 0) * 1000);
      const start = () => {
        if (!EditingRoom.playing) return;
        a.play().catch(() => {});
      };
      if (offsetMs) {
        setTimeout(start, offsetMs);
      } else {
        start();
      }
      a.addEventListener('ended', () => {
        EditingRoom._sfxActive.delete(sfx.audio_ref);
      });
      EditingRoom._sfxActive.set(sfx.audio_ref, a);
      EditingRoom._sfxAudios.push(a);
    }
    // Fade out SFX the new shot doesn't want anymore.
    for (const [ref, a] of Array.from(EditingRoom._sfxActive.entries())) {
      if (!wanted.has(ref)) {
        try { a.pause(); a.src = ''; } catch (e) { /* noop */ }
        EditingRoom._sfxActive.delete(ref);
      }
    }
  },

  _stopAllSfx() {
    if (!EditingRoom._sfxAudios) return;
    for (const a of EditingRoom._sfxAudios) {
      try { a.pause(); a.src = ''; } catch (e) { /* noop */ }
    }
    EditingRoom._sfxAudios = [];
    if (EditingRoom._sfxActive) EditingRoom._sfxActive.clear();
  },

  // Re-mix the current shot's SFX into its preview.mp4 (dialogue-only
  // Wan output → dialogue + SFX mix). Fast — no video re-render.
  async mixPreviewAudio() {
    const shot = EditingRoom.shots[EditingRoom.currentIdx];
    if (!shot) return;
    const sfxCount = (shot.sound_effects || []).length;
    const hasH = !!shot.preview_video_url;
    const hasV = !!shot.preview_video_url_vertical;
    if (!hasH && !hasV) {
      App.showToast('No preview video to mix into', 'warning');
      return;
    }
    if (!sfxCount) {
      App.showToast('No SFX on this shot — nothing to mix', 'warning');
      return;
    }
    const orientation = hasH && hasV ? 'both' : (hasH ? 'horizontal' : 'vertical');
    try {
      const r = await App.api('POST',
        `/api/${EditingRoom.slug}/shot/${shot.shot_id}/mix-preview`,
        { orientation });
      const mixed = r.results.filter(x => x.status === 'mixed').length;
      if (mixed) {
        App.showToast(`Mixed ${sfxCount} SFX into ${mixed} preview(s)`, 'success');
        await EditingRoom.load();
      } else {
        App.showToast('Mix produced no output — check console', 'error');
        console.warn('Mix results:', r.results);
      }
    } catch (e) {
      App.showToast(`Mix failed: ${e.message || e}`, 'error');
    }
  },

  // Render a Wan 2.1 + InfiniTalk talking-head mp4 for the currently-
  // selected shot. Free and local (runs on the host GPU), but slow —
  // a 12s slice takes ~10 minutes on a 4090. The SSE panel shows
  // progress; the detail panel auto-swaps the still for the mp4 when
  // the stage completes.
  async generatePreviewVideo(orientation) {
    orientation = orientation || 'horizontal';
    const shot = EditingRoom.shots[EditingRoom.currentIdx];
    if (!shot) {
      App.showToast('Select a shot first', 'warning');
      return;
    }
    // Silent shots (no audio slices) fall through to Wan I2V — timing
    // comes from shot.duration_sec. Talking shots use the dialogue
    // slice duration.
    const totalSlice = (shot.audio_lines || []).reduce((s, l) => {
      if (typeof l.start_time_sec === 'number' && typeof l.end_time_sec === 'number') {
        return s + (l.end_time_sec - l.start_time_sec);
      }
      return s;
    }, 0);
    const silent = totalSlice === 0;
    const dur = silent
      ? Math.max(1.0, shot.edit?.duration_sec || shot.original_duration_sec || 3)
      : totalSlice;
    const mode = silent ? 'silent (Wan I2V)' : 'talking (InfiniTalk)';
    const est = Math.round(dur * 12);  // ~12s wall per 1s output on 4090
    const aspect = orientation === 'vertical' ? '9:16' : '16:9';
    if (!confirm(
      `Render ${mode} preview for ${shot.shot_id} (${aspect})?\n\n` +
      `${silent ? 'Shot duration' : 'Dialogue'}: ${dur.toFixed(1)}s  →  ~${est}s GPU time (free, local).\n\n` +
      `Any existing ${aspect} preview will be overwritten. You can keep working in ` +
      `the Editing Room while it renders — progress streams into the ` +
      `Active Jobs panel.`
    )) return;

    try {
      await StageRunner.run('preview_video', {
        shot_id: shot.shot_id,
        orientation,
      });
      App.showToast(`${aspect} preview generated`, 'success');
      await EditingRoom.load();  // pulls fresh preview_video_url / _vertical
    } catch (e) {
      App.showToast(`Preview video failed: ${e.message || e}`, 'error');
    }
  },

  // Batch-render Wan 2.1 preview videos across every shot in the
  // currently-loaded chapter. PreviewVideoStage auto-skips shots that
  // already have a preview for the chosen orientation unless force is
  // set.
  async generateAllPreviews() {
    if (!EditingRoom.chapterId) {
      App.showToast('Select a chapter first', 'warning');
      return;
    }
    const shots = EditingRoom.shots || [];
    const orient = prompt(
      'Which orientation(s) to render?\n' +
      '  h   — 16:9 horizontal\n' +
      '  v   — 9:16 vertical\n' +
      '  b   — both\n\n' +
      '(Type one letter)', 'h',
    );
    if (!orient) return;
    const key = orient.trim().toLowerCase();
    const orientation = ({h:'horizontal', v:'vertical', b:'both'})[key];
    if (!orientation) {
      App.showToast(`Unknown orientation "${orient}"`, 'error');
      return;
    }
    const forceRegen = confirm(
      `Generate ${orientation} previews for ${shots.length} shots in ${EditingRoom.chapterId}.\n\n` +
      `Shots that already have the chosen preview will be SKIPPED ` +
      `by default — click OK to skip, or Cancel to force regenerate ` +
      `all of them (overwrites existing mp4s).\n\n` +
      `OK = skip existing   /   Cancel = force regenerate all`,
    );
    // Cancel on this confirm means user wants force-regen (opposite of
    // the usual Cancel = abort semantic). Give them one more chance.
    const force = !forceRegen;
    if (force && !confirm(
      `Force-regenerate EVERY ${orientation} preview in the chapter? ` +
      `This overwrites existing mp4s and takes ~10s–30min per shot.`,
    )) return;

    // Rough wall-time estimate — 10s of GPU per 1s of output
    let estDialogueS = 0, estSilentS = 0;
    for (const s of shots) {
      const sl = (s.audio_lines || []).reduce((a, l) => {
        if (typeof l.start_time_sec === 'number' && typeof l.end_time_sec === 'number') {
          return a + (l.end_time_sec - l.start_time_sec);
        }
        return a;
      }, 0);
      if (sl > 0) estDialogueS += sl;
      else estSilentS += Math.max(1, s.edit?.duration_sec || s.original_duration_sec || 3);
    }
    const multiplier = orientation === 'both' ? 2 : 1;
    const mins = Math.round(((estDialogueS * 12 + estSilentS * 8) * multiplier) / 60);
    if (!confirm(
      `Estimated ~${mins} minutes of GPU time (${shots.length} shots, ${estDialogueS.toFixed(0)}s dialogue + ${estSilentS.toFixed(0)}s silent).\n\n` +
      `Progress streams into Active Jobs. You can keep working meanwhile.`,
    )) return;

    try {
      await StageRunner.run('preview_video', {
        chapter_id: EditingRoom.chapterId,
        orientation,
        force,
      });
      App.showToast('Batch preview complete — reloading', 'success');
      await EditingRoom.load();
    } catch (e) {
      App.showToast(`Batch preview failed: ${e.message || e}`, 'error');
    }
  },

  // Trigger the SoundFXStage for the currently-loaded chapter. The
  // stage's internal router decides provider per prompt (ComfyUI for
  // foley, ElevenLabs for tonal SFX) so the user doesn't pick here.
  async generateSfx() {
    if (!EditingRoom.chapterId) {
      App.showToast('Select a chapter first', 'warning');
      return;
    }
    if (!confirm(
      `Generate sound effects for chapter ${EditingRoom.chapterId}?\n\n` +
      `Each shot gets 1–3 SFX. Prompts that mention lyres/bells/chants/` +
      `voices route to ElevenLabs ($0.10 each); everything else runs ` +
      `free on ComfyUI. Shots that already have SFX are skipped.\n\n` +
      `You can monitor progress in the Active Jobs panel on the ` +
      `Dashboard.`
    )) return;

    try {
      await StageRunner.run('sound_fx', { chapter_id: EditingRoom.chapterId });
      App.showToast('Sound FX generation complete — reloading shots', 'success');
      await EditingRoom.load();
    } catch (e) {
      App.showToast(`Sound FX failed: ${e.message || e}`, 'error');
    }
  },

  stopCut() {
    EditingRoom.playing = false;
    if (EditingRoom.playTimer) {
      clearTimeout(EditingRoom.playTimer);
      EditingRoom.playTimer = null;
    }
    if (EditingRoom._cutAudio) {
      try { EditingRoom._cutAudio.pause(); } catch (e) { /* noop */ }
      EditingRoom._cutCurrentRef = null;
      EditingRoom._cutSliceEnd = Infinity;
    }
    EditingRoom._stopAllSfx();
    const progressEl = document.getElementById('er-playback-progress');
    if (progressEl) progressEl.style.display = 'none';
  },

  // ------------------------------------------------------------------
  // Recompute stats client-side (avoids reload)
  // ------------------------------------------------------------------

  _recomputeStats() {
    const shots = EditingRoom.shots;
    const total = shots.length;
    const enabled = shots.filter(s => s.edit.enabled).length;
    const origDur = shots.reduce((sum, s) => sum + s.original_duration_sec, 0);
    const cutDur = shots
      .filter(s => s.edit.enabled)
      .reduce((sum, s) => sum + (s.edit.duration_sec || s.original_duration_sec), 0);
    const dlgDisabled = shots.filter(s => !s.edit.enabled && s.has_dialogue).length;

    EditingRoom.summary = {
      total_shots: total,
      enabled_shots: enabled,
      disabled_shots: total - enabled,
      original_duration_sec: origDur,
      cut_duration_sec: cutDur,
      dialogue_shots_disabled: dlgDisabled,
    };
    EditingRoom._renderStats();
  },

  // ------------------------------------------------------------------
  // Keyboard
  // ------------------------------------------------------------------

  _onKey(e) {
    // Skip if typing in input
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

    switch (e.key) {
      case 'ArrowLeft':
        e.preventDefault();
        EditingRoom.selectShot(EditingRoom.currentIdx - 1);
        break;
      case 'ArrowRight':
        e.preventDefault();
        EditingRoom.selectShot(EditingRoom.currentIdx + 1);
        break;
      case 'd':
      case 'D':
        e.preventDefault();
        if (EditingRoom.currentIdx >= 0) {
          const cb = document.getElementById('er-detail-enabled');
          cb.checked = !cb.checked;
          EditingRoom.toggleEnable();
        }
        break;
      case 'p':
      case 'P':
        e.preventDefault();
        // Play first audio element in detail panel
        const audio = document.querySelector('#er-detail-audio audio');
        if (audio) {
          if (audio.paused) audio.play();
          else audio.pause();
        }
        break;
      case ' ':
        e.preventDefault();
        EditingRoom.playCut();
        break;
      case 'Escape':
        EditingRoom.stopCut();
        break;
    }
  },

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  _formatDuration(sec) {
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  },
};
