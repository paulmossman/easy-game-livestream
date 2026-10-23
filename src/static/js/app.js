const socket = io();
let youtubeChannels = [];
let latestState = {};
let previewHls = null;
let relayMetricSamples = [];
const youtubeCreatePendingKey = 'youtube-create-pending';
const youtubeStatusPollIntervalMs = 15000;
const relayMetricsWindowStorageKey = 'relay-metrics-window-minutes';
const previewModeStorageKey = 'preview-mode';
const previewHostStorageKey = 'webrtc-preview-host';
const toggleBufferMs = Number(document.body.dataset.toggleBufferMs);
let relayMetricsWindowMinutes = 15;

function webrtcPreviewUrl() {
    const selectedHost = document.getElementById('preview-host-select').value;
    const previewHost = selectedHost === 'page' ? window.location.hostname : selectedHost;
    const query = new URLSearchParams({
        controls: 'yes',
        muted: 'yes',
        autoplay: 'yes',
        playsinline: 'yes'
    });
    return `${window.location.protocol}//${previewHost}:8889/live/preview/?${query.toString()}`;
}

function hlsPreviewUrl() {
    return `${window.location.protocol}//${window.location.hostname}:8888/live/preview_hls/index.m3u8`;
}

function previewMode() {
    const selected = document.querySelector('input[name="preview-mode"]:checked');
    return selected ? selected.value : 'webrtc';
}

function teardownHlsPreview() {
    const previewVideo = document.getElementById('preview-video');
    if (previewHls) {
        previewHls.destroy();
        previewHls = null;
    }
    previewVideo.pause();
    previewVideo.removeAttribute('src');
    previewVideo.load();
}

function initializeHlsPreview() {
    const previewVideo = document.getElementById('preview-video');
    const sourceUrl = hlsPreviewUrl();

    teardownHlsPreview();

    if (window.Hls && window.Hls.isSupported()) {
        previewHls = new window.Hls();
        previewHls.loadSource(sourceUrl);
        previewHls.attachMedia(previewVideo);
        return;
    }

    previewVideo.src = sourceUrl;
}

function applyPreviewSource() {
    const previewFrame = document.getElementById('preview-frame');
    const previewVideo = document.getElementById('preview-video');
    const mode = previewMode();

    if (mode === 'hls') {
        previewFrame.src = 'about:blank';
        previewFrame.classList.add('is-hidden');
        previewVideo.classList.remove('is-hidden');
        initializeHlsPreview();
    } else {
        teardownHlsPreview();
        previewVideo.classList.add('is-hidden');
        previewFrame.classList.remove('is-hidden');
        previewFrame.src = webrtcPreviewUrl();
    }
}

function setPreviewVisibility(showVideo) {
    const mode = previewMode();
    document.getElementById('preview-host-group').classList.toggle('is-hidden', mode !== 'webrtc');
    document.getElementById('preview-frame').classList.toggle('is-hidden', !showVideo || mode !== 'webrtc');
    document.getElementById('preview-video').classList.toggle('is-hidden', !showVideo || mode !== 'hls');
    document.getElementById('overlay-mock').classList.toggle('is-hidden', showVideo);
}

function updatePreviewAudioWarning(state) {
    const warning = document.getElementById('preview-audio-warning');
    if (!warning) {
        return;
    }

    const shouldWarn = Boolean(state.incoming_stream_ready) && !Boolean(state.incoming_audio_active);
    warning.classList.toggle('is-hidden', !shouldWarn);
}

function initializePreviewPlayer() {
    const previewFrame = document.getElementById('preview-frame');
    if (!previewFrame) {
        return;
    }

    initializePreviewHostOptions();

    const savedMode = window.localStorage.getItem(previewModeStorageKey);
    const selectedMode = savedMode === 'hls' ? 'hls' : 'webrtc';
    const selectedRadio = document.querySelector(`input[name="preview-mode"][value="${selectedMode}"]`);
    if (selectedRadio) {
        selectedRadio.checked = true;
    }

    applyPreviewSource();
    setPreviewVisibility(document.getElementById('show-video').checked);
}

function initializePreviewHostOptions() {
    const previewFrame = document.getElementById('preview-frame');
    const hostSelect = document.getElementById('preview-host-select');
    const pageHostOption = document.createElement('option');
    pageHostOption.value = 'page';
    pageHostOption.textContent = `${window.location.hostname} (can fail under VPN)`;
    hostSelect.append(pageHostOption);

    const additionalHosts = previewFrame.dataset.webrtcAdditionalHosts
        .split(',')
        .map((host) => host.trim())
        .filter((host, index, hosts) => host && hosts.indexOf(host) === index);
    additionalHosts.forEach((host) => {
        const option = document.createElement('option');
        option.value = host;
        option.textContent = host;
        hostSelect.append(option);
    });

    const savedHost = window.localStorage.getItem(previewHostStorageKey);
    hostSelect.value = [...hostSelect.options].some((option) => option.value === savedHost)
        ? savedHost
        : 'page';
    hostSelect.addEventListener('change', handlePreviewHostChange);
}

function handlePreviewHostChange(event) {
    window.localStorage.setItem(previewHostStorageKey, event.target.value);
    if (previewMode() === 'webrtc') {
        document.getElementById('preview-frame').src = webrtcPreviewUrl();
    }
}

function handlePreviewModeChange(event) {
    window.localStorage.setItem(previewModeStorageKey, event.target.value);
    applyPreviewSource();
    setPreviewVisibility(document.getElementById('show-video').checked);
}

function setYoutubeStatus(title, detail) {
    document.getElementById('youtube-status').textContent = title;
    const detailElement = document.getElementById('youtube-detail');
    detailElement.dataset.statusText = detail;
    delete detailElement.dataset.broadcastUrl;
    delete detailElement.dataset.broadcastText;
    renderYouTubeDetail();
}

function setYouTubeLiveMeta(text = '') {
    document.getElementById('youtube-detail').dataset.liveMeta = text;
    renderYouTubeDetail();
}

function renderYouTubeDetail() {
    const detail = document.getElementById('youtube-detail');
    const primaryText = detail.dataset.broadcastText || detail.dataset.statusText || '';
    const liveMeta = detail.dataset.liveMeta || '';
    detail.textContent = [primaryText, liveMeta].filter(Boolean).join(' · ');

    if (detail.dataset.broadcastUrl) {
        detail.href = detail.dataset.broadcastUrl;
        detail.classList.add('youtube-link');
    } else {
        detail.removeAttribute('href');
        detail.classList.remove('youtube-link');
    }
}

function setYouTubeStreamHealth(text = '') {
    const health = document.getElementById('youtube-stream-health');
    const hasText = Boolean(text);
    health.textContent = text;
    health.classList.toggle('is-hidden', !hasText);
}

function formatViewerCount(viewers) {
    if (typeof viewers !== 'number' || Number.isNaN(viewers)) {
        return '';
    }
    return `${new Intl.NumberFormat().format(viewers)} live viewer${viewers === 1 ? '' : 's'}`;
}

function youtubeLiveMetaText(activeDestination) {
    if (!activeDestination) {
        return '';
    }

    const liveStatus = activeDestination.life_cycle_status || activeDestination.broadcast_status || '';
    const viewerCountText = formatViewerCount(activeDestination.concurrent_viewers);
    if (liveStatus === 'live') {
        return viewerCountText ? `Live now · ${viewerCountText}` : 'Live now';
    }
    if (liveStatus === 'testing' || liveStatus === 'testStarting') {
        return 'YouTube preview/test mode';
    }
    if (liveStatus === 'liveStarting') {
        return 'Starting on YouTube';
    }
    if (liveStatus === 'ready' || liveStatus === 'created') {
        return 'Created on YouTube, waiting to go live';
    }
    if (liveStatus === 'complete') {
        return 'YouTube stream ended';
    }
    return liveStatus ? `YouTube status: ${liveStatus}` : '';
}

function youtubeStreamHealthText(activeDestination) {
    const streamStatus = activeDestination && activeDestination.stream_status;
    if (!streamStatus) {
        return '';
    }

    const inputStatusLabels = {
        active: 'Receiving data',
        created: 'Created',
        error: 'Error',
        inactive: 'Not receiving data',
        ready: 'Ready'
    };
    const healthStatus = streamStatus.healthStatus || {};
    const healthLabels = {
        good: 'Good',
        ok: 'OK',
        bad: 'Bad',
        noData: 'Health data unavailable'
    };
    const details = [];
    if (streamStatus.streamStatus) {
        details.push(`Input: ${inputStatusLabels[streamStatus.streamStatus] || streamStatus.streamStatus}`);
    }
    if (healthStatus.status) {
        details.push(`Overall stream health: ${healthLabels[healthStatus.status] || healthStatus.status}`);
    }
    const updatedAtSeconds = Number(healthStatus.lastUpdateTimeSeconds);
    const youtubeUpdatedAt = Number.isFinite(updatedAtSeconds) && updatedAtSeconds > 0
        ? new Date(updatedAtSeconds * 1000)
        : null;
    const hasYoutubeUpdatedAt = youtubeUpdatedAt && !Number.isNaN(youtubeUpdatedAt.getTime());
    const displayedUpdatedAt = hasYoutubeUpdatedAt ? youtubeUpdatedAt : new Date();
    const updateTimeLabel = hasYoutubeUpdatedAt ? 'Health updated' : 'Health checked';
    const formattedUpdatedAt = new Intl.DateTimeFormat(undefined, {
        timeStyle: 'medium'
    }).format(displayedUpdatedAt);
    details.push(`${updateTimeLabel}: ${formattedUpdatedAt}`);

    const issues = Array.isArray(healthStatus.configurationIssues)
        ? healthStatus.configurationIssues
        : [];
    issues.forEach((issue) => {
        const severity = issue.severity ? `${issue.severity}: ` : '';
        const description = issue.description || issue.reason;
        if (description) {
            details.push(`${severity}${description}`);
        }
    });

    return details.join('\n');
}

function drawRelayMetricsChart(samples) {
    const canvas = document.getElementById('relay-metrics-chart');
    const bounds = canvas.getBoundingClientRect();
    if (!bounds.width || !samples.length) {
        return;
    }

    const pixelRatio = window.devicePixelRatio || 1;
    canvas.width = Math.round(bounds.width * pixelRatio);
    canvas.height = Math.round(bounds.height * pixelRatio);
    const context = canvas.getContext('2d');
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, bounds.width, bounds.height);

    const left = 42;
    const right = bounds.width - 8;
    const chartWidth = Math.max(1, right - left);
    const windowEnd = Date.now() / 1000;
    const windowSeconds = relayMetricsWindowMinutes * 60;
    const windowStart = windowEnd - windowSeconds;
    const lanes = [
        {
            key: 'speed',
            title: 'Speed (x real time)',
            color: '#087f70',
            top: 12,
            height: 56,
            maximum: Math.max(1.5, Math.ceil(Math.max(...samples.map((sample) => sample.speed || 0)) * 2) / 2)
        },
        {
            key: 'send_mbps',
            title: 'Output (Mbps)',
            color: '#c45132',
            top: 94,
            height: 56,
            maximum: Math.max(1, Math.ceil(Math.max(...samples.map((sample) => sample.send_mbps || 0)) * 2) / 2)
        }
    ];

    context.font = '11px "Avenir Next", "Helvetica Neue", sans-serif';
    context.lineWidth = 1;
    lanes.forEach((lane) => {
        const plotTop = lane.top + 13;
        const plotBottom = lane.top + lane.height;
        const plotHeight = plotBottom - plotTop;
        context.fillStyle = '#3d3934';
        context.fillText(lane.title, 0, lane.top + 8);
        context.strokeStyle = 'rgba(31, 26, 23, 0.14)';
        context.fillStyle = '#706b64';
        [0, 0.5, 1].forEach((fraction) => {
            const y = plotBottom - (fraction * plotHeight);
            context.beginPath();
            context.moveTo(left, y);
            context.lineTo(right, y);
            context.stroke();
            context.fillText((lane.maximum * fraction).toFixed(lane.maximum < 2 ? 1 : 0), 0, y + 4);
        });

        if (lane.key === 'speed' && lane.maximum >= 1) {
            const realtimeY = plotBottom - (plotHeight / lane.maximum);
            context.save();
            context.setLineDash([4, 4]);
            context.strokeStyle = '#a8462d';
            context.beginPath();
            context.moveTo(left, realtimeY);
            context.lineTo(right, realtimeY);
            context.stroke();
            context.restore();
        }

        context.strokeStyle = lane.color;
        context.lineWidth = 2;
        context.beginPath();
        let started = false;
        samples.forEach((sample) => {
            const value = sample[lane.key];
            if (typeof value !== 'number' || !Number.isFinite(value)) {
                started = false;
                return;
            }
            const x = left + (Math.max(0, Math.min(1, (sample.timestamp - windowStart) / windowSeconds)) * chartWidth);
            const y = plotBottom - (Math.max(0, Math.min(lane.maximum, value)) / lane.maximum * plotHeight);
            if (started) {
                context.lineTo(x, y);
            } else {
                context.moveTo(x, y);
                started = true;
            }
        });
        context.stroke();
    });

    context.fillStyle = '#706b64';
    context.fillText(`${relayMetricsWindowMinutes} min ago`, left, bounds.height - 2);
    const nowLabel = 'now';
    context.fillText(nowLabel, right - context.measureText(nowLabel).width, bounds.height - 2);
}

async function loadRelayMetrics() {
    try {
        const query = new URLSearchParams({ minutes: String(relayMetricsWindowMinutes) });
        const response = await fetch(`/api/stream/metrics?${query.toString()}`, { cache: 'no-store' });
        if (!response.ok) {
            return;
        }
        const data = await response.json();
        relayMetricSamples = Array.isArray(data.samples) ? data.samples : [];
        const panel = document.getElementById('relay-metrics');
        panel.classList.toggle('is-hidden', relayMetricSamples.length === 0);
        if (!relayMetricSamples.length) {
            return;
        }

        const latest = relayMetricSamples[relayMetricSamples.length - 1];
        const speedText = typeof latest.speed === 'number' ? `${latest.speed.toFixed(2)}x` : 'speed n/a';
        const rateText = typeof latest.send_mbps === 'number'
            ? `${latest.send_mbps.toFixed(2)} Mbps`
            : 'rate warming up';
        const sampleTime = new Date(latest.timestamp * 1000);
        const sampleTimeText = new Intl.DateTimeFormat(undefined, {
            timeStyle: 'medium'
        }).format(sampleTime);
        document.getElementById('relay-metrics-summary').textContent = `${speedText} · ${rateText} · ${sampleTimeText}`;
        drawRelayMetricsChart(relayMetricSamples);
    } catch (error) {
        console.warn('Could not load relay metrics', error);
    }
}

function updateRelayMetricsWindowLabels() {
    const minutes = relayMetricsWindowMinutes;
    document.querySelector('#relay-metrics-window').value = String(minutes);
    document.querySelector('#relay-metrics-window').setAttribute('aria-valuetext', `${minutes} minutes`);
    document.querySelector('#relay-metrics-chart').setAttribute(
        'aria-label',
        `Relay speed and send rate over the last ${minutes} minutes`
    );
}

function initializeRelayMetricsWindow() {
    const input = document.getElementById('relay-metrics-window');
    const savedValue = Number(window.localStorage.getItem(relayMetricsWindowStorageKey));
    if (Number.isInteger(savedValue) && savedValue >= 1 && savedValue <= 1440) {
        relayMetricsWindowMinutes = savedValue;
    }
    updateRelayMetricsWindowLabels();

    input.addEventListener('input', () => {
        const integerText = input.value.replace(/\D/g, '');
        if (input.value !== integerText) {
            input.value = integerText;
        }
        const minutes = Number(integerText);
        if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) {
            return;
        }
        relayMetricsWindowMinutes = minutes;
        window.localStorage.setItem(relayMetricsWindowStorageKey, String(minutes));
        updateRelayMetricsWindowLabels();
        loadRelayMetrics();
    });

    input.addEventListener('blur', updateRelayMetricsWindowLabels);
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            event.stopPropagation();
            input.blur();
        }
    });
}

function setCreateStreamAvailability(enabled, label) {
    const button = document.getElementById('create-stream-button');
    button.disabled = !enabled;
    button.textContent = label;
    button.classList.toggle('is-disabled', !enabled);
}

function renderYouTubeChannels(channels) {
    const container = document.getElementById('youtube-channel-list');
    container.innerHTML = '';

    if (channels.length <= 1) {
        return;
    }

    channels.forEach((channel) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'channel-button';
        button.textContent = `Use ${channel.title}`;
        button.addEventListener('click', () => createYouTubeStream(channel.id));
        container.appendChild(button);
    });
}

async function maybeAutoCreateYouTubeStream() {
    if (youtubeChannels.length !== 1) {
        return false;
    }

    if (window.sessionStorage.getItem(youtubeCreatePendingKey) !== '1') {
        return false;
    }

    await createYouTubeStream(youtubeChannels[0].id);
    return true;
}

function renderYouTubeBroadcastLink(data) {
    const link = document.getElementById('youtube-detail');
    if (data && data.broadcast_url) {
        link.dataset.broadcastUrl = data.broadcast_url;
        link.dataset.broadcastText = `Open "${data.broadcast_title || data.title || 'YouTube broadcast'}"`;
    } else {
        delete link.dataset.broadcastUrl;
        delete link.dataset.broadcastText;
    }
    renderYouTubeDetail();
}

function renderYouTubeActions(data, canStop = false) {
    renderYouTubeBroadcastLink(data);
}

async function loadYouTubeStatus() {
    const response = await fetch('/api/youtube/status');
    if (!response.ok) {
        setYoutubeStatus('YouTube unavailable', 'Could not load YouTube connection status.');
        return;
    }

    const data = await response.json();
    youtubeChannels = Array.isArray(data.channel_choices) ? data.channel_choices : [];
    const oauthConfigured = Boolean(data.oauth_configured);

    if (!oauthConfigured) {
        setCreateStreamAvailability(false, 'Create New Stream Unavailable');
        setYoutubeStatus('Manual YouTube Studio mode', 'This app can publish with a reusable stream key, but browser-created YouTube streams are disabled until Google OAuth is configured.');
        setYouTubeLiveMeta('');
        setYouTubeStreamHealth('');
        renderYouTubeChannels([]);
        renderYouTubeActions(null, false);
        return;
    }

    setCreateStreamAvailability(true, 'Create New Stream');

    if (!data.authorized) {
        setYoutubeStatus('Not connected', data.authorization_error || 'Sign in to YouTube before creating a stream.');
        setYouTubeLiveMeta('');
        setYouTubeStreamHealth('');
        renderYouTubeChannels([]);
        renderYouTubeActions(null, false);
        return;
    }

    if (data.active_destination && data.active_destination.broadcast_url) {
        youtubeChannels = [];
        setYoutubeStatus(
            data.active_destination.channel_title || 'Connected',
            data.active_destination.broadcast_title || 'Current YouTube broadcast is ready.'
        );
        setYouTubeLiveMeta(youtubeLiveMetaText(data.active_destination));
        setYouTubeStreamHealth(youtubeStreamHealthText(data.active_destination));
        renderYouTubeActions(data.active_destination, Boolean(data.can_stop));
    } else if (youtubeChannels.length > 0) {
        if (await maybeAutoCreateYouTubeStream()) {
            renderYouTubeChannels([]);
            return;
        }

        if (youtubeChannels.length === 1) {
            setYoutubeStatus('YouTube ready', 'Create a new stream when you are ready to go live.');
        } else {
            setYoutubeStatus('Choose a channel', 'Select which YouTube channel should receive the new stream.');
        }
        setYouTubeLiveMeta('');
        setYouTubeStreamHealth('');
        renderYouTubeActions(null, false);
    } else {
        setYoutubeStatus('Connected', 'No YouTube channels were returned for this login.');
        setYouTubeLiveMeta('');
        setYouTubeStreamHealth('');
        renderYouTubeActions(null, false);
    }

    renderYouTubeChannels(youtubeChannels);
}

function openYouTubeOAuthPopup() {
    const width = 640;
    const height = 720;
    const left = Math.max(0, Math.round((window.screen.width - width) / 2));
    const top = Math.max(0, Math.round((window.screen.height - height) / 2));
    window.open(
        '/api/youtube/oauth/start',
        'youtube-oauth',
        `width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`
    );
}

async function handleCreateStreamClick() {
    const button = document.getElementById('create-stream-button');
    if (button.disabled) {
        return;
    }

    if (youtubeChannels.length === 1) {
        await createYouTubeStream(youtubeChannels[0].id);
        return;
    }

    if (youtubeChannels.length > 0) {
        setYoutubeStatus('Choose a channel', 'Select which YouTube channel should receive the new stream.');
        return;
    }

    setYoutubeStatus('Connecting to YouTube', 'Complete the Google sign-in flow, then pick a channel here.');
    window.sessionStorage.setItem(youtubeCreatePendingKey, '1');
    openYouTubeOAuthPopup();
}

async function createYouTubeStream(channelId) {
    setYoutubeStatus('Creating stream', 'Asking YouTube to create a brand-new immediate live broadcast.');
    const formData = currentFormData();
    await postState(formData);
    const response = await fetch('/api/youtube/create-stream', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            channel_id: channelId,
            left_team: formData.left_team,
            right_team: formData.right_team
        })
    });

    const data = await response.json();
    if (!response.ok) {
        setYoutubeStatus('Create failed', data.error || 'YouTube did not accept the request.');
        return;
    }

    window.sessionStorage.removeItem(youtubeCreatePendingKey);
    youtubeChannels = [];
    renderYouTubeChannels([]);
    setYoutubeStatus(data.channel_title || 'YouTube ready', data.title || 'Broadcast created.');
    renderYouTubeActions(data, true);
    await loadYouTubeStatus();
}
function meterWidthFromDb(audioDb) {
    if (typeof audioDb !== 'number' || Number.isNaN(audioDb)) {
        return 0;
    }

    const clamped = Math.max(-80, Math.min(-10, audioDb));
    return ((clamped + 80) / 70) * 100;
}

function teamDisplay(teamName, score, ppEnabled, enEnabled, isRightSide) {
    const safeTeamName = teamName || (isRightSide ? 'Right' : 'Left');
    const safeScore = score || '0';
    const flags = [];
    if (ppEnabled) {
        flags.push('PP');
    }
    if (enEnabled) {
        flags.push('EN');
    }

    if (isRightSide) {
        return `${flags.length ? `${flags.join(' ')} ` : ''}${safeTeamName}: ${safeScore}`;
    }

    return `${safeTeamName}: ${safeScore}${flags.length ? ` ${flags.join(' ')}` : ''}`;
}

function teamHeadingDisplay(teamName, fallbackName) {
    const safeTeamName = String(teamName || '').trim() || fallbackName;
    return `${safeTeamName}:`;
}

function renderOverlayMock(state) {
    const isClockIrrelevant = !state.phase_has_duration;
    const timeText = isClockIrrelevant ? state.phase : state.time;
    const phaseText = isClockIrrelevant ? '' : state.phase;
    const timeElement = document.getElementById('overlay-time-text');
    const phaseElement = document.getElementById('overlay-phase-text');
    document.getElementById('overlay-left-text').textContent = teamDisplay(
        state.left_team,
        state.left_score,
        state.left_pp,
        state.left_en,
        false
    );
    document.getElementById('overlay-right-text').textContent = teamDisplay(
        state.right_team,
        state.right_score,
        state.right_pp,
        state.right_en,
        true
    );
    timeElement.textContent = timeText;
    timeElement.classList.toggle('is-hidden', !timeText);
    phaseElement.textContent = phaseText;
    phaseElement.classList.toggle('is-hidden', !phaseText);
    document.getElementById('overlay-mute-icon').classList.toggle('is-hidden', !state.mute);
}

function currentFormData() {
    const selectedClockMode = document.querySelector('input[name="clock-mode"]:checked');
    return {
        left_team: document.getElementById('left-team').value,
        left_score: document.getElementById('left-score').value,
        left_pp: document.getElementById('left-pp').checked,
        left_pp_time: normalizeClockValue(document.getElementById('left-pp-time').value),
        left_en: document.getElementById('left-en').checked,
        right_team: document.getElementById('right-team').value,
        right_score: document.getElementById('right-score').value,
        right_pp: document.getElementById('right-pp').checked,
        right_pp_time: normalizeClockValue(document.getElementById('right-pp-time').value),
        right_en: document.getElementById('right-en').checked,
        clock_mode: selectedClockMode ? selectedClockMode.value : 'stop_time',
        phase: document.getElementById('phase').value,
        time: normalizeClockValue(document.getElementById('time').value),
        mute_on_stop: document.getElementById('mute-on-stop').checked
    };
}

function normalizeClockValue(value) {
    const text = String(value || '').trim();
    if (!text) {
        return '';
    }

    const [minutesText, secondsText = '00'] = text.includes(':') ? text.split(':', 2) : [text, '00'];
    const minutes = Number.parseInt(minutesText || '0', 10);
    const seconds = Number.parseInt(secondsText || '0', 10);
    if (
        Number.isNaN(minutes) ||
        Number.isNaN(seconds) ||
        minutes < 0 ||
        seconds < 0 ||
        seconds > 59
    ) {
        return text;
    }

    return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function normalizeClockInput() {
    const timeField = document.getElementById('time');
    timeField.value = normalizeClockValue(timeField.value);
}

function syncInputValue(id, value) {
    const element = document.getElementById(id);
    if (document.activeElement === element) {
        return;
    }

    element.value = value;
}

function renderState(state) {
    latestState = state;
    updatePreviewAudioWarning(state);
    syncInputValue('left-team', state.left_team);
    syncInputValue('left-score', state.left_score);
    document.getElementById('left-pp').checked = Boolean(state.left_pp);
    syncInputValue('left-pp-time', state.left_pp_time || '');
    document.getElementById('left-en').checked = Boolean(state.left_en);
    syncInputValue('right-team', state.right_team);
    syncInputValue('right-score', state.right_score);
    document.getElementById('right-pp').checked = Boolean(state.right_pp);
    syncInputValue('right-pp-time', state.right_pp_time || '');
    document.getElementById('right-en').checked = Boolean(state.right_en);
    const selectedClockMode = document.querySelector(`input[name="clock-mode"][value="${state.clock_mode || 'stop_time'}"]`);
    if (selectedClockMode) {
        selectedClockMode.checked = true;
    }
    if (document.activeElement !== document.getElementById('phase')) {
        document.getElementById('phase').value = state.phase;
    }
    syncInputValue('time', state.time);
    document.getElementById('left-team-heading').textContent = teamHeadingDisplay(
        document.getElementById('left-team').value,
        'Left'
    );
    document.getElementById('right-team-heading').textContent = teamHeadingDisplay(
        document.getElementById('right-team').value,
        'Right'
    );
    renderOverlayMock(state);

    const toggleButton = document.getElementById('start-stop-button');
    const muteOnStop = document.getElementById('mute-on-stop');
    const muteToggleButton = document.getElementById('mute-toggle-button');
    const clockToggleButton = document.getElementById('clock-toggle-button');
    const clockMode = state.clock_mode || 'stop_time';
    const clockRunning = Boolean(state.clock_running);
    const isMuted = Boolean(state.mute);
    const isUntimedPhase = !state.phase_has_duration;
    const announcementButton = document.getElementById('announcement-button');

    muteOnStop.checked = Boolean(state.mute_on_stop);
    document.getElementById('game-time-button').textContent = state.time || '--:--';
    announcementButton.textContent = isMuted ? '🔇' : '📣';
    announcementButton.setAttribute('aria-label', isMuted ? 'Outgoing audio muted' : 'Outgoing audio active');
    announcementButton.title = announcementButton.getAttribute('aria-label');
    clockToggleButton.textContent = clockRunning ? 'Stop' : 'Start';
    clockToggleButton.classList.toggle('stopped', !clockRunning);
    clockToggleButton.classList.toggle('is-hidden', clockMode !== 'run_time');

    if (clockMode === 'stop_time' && !isUntimedPhase) {
        toggleButton.classList.remove('is-hidden');
        toggleButton.textContent = clockRunning ? 'Stop' : 'Start';
        toggleButton.classList.toggle('stopped', !clockRunning);
    } else if (isUntimedPhase || state.mute_on_stop) {
        toggleButton.classList.remove('is-hidden');
        toggleButton.textContent = isMuted ? 'Un-mute' : 'Mute';
        toggleButton.classList.toggle('stopped', !isMuted);
    } else {
        toggleButton.classList.remove('is-hidden');
        toggleButton.textContent = clockRunning ? 'Stop' : 'Start';
        toggleButton.classList.toggle('stopped', !clockRunning);
    }

    updatePrimaryActionKeypressIndicator();

    muteToggleButton.textContent = isMuted ? 'Un-mute' : 'Mute';
    muteToggleButton.classList.toggle('is-hidden', Boolean(state.mute_on_stop));
}

async function postState(data) {
    const response = await fetch('/api/state', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    });

    if (!response.ok) {
        console.error('State update failed', response.status);
    }
}

async function submitOverlayUpdate(event) {
    if (event) {
        event.preventDefault();
    }
    await postState(currentFormData());
}

async function submitTeamNameUpdate() {
    await postState(currentFormData());
    await loadYouTubeStatus();
}

async function togglePrimaryAction() {
    const clockMode = latestState.clock_mode || 'stop_time';
    const isUntimedPhase = !latestState.phase_has_duration;
    if (clockMode === 'stop_time' && !isUntimedPhase) {
        await postState({
            ...currentFormData(),
            clock_running: !Boolean(latestState.clock_running)
        });
        return;
    }

    if (isUntimedPhase || latestState.mute_on_stop) {
        await postState({
            ...currentFormData(),
            mute: !Boolean(latestState.mute)
        });
        return;
    }

    await toggleClockRunning();
}

async function toggleClockRunning() {
    await postState({
        ...currentFormData(),
        clock_running: !Boolean(latestState.clock_running)
    });
}

async function incrementScore(scoreFieldId) {
    const scoreField = document.getElementById(scoreFieldId);
    const currentValue = Number.parseInt(scoreField.value, 10);
    scoreField.value = Number.isNaN(currentValue) ? '1' : String(currentValue + 1);
    await postState(currentFormData());
}

async function adjustClockTime(secondsDelta) {
    const timeField = document.getElementById('time');
    const match = /^(\d+):([0-5]\d)$/.exec(normalizeClockValue(timeField.value));
    if (!match) {
        normalizeClockInput();
        await postState({
            ...currentFormData(),
            clock_running: Boolean(latestState.clock_running)
        });
        return;
    }

    const currentSeconds = (Number.parseInt(match[1], 10) * 60) + Number.parseInt(match[2], 10);
    const adjustedSeconds = Math.max(0, currentSeconds + secondsDelta);
    const actualSecondsDelta = adjustedSeconds - currentSeconds;
    const minutes = Math.floor(adjustedSeconds / 60);
    const seconds = adjustedSeconds % 60;
    timeField.value = `${minutes}:${String(seconds).padStart(2, '0')}`;
    await postState({
        ...currentFormData(),
        clock_adjustment_seconds: actualSecondsDelta,
        clock_running: Boolean(latestState.clock_running)
    });
}

async function toggleMute() {
    await postState({
        ...currentFormData(),
        mute: !Boolean(latestState.mute)
    });
}

let lastPrimaryActionKeypressAt = null;

function shouldIgnorePrimaryActionKeypress() {
    const now = Date.now();
    if (lastPrimaryActionKeypressAt !== null && now - lastPrimaryActionKeypressAt < toggleBufferMs) {
        return true;
    }

    lastPrimaryActionKeypressAt = now;
    return false;
}

function canHandleGlobalKeypress(event) {
    if (event.metaKey || event.ctrlKey || event.altKey) {
        return false;
    }

    return canHandlePrimaryAction();
}

function canHandlePrimaryAction() {
    const activeTag = document.activeElement ? document.activeElement.tagName : '';
    const isEditable = activeTag === 'INPUT' || activeTag === 'TEXTAREA' || activeTag === 'SELECT';
    if (isEditable) {
        return false;
    }

    return !document.getElementById('start-stop-button').classList.contains('is-hidden');
}

function updatePrimaryActionKeypressIndicator(event) {
    const toggleButton = document.getElementById('start-stop-button');
    toggleButton.classList.toggle('keypress-ready', canHandleGlobalKeypress(event || window.event || {}));
}

async function handleGlobalKeypress(event) {
    if (['Shift', 'Meta', 'Tab'].includes(event.key)) {
        return;
    }

    if (['Control', 'Alt'].includes(event.key)) {
        if (!event.repeat && canHandlePrimaryAction()) {
            event.preventDefault();
            if (shouldIgnorePrimaryActionKeypress()) {
                return;
            }
            await togglePrimaryAction();
        }
        return;
    }

    const timeAdjustmentButtonId = {
        '+': 'time-increment',
        '=': 'time-increment',
        '-': 'time-decrement',
        '_': 'time-decrement'
    }[event.key];

    if (timeAdjustmentButtonId && !event.metaKey && !event.ctrlKey && !event.altKey) {
        const activeTag = document.activeElement ? document.activeElement.tagName : '';
        const isEditable = activeTag === 'INPUT' || activeTag === 'TEXTAREA' || activeTag === 'SELECT';
        if (!isEditable) {
            event.preventDefault();
            document.getElementById(timeAdjustmentButtonId).click();
            return;
        }
    }

    if (!canHandleGlobalKeypress(event)) {
        updatePrimaryActionKeypressIndicator(event);
        return;
    }

    event.preventDefault();
    if (shouldIgnorePrimaryActionKeypress()) {
        return;
    }
    await togglePrimaryAction();
}

async function loadInitialState() {
    const response = await fetch('/api/state');
    if (!response.ok) {
        console.error('Failed to load initial state', response.status);
        return;
    }

    renderState(await response.json());
}

function refreshTeamHeadingsFromInputs() {
    document.getElementById('left-team-heading').textContent = teamHeadingDisplay(
        document.getElementById('left-team').value,
        'Left'
    );
    document.getElementById('right-team-heading').textContent = teamHeadingDisplay(
        document.getElementById('right-team').value,
        'Right'
    );
}

async function commitTimeInput(event) {
    normalizeClockInput();
    await submitOverlayUpdate(event);
}

async function handleTimeInputKeydown(event) {
    if (event.key === 'Enter') {
        event.preventDefault();
        event.stopPropagation();
        event.target.blur();
        return;
    }

    if (event.key === 'Escape' || event.key === 'Esc') {
        event.preventDefault();
        event.stopPropagation();
        event.target.value = latestState.time || '';
        event.target.blur();
    }
}

function handleTabFocus(event) {
    if (event.key !== 'Tab') {
        return;
    }

    const target = event.target;
    if (!target.matches('input[type="checkbox"], input[type="text"], textarea')) {
        return;
    }

    event.preventDefault();
    target.blur();
}

document.getElementById('overlay-form').addEventListener('submit', submitOverlayUpdate);
document.getElementById('start-stop-button').addEventListener('click', togglePrimaryAction);
document.getElementById('clock-toggle-button').addEventListener('click', toggleClockRunning);
document.getElementById('left-plus').addEventListener('click', () => incrementScore('left-score'));
document.getElementById('right-plus').addEventListener('click', () => incrementScore('right-score'));
document.querySelectorAll('.time-adjust-button').forEach((button) => {
    button.addEventListener('mousedown', (event) => event.preventDefault());
});
document.getElementById('time-decrement').addEventListener('click', () => adjustClockTime(-1));
document.getElementById('time-increment').addEventListener('click', () => adjustClockTime(1));
document.getElementById('left-pp').addEventListener('change', submitOverlayUpdate);
document.getElementById('left-en').addEventListener('change', submitOverlayUpdate);
document.getElementById('right-pp').addEventListener('change', submitOverlayUpdate);
document.getElementById('right-en').addEventListener('change', submitOverlayUpdate);
document.querySelectorAll('input[name="clock-mode"]').forEach((radio) => {
    radio.addEventListener('change', submitOverlayUpdate);
    radio.addEventListener('change', () => radio.blur());
});
document.getElementById('left-team').addEventListener('blur', submitTeamNameUpdate);
document.getElementById('right-team').addEventListener('blur', submitTeamNameUpdate);
document.getElementById('left-score').addEventListener('blur', submitOverlayUpdate);
document.getElementById('right-score').addEventListener('blur', submitOverlayUpdate);
document.getElementById('phase').addEventListener('change', submitOverlayUpdate);
document.getElementById('time').addEventListener('blur', commitTimeInput);
document.getElementById('time').addEventListener('keydown', handleTimeInputKeydown);
document.querySelectorAll('select').forEach((select) => {
    select.addEventListener('change', () => select.blur());
});
document.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
    checkbox.addEventListener('change', () => checkbox.blur());
});
document.querySelectorAll('.pp-time-input').forEach((input) => {
    input.addEventListener('blur', async (event) => {
        event.target.value = normalizeClockValue(event.target.value);
        await submitOverlayUpdate();
    });
});
document.getElementById('left-score').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('right-score').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('left-pp').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('left-en').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('right-pp').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('right-en').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('phase').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('time').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('mute-on-stop').addEventListener('change', submitOverlayUpdate);
document.getElementById('mute-toggle-button').addEventListener('click', toggleMute);
document.getElementById('create-stream-button').addEventListener('click', handleCreateStreamClick);
document.getElementById('left-team').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('right-team').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('show-video').addEventListener('change', (event) => setPreviewVisibility(event.target.checked));
document.getElementById('preview-advanced-button').addEventListener('click', () => {
    document.getElementById('preview-settings-dialog').showModal();
});
document.getElementById('preview-dialog-close').addEventListener('click', () => {
    document.getElementById('preview-settings-dialog').close();
});
document.querySelectorAll('input[name="preview-mode"]').forEach((radio) => {
    radio.addEventListener('change', handlePreviewModeChange);
});
document.addEventListener('keydown', handleTabFocus);
document.addEventListener('keydown', handleGlobalKeypress);
document.addEventListener('focusin', updatePrimaryActionKeypressIndicator);
document.addEventListener('focusout', updatePrimaryActionKeypressIndicator);
document.addEventListener('keyup', updatePrimaryActionKeypressIndicator);
window.addEventListener('blur', updatePrimaryActionKeypressIndicator);
updatePrimaryActionKeypressIndicator();
window.addEventListener('message', (event) => {
    if (event.origin !== window.location.origin) {
        return;
    }
    if (event.data && event.data.type === 'youtube-oauth-complete') {
        window.sessionStorage.setItem(youtubeCreatePendingKey, '1');
        loadYouTubeStatus();
    }
});

socket.on('state_updated', renderState);

window.addEventListener('resize', () => drawRelayMetricsChart(relayMetricSamples));
initializePreviewPlayer();
initializeRelayMetricsWindow();
loadInitialState();
loadYouTubeStatus();
loadRelayMetrics();
window.setInterval(loadYouTubeStatus, youtubeStatusPollIntervalMs);
window.setInterval(loadRelayMetrics, 5000);
