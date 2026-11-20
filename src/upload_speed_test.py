"""Upload-only internet speed test against a speedtest.net server.

Uses speedtest-cli for the latency check and the upload, but not for the
server list: see fetch_servers(). Framework-independent; app.py adds the
routes.
"""

import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import speedtest

# Length of the upload phase. speedtest.net's default is 10 s; picking a
# server adds ~3 s, so 7 s keeps the whole test to about 10 s.
UPLOAD_SECONDS = 7
# How many of the nearest servers the automatic choice pings.
AUTO_CANDIDATES = 5
# Any ping at or above this means a latency check failed (see _run()).
UNREACHABLE_PING_MS = 60_000
# speedtest-cli's own server list (the legacy speedtest-servers-static.php)
# now returns only a handful of far-away servers, so servers come from the
# JSON API the speedtest.net web page uses. It sorts by distance from the
# caller's GeoIP location, and takes a city/provider search. It returns at
# most 20 servers without a search, and 40 with one.
SERVERS_URL = 'https://www.speedtest.net/api/js/servers'


class UnknownServer(Exception):
    """The server id isn't from an earlier server list."""


class AlreadyRunning(Exception):
    """A speed test is already running."""


def server_label(server):
    return ', '.join(filter(None, (server.get('sponsor'), server.get('name'), server.get('country'))))


def fetch_servers(search=None, limit=20):
    """speedtest.net servers, nearest first, as speedtest-cli server dicts."""
    query = {'engine': 'js', 'limit': limit}
    if search:
        query['search'] = search
    # The API answers 403 to Python's default User-Agent.
    req = urllib.request.Request(
        f'{SERVERS_URL}?{urllib.parse.urlencode(query)}',
        headers={'User-Agent': speedtest.build_user_agent()},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        servers = json.load(response)
    servers = [server for server in servers if server.get('id') and server.get('host')]
    for server in servers:
        # 'url' is on the provider's own hostname, which for most servers
        # now just redirects (307); speedtest-cli doesn't follow that, the
        # test fails, and it counts the unsent bytes as uploaded (hundreds
        # of Mbps). 'host' is Ookla's hostname, which answers directly.
        # Plain http, like speedtest-cli's default: over https each latency
        # check pays for a TLS handshake, which doubles the ping.
        server['url'] = f"http://{server['host']}/speedtest/upload.php"
    return servers


class UploadSpeedTest:
    """One upload test at a time, run in a background thread.

    The test takes about 10 s, so start() returns at once and the caller
    polls status() for the result.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {'running': False}
        # Servers from earlier server lists, by id, so a test can be started
        # on one the UI picked.
        self._known_servers = {}

    def servers(self, search=None):
        """[{'id', 'label', 'distance_km'}], nearest first."""
        servers = fetch_servers(search=search, limit=40 if search else 20)
        with self._lock:
            self._known_servers.update((str(server['id']), server) for server in servers)
        return [
            {'id': str(server['id']), 'label': server_label(server), 'distance_km': server.get('distance')}
            for server in servers
        ]

    def status(self):
        """{'running'}, plus the last result: 'upload_mbps', 'ping_ms' and
        'server', or 'error'; and 'finished_at'."""
        with self._lock:
            return dict(self._state)

    def start(self, server_id=None):
        """Starts a test on server_id (from servers()), or with no server_id
        on the lowest-ping of the nearest few."""
        with self._lock:
            server = self._known_servers.get(server_id) if server_id else None
            if server_id and server is None:
                raise UnknownServer(server_id)
            if self._state.get('running'):
                raise AlreadyRunning()
            self._state = {'running': True}
        threading.Thread(target=self._run, args=(server,), daemon=True).start()

    def _run(self, server):
        # Upload only: speedtest.net's download test is skipped.
        try:
            candidates = [server] if server else fetch_servers(limit=AUTO_CANDIDATES)
            if not candidates:
                raise ValueError('speedtest.net returned no servers')
            tester = speedtest.Speedtest(secure=True)
            tester.config['length']['upload'] = UPLOAD_SECONDS
            best = tester.get_best_server(candidates)
            # A failed latency check counts as 3600 s, so a server that can't
            # be reached shows up as a huge ping. Its upload would fail the
            # same way, and speedtest-cli would report the unsent bytes as a
            # speed.
            if best['latency'] >= UNREACHABLE_PING_MS:
                raise ValueError(f"couldn't reach {server_label(best)}")
            tester.upload(pre_allocate=False)
            results = tester.results
            outcome = {
                'upload_mbps': round(results.upload / 1_000_000, 1),
                'ping_ms': round(results.ping),
                'server': server_label(results.server or {}),
            }
        except Exception as e:
            outcome = {'error': f'Speed test failed: {e}'}
        outcome['running'] = False
        outcome['finished_at'] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._state = outcome
