const socket = io();
let youtubeChannels = [];
let latestState = {};
const youtubeCreatePendingKey = 'youtube-create-pending';

function previewUrl() {
    const query = new URLSearchParams({
        controls: 'yes',
        muted: 'yes',
        autoplay: 'yes',
        playsinline: 'yes'
    });
    return `${window.location.protocol}//${window.location.hostname}:8889/live/preview/?${query.toString()}`;
}

function setPreviewVisibility(showVideo) {
    document.getElementById('preview-frame').classList.toggle('is-hidden', !showVideo);
    document.getElementById('overlay-mock').classList.toggle('is-hidden', showVideo);
}

function initializePreviewPlayer() {
    const previewFrame = document.getElementById('preview-frame');
    if (!previewFrame) {
        return;
    }

    previewFrame.src = previewUrl();
    setPreviewVisibility(document.getElementById('show-video').checked);
}

function setYoutubeStatus(title, detail) {
    document.getElementById('youtube-status').textContent = title;
    document.getElementById('youtube-detail').textContent = detail;
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
    const link = document.getElementById('youtube-broadcast-link');
    if (data && data.broadcast_url) {
        link.href = data.broadcast_url;
        link.textContent = `Open "${data.broadcast_title || data.title || 'YouTube broadcast'}"`;
        link.classList.remove('is-hidden');
        return;
    }

    link.classList.add('is-hidden');
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
        renderYouTubeChannels([]);
        renderYouTubeActions(null, false);
        return;
    }

    setCreateStreamAvailability(true, 'Create New Stream');

    if (!data.authorized) {
        setYoutubeStatus('Not connected', data.authorization_error || 'Sign in to YouTube before creating a stream.');
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
        renderYouTubeActions(null, false);
    } else {
        setYoutubeStatus('Connected', 'No YouTube channels were returned for this login.');
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
            home_team: formData.home_team,
            away_team: formData.away_team
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
    renderYouTubeActions(data, true);
    setYoutubeStatus(data.channel_title || 'YouTube ready', data.title || 'Broadcast created.');
}
function meterWidthFromDb(audioDb) {
    if (typeof audioDb !== 'number' || Number.isNaN(audioDb)) {
        return 0;
    }

    const clamped = Math.max(-80, Math.min(-10, audioDb));
    return ((clamped + 80) / 70) * 100;
}

function teamDisplay(teamName, score, ppEnabled, enEnabled, isRightSide) {
    const safeTeamName = teamName || (isRightSide ? 'Right Team' : 'Left Team');
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

function renderOverlayMock(state) {
    document.getElementById('overlay-home-text').textContent = teamDisplay(
        state.home_team,
        state.home_score,
        state.home_pp,
        state.home_en,
        false
    );
    document.getElementById('overlay-away-text').textContent = teamDisplay(
        state.away_team,
        state.away_score,
        state.away_pp,
        state.away_en,
        true
    );
    document.getElementById('overlay-time-text').textContent = state.time;
    document.getElementById('overlay-period-text').textContent = state.period;
    document.getElementById('overlay-mute-icon').classList.toggle('is-hidden', !state.mute);
}

function currentFormData() {
    const selectedClockMode = document.querySelector('input[name="clock-mode"]:checked');
    return {
        home_team: document.getElementById('home-team').value,
        home_score: document.getElementById('home-score').value,
        home_pp: document.getElementById('home-pp').checked,
        home_en: document.getElementById('home-en').checked,
        away_team: document.getElementById('away-team').value,
        away_score: document.getElementById('away-score').value,
        away_pp: document.getElementById('away-pp').checked,
        away_en: document.getElementById('away-en').checked,
        clock_mode: selectedClockMode ? selectedClockMode.value : 'stop_time',
        period: document.getElementById('period').value,
        time: document.getElementById('time').value,
        mute_on_stop: document.getElementById('mute-on-stop').checked
    };
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
    syncInputValue('home-team', state.home_team);
    syncInputValue('home-score', state.home_score);
    document.getElementById('home-pp').checked = Boolean(state.home_pp);
    document.getElementById('home-en').checked = Boolean(state.home_en);
    syncInputValue('away-team', state.away_team);
    syncInputValue('away-score', state.away_score);
    document.getElementById('away-pp').checked = Boolean(state.away_pp);
    document.getElementById('away-en').checked = Boolean(state.away_en);
    const selectedClockMode = document.querySelector(`input[name="clock-mode"][value="${state.clock_mode || 'stop_time'}"]`);
    if (selectedClockMode) {
        selectedClockMode.checked = true;
    }
    if (document.activeElement !== document.getElementById('period')) {
        document.getElementById('period').value = state.period;
    }
    syncInputValue('time', state.time);
    document.getElementById('home-team-heading').textContent = teamDisplay(
        document.getElementById('home-team').value,
        document.getElementById('home-score').value,
        state.home_pp,
        state.home_en,
        false
    );
    document.getElementById('away-team-heading').textContent = teamDisplay(
        document.getElementById('away-team').value,
        document.getElementById('away-score').value,
        state.away_pp,
        state.away_en,
        true
    );
    renderOverlayMock(state);

    const toggleButton = document.getElementById('start-stop-button');
    const incomingAudioLabel = document.getElementById('incoming-audio-label');
    const incomingAudioMeter = document.getElementById('incoming-audio-meter');
    const muteOnStop = document.getElementById('mute-on-stop');
    const muteToggleButton = document.getElementById('mute-toggle-button');
    const clockToggleButton = document.getElementById('clock-toggle-button');
    const clockMode = state.clock_mode || 'stop_time';
    const clockRunning = Boolean(state.clock_running);
    const isMuted = Boolean(state.mute);

    muteOnStop.checked = Boolean(state.mute_on_stop);
    incomingAudioLabel.textContent = state.incoming_audio_label || 'Waiting for stream';
    incomingAudioMeter.style.width = `${meterWidthFromDb(state.incoming_audio_db)}%`;
    incomingAudioMeter.classList.toggle('is-silent', !state.incoming_audio_active);
    clockToggleButton.textContent = clockRunning ? 'Stop' : 'Start';
    clockToggleButton.classList.toggle('stopped', !clockRunning);
    clockToggleButton.classList.toggle('is-hidden', clockMode !== 'run_time');

    if (clockMode === 'stop_time') {
        toggleButton.classList.remove('is-hidden');
        toggleButton.textContent = clockRunning ? 'Stop' : 'Start';
        toggleButton.classList.toggle('stopped', !clockRunning);
    } else if (state.mute_on_stop) {
        toggleButton.classList.remove('is-hidden');
        toggleButton.textContent = isMuted ? 'Un-mute' : 'Mute';
        toggleButton.classList.toggle('stopped', !isMuted);
    } else {
        toggleButton.classList.add('is-hidden');
    }

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
    if (clockMode === 'stop_time') {
        await postState({
            ...currentFormData(),
            clock_running: !Boolean(latestState.clock_running)
        });
        return;
    }

    if (latestState.mute_on_stop) {
        await postState({
            ...currentFormData(),
            mute: !Boolean(latestState.mute)
        });
    }
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

async function toggleMute() {
    await postState({
        ...currentFormData(),
        mute: !Boolean(latestState.mute)
    });
}

async function handleGlobalKeypress(event) {
    if (event.metaKey || event.ctrlKey || event.altKey) {
        return;
    }

    const activeTag = document.activeElement ? document.activeElement.tagName : '';
    const isEditable = activeTag === 'INPUT' || activeTag === 'TEXTAREA' || activeTag === 'SELECT';
    if (isEditable) {
        return;
    }

    if (document.getElementById('start-stop-button').classList.contains('is-hidden')) {
        return;
    }

    event.preventDefault();
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
    document.getElementById('home-team-heading').textContent = teamDisplay(
        document.getElementById('home-team').value,
        document.getElementById('home-score').value,
        document.getElementById('home-pp').checked,
        document.getElementById('home-en').checked,
        false
    );
    document.getElementById('away-team-heading').textContent = teamDisplay(
        document.getElementById('away-team').value,
        document.getElementById('away-score').value,
        document.getElementById('away-pp').checked,
        document.getElementById('away-en').checked,
        true
    );
}

document.getElementById('overlay-form').addEventListener('submit', submitOverlayUpdate);
document.getElementById('start-stop-button').addEventListener('click', togglePrimaryAction);
document.getElementById('clock-toggle-button').addEventListener('click', toggleClockRunning);
document.getElementById('home-plus').addEventListener('click', () => incrementScore('home-score'));
document.getElementById('away-plus').addEventListener('click', () => incrementScore('away-score'));
document.getElementById('home-pp').addEventListener('change', submitOverlayUpdate);
document.getElementById('home-en').addEventListener('change', submitOverlayUpdate);
document.getElementById('away-pp').addEventListener('change', submitOverlayUpdate);
document.getElementById('away-en').addEventListener('change', submitOverlayUpdate);
document.querySelectorAll('input[name="clock-mode"]').forEach((radio) => {
    radio.addEventListener('change', submitOverlayUpdate);
});
document.getElementById('home-team').addEventListener('blur', submitTeamNameUpdate);
document.getElementById('away-team').addEventListener('blur', submitTeamNameUpdate);
document.getElementById('home-score').addEventListener('blur', submitOverlayUpdate);
document.getElementById('away-score').addEventListener('blur', submitOverlayUpdate);
document.getElementById('period').addEventListener('change', submitOverlayUpdate);
document.getElementById('time').addEventListener('blur', submitOverlayUpdate);
document.getElementById('home-score').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('away-score').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('home-pp').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('home-en').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('away-pp').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('away-en').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('period').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('time').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('mute-on-stop').addEventListener('change', submitOverlayUpdate);
document.getElementById('mute-toggle-button').addEventListener('click', toggleMute);
document.getElementById('create-stream-button').addEventListener('click', handleCreateStreamClick);
document.getElementById('home-team').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('away-team').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('show-video').addEventListener('change', (event) => setPreviewVisibility(event.target.checked));
document.addEventListener('keydown', handleGlobalKeypress);
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

initializePreviewPlayer();
loadInitialState();
loadYouTubeStatus();
