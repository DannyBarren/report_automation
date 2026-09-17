/**
 * field_recorder.js — User-driven guided field recording (template JSON = source of truth).
 *
 * Step progression is controlled only by the Next Step button (+ backend session).
 * “Mark this.” auto-advance is disabled during recording (post-processing only).
 */
(function (global) {
  'use strict';

  var C = global.JOBDOC_REC || {};
  var identificationPhrase = C.identificationPhrase || 'Mark this.';
  var uploadUrl = C.uploadUrl || '/upload_video';

  var DRAFT_DB = 'jobdoc_field_drafts';
  var DRAFT_STORE = 'recordings';
  var DRAFT_KEY = 'latest';
  var INPROGRESS_KEY = 'inprogress';

  var maxVideoSeconds = C.maxVideoSeconds || 180;
  var SNAPSHOT_EVERY_MS = 5000; // periodic crash-recovery snapshot during recording

  /**
   * Diagnostic logger — clear, greppable console output around every point a mobile
   * recording can silently die (permission, tracks, mime, chunks, blob, upload). Prefixed
   * so field logs from a real phone (Safari/Chrome remote inspector) are easy to filter.
   */
  function logDiag() {
    try {
      var args = Array.prototype.slice.call(arguments);
      args.unshift('[jobdoc-rec]');
      (console.info || console.log).apply(console, args);
    } catch (_e) { /* logging must never throw */ }
  }

  /** Detect the mobile browser so we can show tailored, actionable permission tips. */
  function detectPlatform() {
    var ua = navigator.userAgent || '';
    var ios = /iPad|iPhone|iPod/.test(ua) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1); // iPadOS 13+
    var android = /Android/.test(ua);
    var isCriOS = /CriOS/.test(ua);      // Chrome on iOS (still WebKit under the hood)
    var isFxiOS = /FxiOS/.test(ua);      // Firefox on iOS
    var safari = ios && !isCriOS && !isFxiOS;
    var chrome = /Chrome|CriOS/.test(ua);
    return { ios: ios, android: android, safari: safari, chrome: chrome, isCriOS: isCriOS };
  }
  var PLATFORM = detectPlatform();

  /** Plain-language, device-specific instructions for a blocked camera/mic. */
  function permissionTips() {
    if (PLATFORM.ios) {
      return 'On iPhone/iPad: tap the "aA" or page menu in the address bar → Website Settings → ' +
        'allow Camera and Microphone. Or Settings → Safari → Camera/Microphone → Allow. Then reload.';
    }
    if (PLATFORM.android) {
      return 'On Android Chrome: tap the lock icon in the address bar → Permissions → allow ' +
        'Camera and Microphone, then reload. If you tapped "Block", clear it there.';
    }
    return 'Allow Camera and Microphone for this site in your browser settings, then reload.';
  }

  /** Turn a getUserMedia error into a specific, actionable message. */
  function describeMediaError(err) {
    var name = (err && err.name) || '';
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
      return 'Camera/microphone access was blocked. ' + permissionTips();
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      return 'No camera or microphone was found on this device. Try the other camera, or use ' +
        '"Upload a video instead" below.';
    }
    if (name === 'NotReadableError' || name === 'TrackStartError') {
      return 'The camera is in use by another app. Close other camera apps (FaceTime, Zoom, ' +
        'Camera) and tap "Enable Camera & Microphone" again.';
    }
    if (name === 'OverconstrainedError') {
      return 'This camera cannot match the requested quality. Switch to Balanced quality or flip ' +
        'the camera, then retry.';
    }
    if (name === 'SecurityError') {
      return 'Camera/mic require a secure (HTTPS) connection. Open the Space over https:// and reload.';
    }
    return (err && (err.message || String(err))) || 'Could not start the camera.';
  }

  var state = {
    sessionId: null,
    currentStep: null,
    sessionMeta: null,
    marksDetected: 0,
    paused: false,
    recording: false,
    highVis: false,
    chunks: [],
    stream: null,
    mediaRecorder: null,
    selectedMime: '',
    startedAt: 0,
    pauseStartedAt: 0,
    totalPausedMs: 0,
    pendingBlob: null,
    draftDb: null,
    advancing: false,
    marks: [],
    // Continuous-recording step boundaries: one entry each time a guided step becomes
    // active while recording. Sent alongside the single video so the backend knows exactly
    // when the inspector moved Overview → Components → Substrates → Misc.
    stepEvents: [],
    lastStepEventKey: '',
    voiceMuted: false,
    facingMode: 'environment',
    quality: 'balanced',
    pendingMarks: [],
    audioCtx: null,
    analyser: null,
    meterRaf: null,
    snapshotHandle: null,
    permissionGranted: false,
    wakeLock: null,
    warnedNoWakeLock: false,
    firstChunkSnapshotDone: false,
  };

  var els = {};

  function $(id) { return document.getElementById(id); }

  function reportTypesFromDom() {
    if (C.reportTypes && C.reportTypes.length) return C.reportTypes;
    var out = [];
    document.querySelectorAll('[data-report-type]').forEach(function (inp) {
      out.push(inp.getAttribute('data-report-type'));
    });
    return out;
  }

  function init() {
    els = {
      root: $('field-rec-root'),
      dock: $('guided-dock'),
      dockProgress: $('dock-progress'),
      dockTitle: $('dock-title'),
      dockScreen: $('dock-screen'),
      dockVoice: $('dock-voice'),
      progressFill: $('step-progress-fill'),
      markReminder: $('overlay-mark-cue'),
      videoWrap: $('video-stage'),
      preview: $('rec-preview-video'),
      overlayScreen: $('overlay-screen-text'),
      lamp: $('rec-lamp'),
      lampLabel: $('rec-lamp-label'),
      timer: $('rec-timer'),
      liveBadge: $('rec-live-badge'),
      liveLabel: $('rec-live-label'),
      liveTime: $('rec-live-time'),
      msg: $('rec-msg'),
      walkHint: $('walk-hint'),
      uploadBar: $('upload-progress'),
      uploadFill: $('upload-progress-fill'),
      btnCam: $('btn-request-permission'),
      btnStart: $('btn-start'),
      btnPause: $('btn-pause'),
      btnStop: $('btn-stop-upload'),
      btnSpeak: $('btn-speak-step'),
      btnMuteVoice: $('btn-mute-voice'),
      btnVoiceToggle: $('btn-voice-toggle'),
      btnNext: $('btn-next-step'),
      btnRetryUpload: $('btn-retry-upload'),
      btnDownload: $('btn-download-recording'),
      btnHighVis: $('btn-high-vis'),
      checklist: $('section-checklist'),
      draftRestore: $('draft-restore'),
      btnRestoreDraft: $('btn-restore-draft'),
      btnDiscardDraft: $('btn-discard-draft'),
      btnMark: $('btn-mark'),
      markHint: $('mark-hint'),
      marksTimeline: $('marks-timeline'),
      marksTrack: $('marks-track'),
      btnUndoMark: $('btn-undo-mark'),
      btnFlipCam: $('btn-flip-cam'),
      qBalanced: $('q-balanced'),
      qHigh: $('q-high'),
      permGate: $('perm-gate'),
      btnEnable: $('btn-enable-av'),
      permTips: $('perm-tips'),
      permError: $('perm-error'),
      audioMeterFill: $('audio-meter-fill'),
      micChip: $('rec-mic'),
      micLabel: $('rec-mic-label'),
      countdownBadge: $('rec-countdown'),
      fallbackInput: $('fallback-file'),
      recoverBar: $('recover-bar'),
      btnRecover: $('btn-recover'),
      btnDiscardRecover: $('btn-discard-recover'),
      // Immersive overlay extras
      guidance: $('rec-guidance'),
      btnGuidanceCollapse: $('btn-guidance-collapse'),
      stepDots: $('rec-step-dots'),
      tapMarkHint: $('tap-mark-hint'),
      recToast: $('rec-toast'),
    };

    state.highVis = localStorage.getItem('jobdoc_high_vis') === '1';
    applyHighVis(state.highVis);
    if (els.btnHighVis) {
      els.btnHighVis.setAttribute('aria-pressed', state.highVis ? 'true' : 'false');
      els.btnHighVis.addEventListener('click', toggleHighVis);
    }

    // Voice-prompt preference. The pre-session toggle (chosen on the start page and passed in
    // as C.voicePrompts) is the PRIMARY control and sets the initial state for this session.
    // The in-recording Mute button can still override it while capturing. When the pre-session
    // value is unavailable (e.g. resumed/legacy flow) we fall back to the last per-device choice.
    if (typeof C.voicePrompts === 'boolean') {
      state.voiceMuted = !C.voicePrompts;
    } else {
      state.voiceMuted = localStorage.getItem('jobdoc_voice_muted') === '1';
    }
    localStorage.setItem('jobdoc_voice_muted', state.voiceMuted ? '1' : '0');
    applyVoiceMuteUI();

    if (els.permTips) els.permTips.textContent = permissionTips();

    bindControls();
    bindLifecycleGuards();
    openDraftDb(function () {
      checkForLocalDraft();
      checkForInProgress();
    });
    startGuidanceSession();

    if (!hasRecordingSupport()) {
      setLamp('error');
      showPermError(
        PLATFORM.isCriOS
          ? 'Chrome on iPhone cannot record video. Open this page in Safari, or use "Upload a video instead" below.'
          : 'Recording is not supported in this browser. Use Safari (iPhone) or Chrome (Android) on HTTPS, or upload a video below.'
      );
      revealFallbackUpload();
    } else if (!global.isSecureContext) {
      setLamp('error');
      showPermError('HTTPS is required for camera and microphone. Open the Space over https:// and reload.');
    }
  }

  function showPermError(text) {
    if (els.permError) {
      els.permError.textContent = text || '';
      els.permError.hidden = !text;
    }
    setMessage(text || '');
  }

  function revealFallbackUpload() {
    var wrap = $('fallback-upload');
    if (wrap) wrap.hidden = false;
  }

  function hidePermGate() {
    if (els.permGate) els.permGate.hidden = true;
  }

  function hasRecordingSupport() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && global.MediaRecorder);
  }

  function toggleHighVis() {
    state.highVis = !state.highVis;
    localStorage.setItem('jobdoc_high_vis', state.highVis ? '1' : '0');
    applyHighVis(state.highVis);
    if (els.btnHighVis) els.btnHighVis.setAttribute('aria-pressed', state.highVis ? 'true' : 'false');
  }

  function applyHighVis(on) {
    if (els.root) els.root.classList.toggle('high-vis', on);
  }

  function startGuidanceSession() {
    var types = reportTypesFromDom();
    if (!types.length) {
      fallbackClientPlan();
      return;
    }
    if (!C.sessionStartUrl) {
      fallbackClientPlan();
      return;
    }
    setMessage('Loading inspection steps from template…');
    fetch(C.sessionStartUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ report_types: types }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) {
          setMessage(data.error || 'Could not load guidance.');
          fallbackClientPlan();
          return;
        }
        state.sessionId = data.session_id;
        state.sessionMeta = data;
        applySessionPayload(data, false);
        setMessage('');
        if (data.intro_script) {
          setTimeout(function () { speakText(data.intro_script); }, 500);
        }
      })
      .catch(function (err) {
        setMessage('Network error loading steps — using offline template copy.');
        fallbackClientPlan();
      });
  }

  function fallbackClientPlan() {
    if (!C.guidancePlan || !C.guidancePlan.steps || !C.guidancePlan.steps.length) {
      setMessage('No guidance template loaded.');
      return;
    }
    var s = C.guidancePlan.steps[0];
    state.currentStep = {
      step_index: s.step_index,
      total_steps: s.total_steps,
      section_id: s.section_id,
      report_type: s.report_type,
      title: s.title,
      voice_prompt: s.voice_prompt,
      on_screen_text: s.on_screen_text,
      spoken_intro: s.spoken_intro,
      progress_label: 'Step ' + s.step_index + ' of ' + s.total_steps + ' — ' + s.title,
      is_last: s.step_index >= s.total_steps,
      can_advance: s.step_index < s.total_steps,
    };
    state.sessionMeta = { identification_phrase: C.guidancePlan.identification_phrase };
    renderCurrentStep(false);
  }

  function applySessionPayload(data, speak) {
    state.sessionMeta = data;
    if (data.identification_phrase) {
      identificationPhrase = data.identification_phrase;
    }
    state.currentStep = data.current_step;
    // Continuous recording: advancing/jumping steps NEVER stops the single MediaRecorder —
    // we only log a step-boundary marker so the report pipeline can align narration to steps.
    recordStepEvent(state.currentStep);
    renderCurrentStep(speak);
  }

  function renderCurrentStep(speak) {
    var s = state.currentStep;
    if (!s || !els.dock) return;

    var label = s.progress_label || ('Step ' + s.step_index + ' of ' + s.total_steps + ' — ' + s.title);
    if (els.dockProgress) els.dockProgress.textContent = label;
    if (els.dockTitle) els.dockTitle.textContent = s.title;
    if (els.dockScreen) els.dockScreen.textContent = s.on_screen_text || s.title;
    if (els.dockVoice) els.dockVoice.textContent = s.voice_prompt || '';
    if (els.overlayScreen) els.overlayScreen.textContent = s.on_screen_text || s.title;

    if (els.progressFill && s.total_steps) {
      var pct = Math.round((s.step_index / s.total_steps) * 100);
      els.progressFill.style.width = Math.max(8, pct) + '%';
    }

    buildStepDots(s.step_index, s.total_steps);

    if (els.btnNext) {
      els.btnNext.disabled = !!s.is_last || state.advancing;
      els.btnNext.textContent = s.is_last ? 'Final step — finish & upload' : 'Next step →';
    }

    if (els.walkHint) {
      var hint = s.is_last
        ? 'You are on the last section. Complete your narration, then stop and upload.'
        : 'Say “' + identificationPhrase + '”, narrate this area, then tap Next step when ready.';
      // Surface the template's per-section capture target so a first-day worker knows how many
      // marked shots this section expects (e.g. Overview wants many wide shots).
      var minMarks = Number(s.min_marks || 0);
      if (!s.is_last && minMarks > 1) {
        hint += ' Aim for at least ' + minMarks + ' marked shot' + (minMarks === 1 ? '' : 's') + ' here.';
      }
      els.walkHint.textContent = hint;
    }

    updateChecklistHighlight(s.step_index);
    if (speak) speakText(s.spoken_intro || s.voice_prompt);
  }

  /** Compact step dots in the top bar — a glanceable progress indicator over the camera. */
  function buildStepDots(activeIndex, total) {
    if (!els.stepDots || !total) return;
    if (els.stepDots.childElementCount !== total) {
      els.stepDots.innerHTML = '';
      for (var i = 1; i <= total; i++) {
        var d = document.createElement('span');
        d.className = 'rec-step-dot';
        els.stepDots.appendChild(d);
      }
    }
    var dots = els.stepDots.children;
    for (var j = 0; j < dots.length; j++) {
      var idx = j + 1;
      dots[j].classList.toggle('done', idx < activeIndex);
      dots[j].classList.toggle('current', idx === activeIndex);
    }
  }

  /** Toggle the full-screen recording chrome (record vs stop, mark enabled, lamp). */
  function setRecordingUI(on) {
    if (els.root) els.root.classList.toggle('is-recording', on);
    // Safety net: whenever we leave the recording UI, ensure the prominent badge and the
    // paused glow are cleared even if a status path didn't call setLamp (e.g. edge stops).
    if (!on) updateLiveBadge('ready');
    if (els.btnStart) els.btnStart.hidden = on;
    if (els.btnStop) els.btnStop.hidden = !on;
    if (on && els.tapMarkHint) {
      els.tapMarkHint.classList.add('show');
      setTimeout(function () { els.tapMarkHint.classList.remove('show'); }, 2600);
    }
  }

  function updateChecklistHighlight(activeStepIndex) {
    if (!els.checklist) return;
    els.checklist.querySelectorAll('[data-step-index]').forEach(function (card) {
      var idx = parseInt(card.getAttribute('data-step-index'), 10);
      card.classList.toggle('current', idx === activeStepIndex);
      card.classList.toggle('done', idx < activeStepIndex);
    });
  }

  function flashMarkCue() {
    if (!els.markReminder) return;
    els.markReminder.classList.remove('flash');
    void els.markReminder.offsetWidth;
    els.markReminder.classList.add('flash');
    if (navigator.vibrate) navigator.vibrate([60, 30, 60]);
  }

  function speakText(text) {
    if (!text) return;
    // Muted: skip spoken prompts entirely — the on-screen text still guides the user.
    if (state.voiceMuted) {
      if (window.speechSynthesis) window.speechSynthesis.cancel();
      return;
    }
    if (!window.speechSynthesis) {
      setMessage('Voice unavailable — read the on-screen text.');
      return;
    }
    window.speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(text);
    u.rate = state.highVis ? 0.88 : 0.95;
    u.volume = 1;
    window.speechSynthesis.speak(u);
  }

  function applyVoiceMuteUI() {
    var muted = state.voiceMuted;
    // Primary control: the always-visible top-bar Voice toggle.
    if (els.btnVoiceToggle) {
      els.btnVoiceToggle.textContent = muted ? '🔇' : '🔊';
      els.btnVoiceToggle.setAttribute('aria-pressed', muted ? 'true' : 'false');
      els.btnVoiceToggle.setAttribute(
        'title', muted ? 'Voice prompts OFF — tap to turn on' : 'Voice prompts ON — tap to mute'
      );
      els.btnVoiceToggle.classList.toggle('is-muted', muted);
    }
    // Legacy in-card toggle (still supported if present in a template variant).
    if (els.btnMuteVoice) {
      els.btnMuteVoice.textContent = muted ? '🔇 Voice off' : '🔊 Voice on';
      els.btnMuteVoice.setAttribute('aria-pressed', muted ? 'true' : 'false');
      els.btnMuteVoice.classList.toggle('is-muted', muted);
    }
    // Quiet whole-screen cue so the muted state is discoverable beyond the button icon.
    if (els.root) els.root.classList.toggle('voice-muted', muted);
    // Keep the "Speak prompt" button honest — disabled while muted.
    if (els.btnSpeak) els.btnSpeak.disabled = muted;
  }

  function toggleVoiceMute() {
    state.voiceMuted = !state.voiceMuted;
    localStorage.setItem('jobdoc_voice_muted', state.voiceMuted ? '1' : '0');
    if (state.voiceMuted && window.speechSynthesis) window.speechSynthesis.cancel();
    applyVoiceMuteUI();
    setMessage(state.voiceMuted ? 'Voice prompts muted — on-screen text still shows.' : 'Voice prompts on.');
  }

  /** Record a step-boundary marker (continuous recording) with the active step's context. */
  function recordStepEvent(step) {
    if (!state.recording) return;
    var s = step || state.currentStep;
    if (!s) return;
    var key = String(s.step_index || '') + '|' + String(s.section_id || '');
    if (key === state.lastStepEventKey) return; // avoid duplicate entries for the same step
    state.lastStepEventKey = key;
    state.stepEvents.push({
      t_sec: Math.round(recordingElapsedSec() * 100) / 100,
      step_index: s.step_index || null,
      section_id: s.section_id || '',
      report_type: s.report_type || '',
      title: s.title || '',
      total_steps: s.total_steps || null,
    });
  }

  function advanceViaServer() {
    if (!state.sessionId || !C.nextStepUrl) {
      setMessage('Session not ready.');
      return;
    }
    if (state.currentStep && state.currentStep.is_last) {
      setMessage('Final section — finish narrating, then upload.');
      return;
    }
    state.advancing = true;
    if (els.btnNext) els.btnNext.disabled = true;
    setMessage('Loading next section…');

    fetch(C.nextStepUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.advancing = false;
        if (!data.ok) {
          setMessage(data.error || 'Could not advance.');
          if (els.btnNext) els.btnNext.disabled = false;
          return;
        }
        applySessionPayload(data, true);
        setMessage('Now at: ' + (data.current_step && data.current_step.title));
      })
      .catch(function () {
        state.advancing = false;
        setMessage('Network error — try Next step again.');
        if (els.btnNext) els.btnNext.disabled = false;
      });
  }

  function gotoStep(stepIndex) {
    if (!state.sessionId || !C.gotoStepUrl) return;
    fetch(C.gotoStepUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, step_index: stepIndex }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) applySessionPayload(data, true);
      });
  }

  var toastHandle = null;
  function setMessage(t) {
    if (els.msg) els.msg.textContent = t || '';
    // Show as an auto-hiding glass toast so the camera view stays unobstructed.
    if (els.recToast) {
      if (t) {
        els.recToast.classList.add('show');
        if (toastHandle) clearTimeout(toastHandle);
        toastHandle = setTimeout(function () { els.recToast.classList.remove('show'); }, 3600);
      } else {
        els.recToast.classList.remove('show');
      }
    }
  }

  function setLamp(mode) {
    // Drive the small status chip (kept for Ready/Starting/Uploading/Error states)…
    if (els.lamp) {
      els.lamp.classList.remove('recording', 'paused');
      if (mode === 'recording') { els.lamp.classList.add('recording'); els.lampLabel.textContent = 'Recording'; }
      else if (mode === 'paused') { els.lamp.classList.add('paused'); els.lampLabel.textContent = 'Paused'; }
      else if (mode === 'uploading') els.lampLabel.textContent = 'Uploading…';
      else if (mode === 'requesting') els.lampLabel.textContent = 'Starting…';
      else if (mode === 'error') els.lampLabel.textContent = 'Check device';
      else els.lampLabel.textContent = 'Ready';
    }
    // …and the prominent, persistent recording badge. It shows for the whole active
    // session (recording or paused) and hides for every non-recording state. Because
    // setLamp is the single choke point for status changes — and stepping between areas
    // never calls it — the badge stays visible across step changes automatically.
    updateLiveBadge(mode);
  }

  function updateLiveBadge(mode) {
    var recording = (mode === 'recording');
    var paused = (mode === 'paused');
    var active = recording || paused;
    if (els.liveBadge) {
      els.liveBadge.hidden = !active;
      els.liveBadge.classList.toggle('paused', paused);
    }
    if (els.liveLabel) els.liveLabel.textContent = paused ? 'PAUSED' : 'REC';
    // Amber/dimmed glow ring while paused, red breathing ring while recording.
    if (els.root) els.root.classList.toggle('is-paused', paused);
  }

  function formatElapsed(ms) {
    var t = Math.floor(ms / 1000);
    var m = Math.floor(t / 60);
    var s = t % 60;
    // Zero-padded minutes for a stable, camera-style readout (e.g. 07:42).
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  /** Push the elapsed time to both the small chip and the prominent live badge. */
  function renderElapsed(ms) {
    var text = formatElapsed(ms);
    if (els.timer) els.timer.textContent = text;
    if (els.liveTime) els.liveTime.textContent = text;
  }

  var timerHandle = null;
  function startTimer() {
    stopTimer();
    timerHandle = setInterval(function () {
      if (!state.recording || state.paused) return;
      var ms = Date.now() - state.startedAt - state.totalPausedMs;
      renderElapsed(ms);
      var remaining = maxVideoSeconds - ms / 1000;
      // Auto-stop guard: respect the Space's max recording length (higher on GPU Spaces).
      if (remaining <= 0) {
        if (els.countdownBadge) els.countdownBadge.hidden = true;
        setMessage('Reached the ' + Math.round(maxVideoSeconds / 60) + ' min limit — stopping and uploading.');
        stopRecorder();
      } else if (remaining <= 15) {
        if (els.countdownBadge) {
          els.countdownBadge.hidden = false;
          els.countdownBadge.textContent = 'Auto-stop in ' + Math.ceil(remaining) + 's';
        }
      } else if (els.countdownBadge && !els.countdownBadge.hidden) {
        els.countdownBadge.hidden = true;
      }
    }, 400);
  }
  function stopTimer() { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } }

  /** Seconds elapsed in the recording, excluding paused time (matches the on-screen timer). */
  function recordingElapsedSec() {
    if (!state.startedAt) return 0;
    var paused = state.totalPausedMs + (state.paused ? (Date.now() - state.pauseStartedAt) : 0);
    return Math.max(0, (Date.now() - state.startedAt - paused) / 1000);
  }

  /** Log an exact-timestamp mark tied to the current guided step (production reliability path). */
  function addMark() {
    if (!state.recording) {
      setMessage('Start recording before adding a mark.');
      return;
    }
    var t = recordingElapsedSec();
    var s = state.currentStep || {};
    state.marks.push({
      t_sec: Math.round(t * 100) / 100,
      step_index: s.step_index || null,
      section_id: s.section_id || '',
      report_type: s.report_type || '',
      title: s.title || '',
      note: '',
    });
    flashMarkCue();
    renderMarks();
    setMessage('Marked at ' + t.toFixed(1) + 's — keep narrating.');
  }

  function renderMarks() {
    if (!els.marksTrack) return;
    var has = state.marks.length > 0;
    if (els.marksTimeline) els.marksTimeline.hidden = !has;
    els.marksTrack.innerHTML = '';
    state.marks.forEach(function (m, i) {
      var chip = document.createElement('span');
      chip.className = 'mini-mark';
      var label = m.title || ('Mark ' + (i + 1));
      chip.innerHTML = '<b>' + m.t_sec.toFixed(1) + 's</b> ' + label;
      els.marksTrack.appendChild(chip);
    });
  }

  function undoLastMark() {
    if (!state.marks.length) return;
    state.marks.pop();
    renderMarks();
    setMessage(state.marks.length ? 'Removed last mark.' : 'All marks cleared.');
  }

  function setMarkEnabled(on) {
    if (els.btnMark) els.btnMark.disabled = !on;
  }

  function clearStream() {
    stopAudioMeter();
    if (state.stream) {
      state.stream.getTracks().forEach(function (t) { t.stop(); });
      state.stream = null;
    }
    if (els.preview) els.preview.srcObject = null;
  }

  function getMime() {
    if (!MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    // Prefer mp4 (iOS Safari emits this) for direct browser playback; then modern webm codecs.
    var c = [
      'video/mp4;codecs=h264,aac',
      'video/mp4',
      'video/webm;codecs=vp9,opus',
      'video/webm;codecs=vp8,opus',
      'video/webm;codecs=h264,opus',
      'video/webm',
    ];
    for (var i = 0; i < c.length; i++) {
      if (MediaRecorder.isTypeSupported(c[i])) return c[i];
    }
    return '';
  }

  function audioConstraints() {
    return { echoCancellation: true, noiseSuppression: true, sampleRate: 48000, channelCount: 1 };
  }

  function videoConstraints() {
    var dims = state.quality === 'high'
      ? { width: { ideal: 1280 }, height: { ideal: 720 } }
      : { width: { ideal: 960 }, height: { ideal: 540 } };
    return {
      facingMode: { ideal: state.facingMode },
      width: dims.width,
      height: dims.height,
      frameRate: { ideal: 30, max: 30 },
    };
  }

  /** Ordered constraint sets, progressively relaxed so a quirky phone still gets a stream. */
  function constraintLadder() {
    return [
      { video: videoConstraints(), audio: audioConstraints() },
      { video: { facingMode: { ideal: state.facingMode } }, audio: audioConstraints() },
      { video: { facingMode: state.facingMode }, audio: true },
      { video: true, audio: true },
    ];
  }

  /**
   * Verify the acquired stream is usable BEFORE recording. Returns
   * ``{ ok, hasVideo, hasAudio, message, warning }``.
   *
   * Hard-block (ok:false) only for the unrecoverable cases: no live video track, or no audio
   * track at all / an ended-or-disabled one — these produce a useless recording (failure #3:
   * silent video → no STT → no summaries). We do NOT hard-block on ``track.muted`` because
   * that flag is frequently ``true`` transiently right after getUserMedia on mobile and would
   * cause false failures; instead we surface a non-blocking ``warning`` so the inspector can
   * check their mic switch.
   */
  function verifyStreamTracks(stream) {
    var audioTracks = stream.getAudioTracks ? stream.getAudioTracks() : [];
    var videoTracks = stream.getVideoTracks ? stream.getVideoTracks() : [];
    var liveVideo = videoTracks.filter(function (t) { return t.readyState !== 'ended'; });
    var liveAudio = audioTracks.filter(function (t) {
      return t.readyState !== 'ended' && t.enabled !== false;
    });
    logDiag('stream tracks', {
      audio: audioTracks.length,
      audioLive: liveAudio.length,
      video: videoTracks.length,
      videoLive: liveVideo.length,
      audioMuted: audioTracks.map(function (t) { return !!t.muted; }),
      audioLabels: audioTracks.map(function (t) { return t.label || '(unnamed)'; }),
      videoLabels: videoTracks.map(function (t) { return t.label || '(unnamed)'; }),
    });
    if (!liveVideo.length) {
      return {
        ok: false, hasVideo: false, hasAudio: liveAudio.length > 0, warning: '',
        message: 'No live camera track was available. Try flipping the camera, or use ' +
          '"Upload a video instead" below.',
      };
    }
    if (!liveAudio.length) {
      return {
        ok: false, hasVideo: true, hasAudio: false, warning: '',
        message: 'Microphone access is missing or disabled, so this recording would have no ' +
          'audio and could not be transcribed. ' + permissionTips(),
      };
    }
    // Non-blocking heads-up when the mic reports muted at the hardware/OS level.
    var warning = '';
    if (liveAudio.every(function (t) { return t.muted === true; })) {
      warning = 'Your microphone may be muted (check the phone\u2019s mute switch and that no ' +
        'other app is using the mic). Audio is required to transcribe your narration.';
    }
    return { ok: true, hasVideo: true, hasAudio: true, warning: warning, message: '' };
  }

  async function openCamera() {
    if (!hasRecordingSupport()) {
      showPermError('Recording is not supported in this browser. Use Safari/Chrome or upload a video.');
      revealFallbackUpload();
      return false;
    }
    if (!global.isSecureContext) {
      showPermError('HTTPS is required for camera and microphone. Reload over https://');
      return false;
    }
    setLamp('requesting');
    setMessage('Requesting camera and microphone…');
    clearStream();

    var ladder = constraintLadder();
    var lastErr = null;
    for (var i = 0; i < ladder.length; i++) {
      try {
        logDiag('getUserMedia attempt', i + 1, 'of', ladder.length, ladder[i]);
        state.stream = await navigator.mediaDevices.getUserMedia(ladder[i]);
        logDiag('getUserMedia OK on rung', i + 1);
        break;
      } catch (err) {
        lastErr = err;
        logDiag('getUserMedia failed on rung', i + 1, (err && err.name) || err);
        // Permission/secure errors won't be fixed by relaxing constraints — stop early.
        if (err && (err.name === 'NotAllowedError' || err.name === 'SecurityError' ||
                    err.name === 'PermissionDeniedError')) {
          break;
        }
      }
    }

    if (!state.stream) {
      setLamp('error');
      showPermError(describeMediaError(lastErr));
      if (lastErr && (lastErr.name === 'NotFoundError' || lastErr.name === 'NotReadableError')) {
        revealFallbackUpload();
      }
      return false;
    }

    // Guarantee a usable audio + video stream BEFORE we let the user record. A common mobile
    // failure is a granted camera but NO microphone track (mic blocked separately, held by
    // another app, or a hardware mute) — which records silent video, so Deepgram STT and all
    // downstream LLM/summary work never run. Fail loudly here with an actionable message
    // instead of producing an audio-less recording the inspector cannot recover.
    var trackCheck = verifyStreamTracks(state.stream);
    if (!trackCheck.ok) {
      setLamp('error');
      showPermError(trackCheck.message);
      if (!trackCheck.hasVideo) revealFallbackUpload();
      // Keep the user from starting a doomed (e.g. audio-less) recording: disable Start and
      // hold the permission gate open until a usable stream is acquired.
      if (els.btnStart) els.btnStart.disabled = true;
      if (els.permGate) els.permGate.hidden = false;
      clearStream();
      return false;
    }

    try {
      els.preview.srcObject = state.stream;
      els.preview.muted = true;
      els.preview.setAttribute('playsinline', '');
      await els.preview.play().catch(function () {});
      state.permissionGranted = true;
      hidePermGate();
      showPermError('');
      startAudioMeter(state.stream);
      setLamp('ready');
      // If the mic reported muted (non-blocking), tell the user but still let them record —
      // the live audio meter below will confirm whether sound is actually being picked up.
      setMessage(trackCheck.warning || 'Camera ready. Tap \u25CF Record to begin.');
      if (els.btnStart) els.btnStart.disabled = false;
      return true;
    } catch (err) {
      setLamp('error');
      showPermError(describeMediaError(err));
      return false;
    }
  }

  // --- Live audio level meter (Web Audio API) — reassures the user the mic is working ---
  // Sets a clear "Mic active" state the moment recording starts, and a transient "hearing"
  // pulse whenever voice is detected, so the user always knows audio is being captured.
  function setMicActive(active) {
    if (els.micChip) els.micChip.classList.toggle('is-live', !!active);
    if (els.micLabel) els.micLabel.textContent = active ? 'Mic active' : 'Mic';
  }

  function startAudioMeter(stream) {
    stopAudioMeter();
    // Show the active state even if the Web Audio meter isn't supported — the mic IS recording.
    setMicActive(true);
    if (!els.audioMeterFill) return;
    var AC = global.AudioContext || global.webkitAudioContext;
    if (!AC) return;
    try {
      state.audioCtx = new AC();
      var src = state.audioCtx.createMediaStreamSource(stream);
      state.analyser = state.audioCtx.createAnalyser();
      state.analyser.fftSize = 512;
      src.connect(state.analyser);
      var buf = new Uint8Array(state.analyser.frequencyBinCount);
      var tick = function () {
        if (!state.analyser) return;
        state.analyser.getByteTimeDomainData(buf);
        var peak = 0;
        for (var i = 0; i < buf.length; i++) {
          var v = Math.abs(buf[i] - 128);
          if (v > peak) peak = v;
        }
        var pct = Math.min(100, Math.round((peak / 128) * 140));
        els.audioMeterFill.style.width = pct + '%';
        els.audioMeterFill.classList.toggle('hot', pct > 85);
        // Voice-detected pulse on the mic chip when the level clears the ambient-noise floor.
        if (els.micChip) els.micChip.classList.toggle('hearing', pct > 22);
        state.meterRaf = global.requestAnimationFrame(tick);
      };
      tick();
    } catch (_e) { /* meter is best-effort */ }
  }

  function stopAudioMeter() {
    if (state.meterRaf) { global.cancelAnimationFrame(state.meterRaf); state.meterRaf = null; }
    state.analyser = null;
    if (state.audioCtx) {
      try { state.audioCtx.close(); } catch (_e) { /* */ }
      state.audioCtx = null;
    }
    if (els.audioMeterFill) els.audioMeterFill.style.width = '0%';
    if (els.micChip) els.micChip.classList.remove('hearing');
    setMicActive(false);
  }

  // --- Screen Wake Lock: keep the phone screen on during a recording ---
  // Without this, the display dims/locks after the OS idle timeout, which on mobile suspends
  // the tab and can stop/interrupt the MediaRecorder mid-walkthrough. Best-effort: silently
  // no-ops on browsers without the API (falls back to the crash-recovery snapshots).
  async function requestWakeLock() {
    try {
      if (!('wakeLock' in navigator) || !navigator.wakeLock.request) return;
      state.wakeLock = await navigator.wakeLock.request('screen');
      state.wakeLock.addEventListener('release', function () { state.wakeLock = null; });
    } catch (_e) { state.wakeLock = null; /* denied/unsupported — non-fatal */ }
  }

  function releaseWakeLock() {
    if (state.wakeLock) {
      try { state.wakeLock.release(); } catch (_e) { /* */ }
      state.wakeLock = null;
    }
  }

  // iOS Safari has no Screen Wake Lock API, so the display can dim/lock during a long
  // walkthrough and suspend the tab. We can't prevent it — warn the user once per session
  // so they keep the screen awake (tap occasionally / raise auto-lock time). Crash-recovery
  // snapshots still protect the recording if it does get interrupted.
  function maybeWarnScreenSleep() {
    var supported = ('wakeLock' in navigator) && !!navigator.wakeLock;
    if (supported || state.warnedNoWakeLock) return;
    state.warnedNoWakeLock = true;
    setMessage('Tip: keep your screen on during recording (tap occasionally) — auto-lock can interrupt it.');
  }

  /** Re-acquire the wake lock after the OS auto-releases it on tab hide/return. */
  function reacquireWakeLockIfRecording() {
    if (state.recording && !state.wakeLock && document.visibilityState === 'visible') {
      requestWakeLock();
    }
  }

  async function flipCamera() {
    if (state.recording) {
      setMessage('Pause before switching cameras.');
      return;
    }
    state.facingMode = state.facingMode === 'environment' ? 'user' : 'environment';
    var ok = await openCamera();
    if (ok) setMessage((state.facingMode === 'user' ? 'Front' : 'Rear') + ' camera selected.');
  }

  function setQuality(q) {
    state.quality = q === 'high' ? 'high' : 'balanced';
    if (els.qBalanced) els.qBalanced.classList.toggle('is-on', state.quality === 'balanced');
    if (els.qHigh) els.qHigh.classList.toggle('is-on', state.quality === 'high');
    // Apply on next camera open; if a preview is live and not recording, refresh it now.
    if (state.stream && !state.recording) openCamera();
  }

  function startRecorder() {
    state.chunks = [];
    state.firstChunkSnapshotDone = false;
    state.selectedMime = getMime();
    try {
      state.mediaRecorder = state.selectedMime
        ? new MediaRecorder(state.stream, { mimeType: state.selectedMime })
        : new MediaRecorder(state.stream);
    } catch (_e) {
      state.mediaRecorder = new MediaRecorder(state.stream);
    }
    // Log the codec actually chosen by the browser — the effective mimeType can differ from
    // what we requested (esp. iOS Safari), and a wrong/empty one is a common cause of empty
    // or unplayable recordings.
    logDiag('MediaRecorder started', {
      requestedMime: state.selectedMime || '(browser default)',
      effectiveMime: (state.mediaRecorder && state.mediaRecorder.mimeType) || '(unknown)',
      audioBitsPerSecond: state.mediaRecorder.audioBitsPerSecond,
      videoBitsPerSecond: state.mediaRecorder.videoBitsPerSecond,
    });
    state.mediaRecorder.ondataavailable = function (e) {
      if (e.data && e.data.size) {
        state.chunks.push(e.data);
        if (!state.firstChunkSnapshotDone) {
          logDiag('first data chunk', e.data.size, 'bytes; total chunks so far', state.chunks.length);
        }
        // Write a crash-recovery snapshot as soon as the first real data lands, so an
        // interruption in the first few seconds still leaves something recoverable
        // (the periodic snapshot interval alone could miss a very early crash).
        if (!state.firstChunkSnapshotDone) {
          state.firstChunkSnapshotDone = true;
          try { writeInProgressSnapshot(); } catch (_e) { /* best-effort */ }
        }
      } else {
        logDiag('empty data chunk received (size 0)');
      }
    };
    state.mediaRecorder.onerror = function (ev) {
      var err = ev && ev.error;
      setMessage('Recording error: ' + ((err && err.name) || 'unknown') + '. Saving what we have…');
    };
    state.mediaRecorder.onstop = function () {
      stopTimer();
      stopSnapshots();
      if (els.countdownBadge) els.countdownBadge.hidden = true;
      var mime = (state.mediaRecorder && state.mediaRecorder.mimeType) || state.selectedMime || 'video/webm';
      if (!state.chunks.length) {
        logDiag('onstop: no chunks captured');
        setLamp('error');
        setMessage('No video data was captured. Try again, or use "Upload a video instead".');
        revealFallbackUpload();
        setRecordingUI(false);
        if (els.btnStart) els.btnStart.disabled = false;
        return;
      }
      state.pendingBlob = new Blob(state.chunks, { type: mime });
      logDiag('onstop: assembled blob', { bytes: state.pendingBlob.size, mime: mime, chunks: state.chunks.length });
      // Guard against a zero-byte blob (all chunks empty) — uploading it is pointless and
      // yields a confusing server-side "empty upload" error.
      if (!state.pendingBlob.size) {
        setLamp('error');
        setMessage('The recording came out empty (0 bytes). Please re-record, or use "Upload a video instead".');
        revealFallbackUpload();
        setRecordingUI(false);
        if (els.btnStart) els.btnStart.disabled = false;
        return;
      }
      // MOBILE-FIRST DURABILITY: persist the FULL recording to this device BEFORE attempting
      // any upload. If the network is down, the tab is killed, or the user navigates away
      // during upload, the video is already safe on-device and can be restored/retried or
      // downloaded — the recording is never lost just because the server was unreachable.
      saveFinalDraftLocally(state.pendingBlob, mime);
      offerDownload(state.pendingBlob, mime);
      setMessage('Recording saved on this device. Uploading…');
      uploadWithRetry(state.pendingBlob, mime);
    };
    // Timeslice keeps chunks flowing so periodic crash-recovery snapshots stay current.
    state.mediaRecorder.start(1000);
    startSnapshots(state.selectedMime || 'video/webm');
    requestWakeLock(); // keep the screen awake for the whole walkthrough
    state.recording = true;
    state.paused = false;
    state.startedAt = Date.now();
    state.totalPausedMs = 0;
    state.marks = [];
    // One continuous recording spans every step; seed the boundary log with the current step.
    state.stepEvents = [];
    state.lastStepEventKey = '';
    recordStepEvent(state.currentStep);
    renderMarks();
    setLamp('recording');
    setMessage('Recording — tap MARK at each finding, use Next between areas.');
    // Show the "keep screen on" reminder just after the recording confirmation so it isn't
    // immediately overwritten (fires once, only where Wake Lock is unavailable, e.g. iOS).
    if (!state.warnedNoWakeLock) setTimeout(maybeWarnScreenSleep, 3800);
    renderElapsed(0); // seed the live badge/timer at 00:00 before the first tick
    startTimer();
    if (els.btnPause) els.btnPause.disabled = false;
    if (els.btnStop) els.btnStop.disabled = false;
    setRecordingUI(true);
    setMarkEnabled(true);
    var s = state.currentStep;
    speakText((s && (s.spoken_intro || s.voice_prompt)) || ('Begin. Say ' + identificationPhrase + ' at each stop.'));
  }

  function pauseRecorder() {
    if (!state.mediaRecorder || state.mediaRecorder.state !== 'recording') return;
    state.mediaRecorder.pause();
    state.paused = true;
    state.pauseStartedAt = Date.now();
    setLamp('paused');
    setMessage('Paused — tap Resume when ready.');
    if (els.btnPause) els.btnPause.textContent = 'Resume';
    setMarkEnabled(false);
  }

  function resumeRecorder() {
    if (!state.mediaRecorder || state.mediaRecorder.state !== 'paused') return;
    state.totalPausedMs += Date.now() - state.pauseStartedAt;
    state.mediaRecorder.resume();
    state.paused = false;
    setLamp('recording');
    setMessage('Recording resumed.');
    if (els.btnPause) els.btnPause.textContent = 'Pause';
    setMarkEnabled(true);
  }

  function stopRecorder() {
    if (state.mediaRecorder && state.mediaRecorder.state !== 'inactive') {
      setLamp('uploading');
      setMessage('Stopping…');
      state.mediaRecorder.stop();
    }
    releaseWakeLock();
    state.recording = false;
    if (els.btnStop) els.btnStop.disabled = true;
    if (els.btnPause) els.btnPause.disabled = true;
    setRecordingUI(false);
    setMarkEnabled(false);
    if (state.sessionMeta && state.sessionMeta.outro_script) {
      speakText(state.sessionMeta.outro_script);
    }
  }

  function setUploadProgress(pct) {
    if (!els.uploadBar) return;
    els.uploadBar.hidden = pct < 0;
    if (els.uploadFill) els.uploadFill.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  function uploadWithRetry(blob, mime, attempt) {
    attempt = attempt || 1;
    var maxAttempts = 4;
    setUploadProgress(5);
    setMessage('Uploading (attempt ' + attempt + '/' + maxAttempts + ')…');
    if (els.btnRetryUpload) els.btnRetryUpload.hidden = true;

    var fd = new FormData();
    var ext = (mime || '').indexOf('mp4') >= 0 ? 'mp4' : 'webm';
    fd.append('video', blob, 'field-' + Date.now() + '.' + ext);
    reportTypesFromDom().forEach(function (rt) { fd.append('report_types', rt); });
    // Job details captured on the home screen — surfaced on the PDF cover.
    if (C.jobAddress) fd.append('job_address', C.jobAddress);
    if (C.jobNotes) fd.append('job_notes', C.jobNotes);
    // Explicit mark events (exact timestamps + step context) for section alignment.
    try { fd.append('mark_events', JSON.stringify(state.marks || [])); } catch (_e) { /* */ }
    // Step boundaries from the single continuous recording (when each guided step began).
    try { fd.append('step_events', JSON.stringify(state.stepEvents || [])); } catch (_e) { /* */ }

    logDiag('upload start', { attempt: attempt, bytes: blob && blob.size, mime: mime, url: uploadUrl });

    var xhr = new XMLHttpRequest();
    xhr.open('POST', uploadUrl);
    xhr.setRequestHeader('X-JobDoc-Client', '1');
    xhr.timeout = 300000;

    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable) {
        var pct = Math.round((ev.loaded / ev.total) * 100);
        setUploadProgress(pct);
        setMessage('Uploading… ' + pct + '%');
      }
    };

    xhr.onload = function () {
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (_e) { data = null; }
      logDiag('upload response', { status: xhr.status, ok: data && data.ok, error: data && data.error });
      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok && data.redirect_url) {
        setUploadProgress(100);
        setMessage('Upload complete!');
        if (els.btnDownload) els.btnDownload.hidden = true;
        clearLocalDraft();
        clearInProgress();
        window.location.href = data.redirect_url;
        return;
      }
      // Surface the server's specific, non-technical reason (empty upload, too long, storage
      // busy) verbatim when present, and don't pointlessly retry a hard 4xx rejection.
      var serverMsg = (data && data.error) || 'Server rejected upload.';
      var hardReject = xhr.status === 400 || xhr.status === 413;
      if (hardReject) {
        saveDraftLocally(blob, mime);
        setUploadProgress(-1);
        setLamp('error');
        setMessage(serverMsg + ' Your recording is saved on this device.');
        if (els.btnRetryUpload) {
          els.btnRetryUpload.hidden = false;
          els.btnRetryUpload.onclick = function () { uploadWithRetry(blob, mime, 1); };
        }
        setRecordingUI(false);
        if (els.btnStart) els.btnStart.disabled = false;
        return;
      }
      retryOrFail(blob, mime, attempt, maxAttempts, serverMsg);
    };
    xhr.onerror = function () {
      logDiag('upload network error', { attempt: attempt });
      retryOrFail(blob, mime, attempt, maxAttempts, 'Network error — check your signal.');
    };
    xhr.ontimeout = function () {
      logDiag('upload timeout', { attempt: attempt });
      retryOrFail(blob, mime, attempt, maxAttempts, 'Upload timed out on a slow connection.');
    };
    xhr.send(fd);
  }

  function openDraftDb(cb) {
    if (!global.indexedDB) { if (cb) cb(); return; }
    var req = indexedDB.open(DRAFT_DB, 1);
    req.onupgradeneeded = function (ev) {
      var db = ev.target.result;
      if (!db.objectStoreNames.contains(DRAFT_STORE)) db.createObjectStore(DRAFT_STORE);
    };
    req.onsuccess = function (ev) { state.draftDb = ev.target.result; if (cb) cb(); };
    req.onerror = function () { if (cb) cb(); };
  }

  function saveDraftLocally(blob, mime) {
    if (!state.draftDb) return;
    var reportTypes = reportTypesFromDom();
    try {
      var tx = state.draftDb.transaction(DRAFT_STORE, 'readwrite');
      tx.objectStore(DRAFT_STORE).put({ blob: blob, mime: mime, savedAt: new Date().toISOString(), reportTypes: reportTypes, marks: state.marks || [], stepEvents: state.stepEvents || [] }, DRAFT_KEY);
      localStorage.setItem('jobdoc_has_draft', '1');
    } catch (_e) { /* */ }
  }

  /**
   * Persist the finished recording to IndexedDB immediately on stop (before upload). Reuses
   * the same DRAFT_KEY slot that the restore-draft UI reads, so a reload after an interrupted
   * upload offers the saved video. Cleared automatically once the upload succeeds.
   */
  function saveFinalDraftLocally(blob, mime) {
    try {
      saveDraftLocally(blob, mime);
      logDiag('saved recording to IndexedDB', { bytes: blob.size, mime: mime });
    } catch (e) {
      logDiag('failed to save recording locally', e);
    }
  }

  /**
   * Give the user a way to keep the video even if the server is never reachable: native Share
   * sheet when available (best on mobile — save to Files/Photos, send via Messages), otherwise
   * a plain download link. Wired to a button that stays available after stop / on failure.
   */
  var _downloadUrl = null;
  function offerDownload(blob, mime) {
    if (!els.btnDownload) return;
    var ext = (mime || '').indexOf('mp4') >= 0 ? 'mp4' : 'webm';
    var fname = 'jobdoc-recording-' + Date.now() + '.' + ext;
    els.btnDownload.hidden = false;
    els.btnDownload.onclick = function () {
      // Prefer the native share sheet on mobile (lets the user save to Photos/Files reliably).
      try {
        if (navigator.canShare && navigator.share) {
          var file = new File([blob], fname, { type: mime || 'video/mp4' });
          if (navigator.canShare({ files: [file] })) {
            navigator.share({ files: [file], title: 'JobDoc recording' })
              .catch(function () { /* user cancelled — non-fatal */ });
            return;
          }
        }
      } catch (_e) { /* fall through to download */ }
      try {
        if (_downloadUrl) URL.revokeObjectURL(_downloadUrl);
        _downloadUrl = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = _downloadUrl;
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      } catch (e) {
        logDiag('download failed', e);
        setMessage('Could not export the file on this browser.');
      }
    };
  }

  function clearLocalDraft() {
    localStorage.removeItem('jobdoc_has_draft');
    if (!state.draftDb) return;
    try {
      var tx = state.draftDb.transaction(DRAFT_STORE, 'readwrite');
      tx.objectStore(DRAFT_STORE).delete(DRAFT_KEY);
    } catch (_e) { /* */ }
    if (els.draftRestore) els.draftRestore.hidden = true;
  }

  // --- Crash recovery: periodically snapshot the in-progress recording to IndexedDB ---
  /** Write the current in-progress recording to IndexedDB (best-effort, never throws). */
  function writeInProgressSnapshot(mime) {
    if (!state.recording || !state.draftDb || !state.chunks.length) return;
    try {
      var payload = {
        chunks: state.chunks.slice(),
        mime: mime || state.selectedMime || 'video/webm',
        marks: state.marks || [],
        stepEvents: state.stepEvents || [],
        reportTypes: reportTypesFromDom(),
        savedAt: new Date().toISOString(),
      };
      var tx = state.draftDb.transaction(DRAFT_STORE, 'readwrite');
      tx.objectStore(DRAFT_STORE).put(payload, INPROGRESS_KEY);
      localStorage.setItem('jobdoc_inprogress', '1');
    } catch (_e) { /* best-effort */ }
  }

  function startSnapshots(mime) {
    stopSnapshots();
    state.snapshotHandle = setInterval(function () {
      writeInProgressSnapshot(mime);
    }, SNAPSHOT_EVERY_MS);
  }

  /**
   * Force a fresh snapshot when the app is about to be backgrounded/closed. On mobile the
   * setInterval snapshot can be throttled or skipped when the tab hides, so we ask the
   * MediaRecorder to flush a chunk (requestData) and immediately persist it. This is the
   * key resilience path for phone calls, app switches, and low-memory tab kills.
   */
  function flushSnapshotOnHide() {
    if (!state.recording) return;
    try {
      if (state.mediaRecorder && state.mediaRecorder.state === 'recording' &&
          typeof state.mediaRecorder.requestData === 'function') {
        state.mediaRecorder.requestData(); // triggers ondataavailable synchronously-ish
      }
    } catch (_e) { /* */ }
    // Persist after the flushed chunk lands (next tick), and also right now as a safety net.
    writeInProgressSnapshot();
    setTimeout(function () { writeInProgressSnapshot(); }, 60);
  }

  function stopSnapshots() {
    if (state.snapshotHandle) { clearInterval(state.snapshotHandle); state.snapshotHandle = null; }
  }

  function clearInProgress() {
    localStorage.removeItem('jobdoc_inprogress');
    if (!state.draftDb) return;
    try {
      var tx = state.draftDb.transaction(DRAFT_STORE, 'readwrite');
      tx.objectStore(DRAFT_STORE).delete(INPROGRESS_KEY);
    } catch (_e) { /* */ }
    if (els.recoverBar) els.recoverBar.hidden = true;
  }

  /** On load, offer to recover a recording that was interrupted (refresh/background/battery). */
  function checkForInProgress() {
    if (localStorage.getItem('jobdoc_inprogress') !== '1' || !state.draftDb) return;
    var tx = state.draftDb.transaction(DRAFT_STORE, 'readonly');
    var req = tx.objectStore(DRAFT_STORE).get(INPROGRESS_KEY);
    req.onsuccess = function () {
      var rec = req.result;
      if (!rec || !rec.chunks || !rec.chunks.length || !els.recoverBar) return;
      els.recoverBar.hidden = false;
      if (els.btnRecover) {
        els.btnRecover.onclick = function () {
          state.marks = rec.marks || [];
          state.stepEvents = rec.stepEvents || [];
          renderMarks();
          var blob = new Blob(rec.chunks, { type: rec.mime || 'video/webm' });
          els.recoverBar.hidden = true;
          uploadWithRetry(blob, rec.mime || 'video/webm', 1);
        };
      }
      if (els.btnDiscardRecover) {
        els.btnDiscardRecover.onclick = function () {
          clearInProgress();
          setMessage('Interrupted recording discarded.');
        };
      }
    };
  }

  function checkForLocalDraft() {
    if (localStorage.getItem('jobdoc_has_draft') !== '1' || !state.draftDb) return;
    var tx = state.draftDb.transaction(DRAFT_STORE, 'readonly');
    var req = tx.objectStore(DRAFT_STORE).get(DRAFT_KEY);
    req.onsuccess = function () {
      if (req.result && req.result.blob && els.draftRestore) {
        els.draftRestore.hidden = false;
        state.pendingBlob = req.result.blob;
        if (els.btnRestoreDraft) {
          els.btnRestoreDraft.onclick = function () {
            state.marks = req.result.marks || [];
            state.stepEvents = req.result.stepEvents || [];
            renderMarks();
            uploadWithRetry(req.result.blob, req.result.mime || 'video/webm', 1);
          };
        }
        if (els.btnDiscardDraft) {
          els.btnDiscardDraft.onclick = function () {
            clearLocalDraft();
            setMessage('Draft discarded.');
          };
        }
      }
    };
  }

  function retryOrFail(blob, mime, attempt, maxAttempts, reason) {
    if (attempt < maxAttempts) {
      var delay = Math.min(8000, 1500 * attempt);
      setMessage(reason + ' Retrying in ' + Math.round(delay / 1000) + 's…');
      setTimeout(function () { uploadWithRetry(blob, mime, attempt + 1); }, delay);
      return;
    }
    saveDraftLocally(blob, mime);
    offerDownload(blob, mime); // ensure export stays available even for restored/recovered blobs
    setUploadProgress(-1);
    setLamp('error');
    setMessage(reason + ' Recording saved on device — tap Retry upload when online, or Save/Share it.');
    if (els.btnRetryUpload) {
      els.btnRetryUpload.hidden = false;
      els.btnRetryUpload.onclick = function () { uploadWithRetry(blob, mime, 1); };
    }
    setRecordingUI(false);
    if (els.btnStart) els.btnStart.disabled = false;
    if (els.btnCam) els.btnCam.disabled = false;
    clearStream();
  }

  function runCountdown(cb) {
    var overlay = $('countdown-overlay');
    var num = $('countdown-num');
    if (!overlay || !num) { cb(); return; }
    var n = 3;
    overlay.classList.add('show');
    num.textContent = String(n);
    var iv = setInterval(function () {
      n -= 1;
      if (n <= 0) {
        clearInterval(iv);
        overlay.classList.remove('show');
        cb();
        return;
      }
      num.textContent = String(n);
    }, 900);
  }

  function bindControls() {
    if (els.btnCam) els.btnCam.addEventListener('click', function () { openCamera(); });
    if (els.btnStart) {
      els.btnStart.addEventListener('click', function () {
        runCountdown(async function () {
          if (!state.stream) {
            var ok = await openCamera();
            if (!ok) return;
          }
          startRecorder();
        });
      });
    }
    if (els.btnPause) {
      els.btnPause.addEventListener('click', function () {
        if (state.paused) resumeRecorder();
        else pauseRecorder();
      });
    }
    if (els.btnStop) els.btnStop.addEventListener('click', stopRecorder);
    if (els.btnSpeak) {
      els.btnSpeak.addEventListener('click', function () {
        var s = state.currentStep;
        if (s) speakText(s.spoken_intro || s.voice_prompt);
      });
    }
    if (els.btnMuteVoice) els.btnMuteVoice.addEventListener('click', toggleVoiceMute);
    if (els.btnVoiceToggle) els.btnVoiceToggle.addEventListener('click', toggleVoiceMute);
    if (els.btnNext) {
      els.btnNext.addEventListener('click', function () {
        if (state.currentStep && state.currentStep.is_last) {
          setMessage('Last section — tap ■ Upload when finished.');
          return;
        }
        advanceViaServer();
      });
    }
    document.querySelectorAll('[data-goto-step]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var i = parseInt(btn.getAttribute('data-goto-step'), 10);
        if (!isNaN(i)) gotoStep(i);
      });
    });
    if (els.btnMark) els.btnMark.addEventListener('click', addMark);
    if (els.btnUndoMark) els.btnUndoMark.addEventListener('click', undoLastMark);
    // Tap anywhere on the live preview to MARK while recording (native-camera feel).
    if (els.videoWrap) {
      els.videoWrap.addEventListener('click', function () {
        if (state.recording && !state.paused) addMark();
      });
    }
    // Collapse / expand the center guidance card to clear the viewfinder.
    if (els.btnGuidanceCollapse && els.guidance) {
      els.btnGuidanceCollapse.addEventListener('click', function () {
        var collapsed = els.guidance.classList.toggle('collapsed');
        els.btnGuidanceCollapse.setAttribute('aria-label', collapsed ? 'Expand guidance' : 'Collapse guidance');
      });
    }
    if (els.btnFlipCam) els.btnFlipCam.addEventListener('click', function () { flipCamera(); });
    if (els.qBalanced) els.qBalanced.addEventListener('click', function () { setQuality('balanced'); });
    if (els.qHigh) els.qHigh.addEventListener('click', function () { setQuality('high'); });
    if (els.btnEnable) {
      els.btnEnable.addEventListener('click', function () {
        els.btnEnable.disabled = true;
        openCamera().finally(function () { els.btnEnable.disabled = false; });
      });
    }
    if (els.fallbackInput) {
      els.fallbackInput.addEventListener('change', function () {
        var file = els.fallbackInput.files && els.fallbackInput.files[0];
        if (!file) return;
        state.marks = state.marks || [];
        setLamp('uploading');
        setMessage('Uploading selected video…');
        uploadWithRetry(file, file.type || 'video/mp4', 1);
      });
    }
  }

  /** Guard the recording against backgrounding / accidental navigation (crash resilience). */
  function bindLifecycleGuards() {
    // Flush a recovery snapshot the moment the page is hidden or being unloaded.
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') flushSnapshotOnHide();
      else reacquireWakeLockIfRecording(); // OS releases the wake lock on hide — re-arm it
    });
    global.addEventListener('pagehide', flushSnapshotOnHide);
    global.addEventListener('freeze', flushSnapshotOnHide); // Chrome page lifecycle

    // Warn before leaving mid-recording so a stray back/refresh doesn't lose the walkthrough.
    global.addEventListener('beforeunload', function (e) {
      if (state.recording) {
        flushSnapshotOnHide();
        e.preventDefault();
        e.returnValue = 'You are still recording. Leaving now may lose this walkthrough.';
        return e.returnValue;
      }
    });
  }

  global.JobDocFieldRecorder = { init: init };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
