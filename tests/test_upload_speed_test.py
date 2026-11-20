import io
import json
import unittest
import urllib.parse
from unittest import mock

from src import upload_speed_test
from src.upload_speed_test import AlreadyRunning, UnknownServer, UploadSpeedTest, fetch_servers, server_label


def api_server(id_, host, **fields):
    # One entry as speedtest.net's JSON server API returns it. 'url' is on
    # the provider's own hostname, which redirects.
    server = {
        'id': id_,
        'host': host,
        'url': f"http://provider-{id_}.example:8080/speedtest/upload.php",
        'sponsor': f'Sponsor {id_}',
        'name': 'Testville, ON',
        'country': 'Canada',
        'distance': 7,
    }
    server.update(fields)
    return server


def fake_urlopen(servers):
    """A urlopen stand-in returning servers as JSON; records each request."""
    requests = []

    def urlopen(request, timeout=None):
        requests.append(request)
        return io.BytesIO(json.dumps(servers).encode())

    return mock.patch.object(upload_speed_test.urllib.request, 'urlopen', side_effect=urlopen), requests


class FakeResults:
    def __init__(self, upload, ping, server):
        self.upload = upload
        self.ping = ping
        self.server = server


class FakeSpeedtest:
    """Stands in for speedtest.Speedtest. The class attributes set the
    outcome and record what the code under test did."""

    latency_ms = 12.3
    upload_bps = 48_765_432.0
    upload_error = None
    instances = []

    def __init__(self, secure=False):
        self.secure = secure
        self.config = {'length': {'upload': 10}}
        self.results = None
        self.pinged = None
        self.uploaded = False
        FakeSpeedtest.instances.append(self)

    def get_best_server(self, servers):
        self.pinged = list(servers)
        best = dict(servers[0], latency=FakeSpeedtest.latency_ms)
        self.results = FakeResults(0, FakeSpeedtest.latency_ms, best)
        return best

    def upload(self, pre_allocate=True):
        if FakeSpeedtest.upload_error:
            raise FakeSpeedtest.upload_error
        self.uploaded = True
        self.results.upload = FakeSpeedtest.upload_bps


class ServerLabelTests(unittest.TestCase):
    def test_joins_sponsor_city_and_country(self):
        server = {'sponsor': 'Testnet', 'name': 'Testville, ON', 'country': 'Canada'}
        self.assertEqual(server_label(server), 'Testnet, Testville, ON, Canada')

    def test_skips_missing_parts(self):
        self.assertEqual(server_label({'sponsor': 'Testnet', 'country': ''}), 'Testnet')
        self.assertEqual(server_label({}), '')


class FetchServersTests(unittest.TestCase):
    def test_upload_url_is_built_from_the_ookla_host(self):
        # The provider's 'url' redirects, which speedtest-cli doesn't follow:
        # the test fails and reports the unsent bytes as hundreds of Mbps.
        patcher, _ = fake_urlopen([api_server('1', 'st1.example.prod.hosts.ooklaserver.net:8080')])
        with patcher:
            servers = fetch_servers()
        self.assertEqual(servers[0]['url'], 'http://st1.example.prod.hosts.ooklaserver.net:8080/speedtest/upload.php')

    def test_drops_servers_without_an_id_or_host(self):
        patcher, _ = fake_urlopen([
            api_server('1', 'one.example:8080'),
            api_server('', 'no-id.example:8080'),
            api_server('3', ''),
            {'id': '4'},
        ])
        with patcher:
            servers = fetch_servers()
        self.assertEqual([server['id'] for server in servers], ['1'])

    def test_request_sends_limit_search_and_speedtest_user_agent(self):
        patcher, requests = fake_urlopen([])
        with patcher:
            fetch_servers(search='Testville', limit=40)
            fetch_servers(limit=5)
        searched, nearest = (urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query) for r in requests)
        self.assertEqual(searched, {'engine': ['js'], 'limit': ['40'], 'search': ['Testville']})
        self.assertEqual(nearest, {'engine': ['js'], 'limit': ['5']})
        # The API answers 403 to Python's default User-Agent.
        self.assertIn('speedtest-cli', requests[0].get_header('User-agent'))


class ServersTests(unittest.TestCase):
    def test_lists_20_nearest_or_40_matches(self):
        tester = UploadSpeedTest()
        with mock.patch.object(upload_speed_test, 'fetch_servers', return_value=[]) as fetch:
            tester.servers()
            tester.servers(search='Testville')
        self.assertEqual(fetch.call_args_list, [
            mock.call(search=None, limit=20),
            mock.call(search='Testville', limit=40),
        ])

    def test_returns_id_label_and_distance(self):
        tester = UploadSpeedTest()
        server = api_server('7', 'seven.example:8080')
        with mock.patch.object(upload_speed_test, 'fetch_servers', return_value=[server]):
            listed = tester.servers()
        self.assertEqual(listed, [{'id': '7', 'label': 'Sponsor 7, Testville, ON, Canada', 'distance_km': 7}])


class StartTests(unittest.TestCase):
    def setUp(self):
        # start() only queues the test: don't run it.
        patcher = mock.patch.object(upload_speed_test.threading, 'Thread')
        self.thread = patcher.start()
        self.addCleanup(patcher.stop)
        self.tester = UploadSpeedTest()

    def test_unknown_server_is_refused(self):
        with self.assertRaises(UnknownServer):
            self.tester.start('99')
        self.assertFalse(self.tester.status()['running'])
        self.thread.assert_not_called()

    def test_listed_server_is_passed_to_the_test(self):
        server = api_server('7', 'seven.example:8080')
        with mock.patch.object(upload_speed_test, 'fetch_servers', return_value=[server]):
            self.tester.servers()
        self.tester.start('7')
        self.assertEqual(self.thread.call_args.kwargs['args'][0]['id'], '7')

    def test_no_server_means_automatic(self):
        self.tester.start()
        self.assertIsNone(self.thread.call_args.kwargs['args'][0])
        self.assertEqual(self.tester.status(), {'running': True})

    def test_second_start_while_running_is_refused(self):
        self.tester.start()
        with self.assertRaises(AlreadyRunning):
            self.tester.start()
        self.assertEqual(self.thread.call_count, 1)


class RunTests(unittest.TestCase):
    def setUp(self):
        FakeSpeedtest.latency_ms = 12.3
        FakeSpeedtest.upload_bps = 48_765_432.0
        FakeSpeedtest.upload_error = None
        FakeSpeedtest.instances = []
        patcher = mock.patch.object(upload_speed_test.speedtest, 'Speedtest', FakeSpeedtest)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tester = UploadSpeedTest()
        self.server = api_server('7', 'seven.example:8080')

    def test_reports_upload_ping_and_server(self):
        self.tester._run(self.server)
        status = self.tester.status()
        self.assertEqual(
            {key: status[key] for key in ('running', 'upload_mbps', 'ping_ms', 'server')},
            {'running': False, 'upload_mbps': 48.8, 'ping_ms': 12, 'server': 'Sponsor 7, Testville, ON, Canada'},
        )
        self.assertIn('finished_at', status)
        self.assertNotIn('error', status)

    def test_upload_phase_is_shortened(self):
        self.tester._run(self.server)
        tester = FakeSpeedtest.instances[0]
        self.assertEqual(tester.config['length']['upload'], upload_speed_test.UPLOAD_SECONDS)
        self.assertTrue(tester.uploaded)

    def test_unreachable_server_is_an_error_not_a_speed(self):
        # speedtest-cli counts a failed latency check as 3600 s; uploading
        # anyway would report the unsent bytes as a speed.
        FakeSpeedtest.latency_ms = 1_800_000
        self.tester._run(self.server)
        status = self.tester.status()
        self.assertIn("couldn't reach Sponsor 7", status['error'])
        self.assertNotIn('upload_mbps', status)
        self.assertFalse(FakeSpeedtest.instances[0].uploaded)

    def test_automatic_pings_the_nearest_few(self):
        nearest = [api_server(str(i), f'{i}.example:8080') for i in range(1, 6)]
        with mock.patch.object(upload_speed_test, 'fetch_servers', return_value=nearest) as fetch:
            self.tester._run(None)
        fetch.assert_called_once_with(limit=upload_speed_test.AUTO_CANDIDATES)
        self.assertEqual(FakeSpeedtest.instances[0].pinged, nearest)

    def test_no_servers_is_an_error(self):
        with mock.patch.object(upload_speed_test, 'fetch_servers', return_value=[]):
            self.tester._run(None)
        self.assertIn('returned no servers', self.tester.status()['error'])

    def test_failure_is_reported_and_a_new_test_can_start(self):
        FakeSpeedtest.upload_error = OSError('connection reset')
        self.tester._run(self.server)
        status = self.tester.status()
        self.assertEqual(status['error'], 'Speed test failed: connection reset')
        self.assertFalse(status['running'])
        with mock.patch.object(upload_speed_test.threading, 'Thread'):
            self.tester.start()
        self.assertTrue(self.tester.status()['running'])


if __name__ == '__main__':
    unittest.main()
