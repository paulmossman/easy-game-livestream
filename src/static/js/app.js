const socket = io();

function previewUrl() {
    const query = new URLSearchParams({
        controls: 'yes',
        muted: 'no',
        autoplay: 'yes',
        playsinline: 'yes'
    });
    return `${window.location.protocol}//${window.location.hostname}:8889/live/preview/?${query.toString()}`;
}

function setPreviewStatus(message) {
    const previewStatus = document.getElementById('preview-status');
    if (previewStatus) {
        previewStatus.textContent = message;
    }
}

function initializePreviewPlayer() {
    const previewFrame = document.getElementById('preview-frame');
    if (!previewFrame) {
        return;
    }

    previewFrame.src = previewUrl();
    setPreviewStatus('Preview ready. If your browser blocks autoplay with sound, click once inside the player.');
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

function currentFormData() {
    return {
        home_team: document.getElementById('home-team').value,
        home_score: document.getElementById('home-score').value,
        home_pp: document.getElementById('home-pp').checked,
        home_en: document.getElementById('home-en').checked,
        away_team: document.getElementById('away-team').value,
        away_score: document.getElementById('away-score').value,
        away_pp: document.getElementById('away-pp').checked,
        away_en: document.getElementById('away-en').checked,
        period: document.getElementById('period').value,
        time: document.getElementById('time').value,
        mute_on_stop: document.getElementById('mute-on-stop').checked
    };
}

function renderState(state) {
    document.getElementById('home-team').value = state.home_team;
    document.getElementById('home-score').value = state.home_score;
    document.getElementById('home-pp').checked = Boolean(state.home_pp);
    document.getElementById('home-en').checked = Boolean(state.home_en);
    document.getElementById('away-team').value = state.away_team;
    document.getElementById('away-score').value = state.away_score;
    document.getElementById('away-pp').checked = Boolean(state.away_pp);
    document.getElementById('away-en').checked = Boolean(state.away_en);
    document.getElementById('period').value = state.period;
    document.getElementById('time').value = state.time;
    document.getElementById('time-display').textContent = state.time;
    document.getElementById('home-team-heading').textContent = teamDisplay(
        state.home_team,
        state.home_score,
        state.home_pp,
        state.home_en,
        false
    );
    document.getElementById('away-team-heading').textContent = teamDisplay(
        state.away_team,
        state.away_score,
        state.away_pp,
        state.away_en,
        true
    );

    const toggleButton = document.getElementById('start-stop-button');
    const statusText = document.getElementById('status-text');
    const audioState = document.getElementById('audio-state');
    const incomingAudioLabel = document.getElementById('incoming-audio-label');
    const incomingAudioMeter = document.getElementById('incoming-audio-meter');
    const muteOnStop = document.getElementById('mute-on-stop');
    const muteToggleButton = document.getElementById('mute-toggle-button');
    const isRunning = Boolean(state.running);

    muteOnStop.checked = Boolean(state.mute_on_stop);
    toggleButton.textContent = isRunning ? 'Stop' : 'Start';
    toggleButton.classList.toggle('stopped', !isRunning);
    statusText.textContent = isRunning ? 'Running' : 'Stopped';
    audioState.textContent = state.mute ? 'Muted' : 'Un-muted';
    incomingAudioLabel.textContent = state.incoming_audio_label || 'Waiting for stream';
    incomingAudioMeter.style.width = `${meterWidthFromDb(state.incoming_audio_db)}%`;
    incomingAudioMeter.classList.toggle('is-silent', !state.incoming_audio_active);
    muteToggleButton.textContent = state.mute ? 'Un-mute' : 'Mute';
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
    event.preventDefault();
    await postState(currentFormData());
}

async function toggleRunning() {
    const isRunning = document.getElementById('start-stop-button').textContent === 'Stop';
    await postState({
        ...currentFormData(),
        running: !isRunning
    });
}

async function incrementScore(scoreFieldId) {
    const scoreField = document.getElementById(scoreFieldId);
    const currentValue = Number.parseInt(scoreField.value, 10);
    scoreField.value = Number.isNaN(currentValue) ? '1' : String(currentValue + 1);
    await postState(currentFormData());
}

async function toggleMute() {
    const isMuted = document.getElementById('audio-state').textContent === 'Muted';
    await postState({
        ...currentFormData(),
        mute: !isMuted
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

    event.preventDefault();
    await toggleRunning();
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
document.getElementById('start-stop-button').addEventListener('click', toggleRunning);
document.getElementById('home-plus').addEventListener('click', () => incrementScore('home-score'));
document.getElementById('away-plus').addEventListener('click', () => incrementScore('away-score'));
document.getElementById('home-pp').addEventListener('change', submitOverlayUpdate);
document.getElementById('home-en').addEventListener('change', submitOverlayUpdate);
document.getElementById('away-pp').addEventListener('change', submitOverlayUpdate);
document.getElementById('away-en').addEventListener('change', submitOverlayUpdate);
document.getElementById('home-score').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('away-score').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('home-pp').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('home-en').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('away-pp').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('away-en').addEventListener('change', refreshTeamHeadingsFromInputs);
document.getElementById('mute-on-stop').addEventListener('change', submitOverlayUpdate);
document.getElementById('mute-toggle-button').addEventListener('click', toggleMute);
document.getElementById('home-team').addEventListener('input', refreshTeamHeadingsFromInputs);
document.getElementById('away-team').addEventListener('input', refreshTeamHeadingsFromInputs);
document.addEventListener('keydown', handleGlobalKeypress);

socket.on('state_updated', renderState);

initializePreviewPlayer();
loadInitialState();
