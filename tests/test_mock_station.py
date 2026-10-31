"""Tests for tests/mock_station.py.

These prove the mock is faithful to the real station's dangerous behaviour, so
that apply can later be trusted against it. No network beyond loopback.
"""

import base64
import io
import json
import unittest
import urllib.error
import urllib.request
import zipfile

import sys
from pathlib import Path

# Both invocation styles must work: `discover -s tests` puts this directory on
# sys.path, a dotted `unittest tests.test_smoke` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_station


def call(server, path, method="GET", body=None, auth=True):
    """Returns (status, payload). payload is bytes for non-JSON responses."""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(server.url + path, method=method, data=data)
    request.add_header("Content-Type", "application/json")
    if auth:
        user, password = server.admin
        token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        request.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            if response.headers.get("Content-Type", "").startswith("application/json"):
                return response.status, json.loads(raw)
            return response.status, raw
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, raw
        finally:
            exc.close()


class MockTestCase(unittest.TestCase):
    max_shows = 12

    def setUp(self):
        self.server = mock_station.start_mock(max_shows=self.max_shows)
        self.addCleanup(self.server.stop)
        self.station = self.server.station


class ReadTests(MockTestCase):
    def test_health_is_public(self):
        status, payload = call(self.server, "/api/health", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "on-air")

    def test_now_playing_is_public(self):
        status, payload = call(self.server, "/api/now-playing", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("activeShow", payload)

    def test_schedule_trims_shows_to_seven_fields(self):
        status, payload = call(self.server, "/api/schedule", auth=False)
        self.assertEqual(status, 200)
        show = payload["shows"][0]
        self.assertEqual(set(show), set(mock_station.PUBLIC_SHOW_FIELDS))
        self.assertEqual(len(show), 7)
        self.assertNotIn("genres", show)
        self.assertNotIn("playlistIds", show)
        # the grid itself is fully readable
        self.assertEqual(sum(len(day) for day in payload["schedule"].values()), 168)

    def test_state_exposes_station_identity(self):
        status, payload = call(self.server, "/api/state", auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(payload["station"]["name"])
        self.assertIn("timezone", payload)

    def test_personas_are_trimmed_to_four_fields(self):
        status, payload = call(self.server, "/api/personas", auth=False)
        self.assertEqual(status, 200)
        persona = payload["personas"][0]
        self.assertEqual(set(persona), {"id", "name", "tagline", "avatar"})
        self.assertNotIn("soul", persona)

    def test_settings_requires_auth(self):
        status, _ = call(self.server, "/api/settings", auth=False)
        self.assertEqual(status, 401)

    def test_settings_is_a_summary_not_the_document(self):
        status, payload = call(self.server, "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), set(mock_station.SETTINGS_SUMMARY_KEYS))
        self.assertNotIn("personas", payload)
        self.assertNotIn("stream", payload)

    def test_backup_omits_the_schedule(self):
        status, raw = call(self.server, "/api/backup/export")
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            self.assertIn("settings.json", names)
            self.assertFalse([n for n in names if "schedule" in n.lower()],
                             "the mock must reproduce the missing schedule")
            settings = json.loads(archive.read("settings.json"))
            self.assertIn("personas", settings)

    def test_backup_requires_auth(self):
        status, _ = call(self.server, "/api/backup/export", auth=False)
        self.assertEqual(status, 401)


class SettingsMergeTests(MockTestCase):
    def test_merge_is_shallow_and_replaces_a_top_level_key_wholesale(self):
        before = json.loads(json.dumps(self.station.settings))
        status, _ = call(self.server, "/api/settings", "POST",
                         {"stream": {"maxListeners": 3}})
        self.assertEqual(status, 200)
        self.assertEqual(self.station.settings["stream"], {"maxListeners": 3})
        self.assertNotIn("bitrate", self.station.settings["stream"],
                         "a shallow merge must drop keys not sent")
        # other top-level sections are untouched
        self.assertEqual(self.station.settings["requests"], before["requests"])

    def test_merge_of_personas_replaces_the_whole_array(self):
        status, _ = call(self.server, "/api/settings", "POST",
                         {"personas": [{"id": "p_only", "name": "Only"}]})
        self.assertEqual(status, 200)
        self.assertEqual(self.station.settings["personas"],
                         [{"id": "p_only", "name": "Only"}])

    def test_merge_requires_auth(self):
        status, _ = call(self.server, "/api/settings", "POST", {"stream": {}}, auth=False)
        self.assertEqual(status, 401)


class ShowUpsertTests(MockTestCase):
    def test_show_without_id_gets_one(self):
        status, payload = call(self.server, "/api/shows", "POST",
                               {"show": {"name": "Brand New", "personaId": "p_x"}})
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        self.assertTrue(payload["show"]["id"].startswith("s_"))
        self.assertEqual(len(self.station.schedule["shows"]), 3)

    def test_show_with_existing_id_updates_in_place(self):
        existing = self.station.schedule["shows"][0]
        status, payload = call(self.server, "/api/shows", "POST",
                               {"show": {"id": existing["id"], "name": "Renamed"}})
        self.assertEqual(status, 200)
        self.assertFalse(payload["created"])
        self.assertEqual(len(self.station.schedule["shows"]), 2)
        self.assertEqual(payload["show"]["name"], "Renamed")

    def test_show_cap_returns_409(self):
        status, payload = call(self.server, "/api/shows", "POST", {"show": {"name": "Over"}})
        self.assertEqual(status, 200)  # 2 -> 3 is within the cap
        for index in range(3, self.max_shows):
            status, _ = call(self.server, "/api/shows", "POST", {"show": {"name": "S%d" % index}})
            self.assertEqual(status, 200)
        status, payload = call(self.server, "/api/shows", "POST", {"show": {"name": "Too many"}})
        self.assertEqual(status, 409)
        self.assertIn("limit", payload)

    def test_show_id_with_a_dash_is_rejected(self):
        status, payload = call(self.server, "/api/shows", "POST",
                               {"show": {"id": "s_bad-id", "name": "Bad"}})
        self.assertEqual(status, 400, "the station's ID_RE forbids dashes")
        self.assertIn("invalid show id", payload["error"])

    def test_persona_id_with_a_dash_is_rejected(self):
        status, payload = call(self.server, "/api/settings", "POST",
                               {"personas": [{"id": "p_bad-id", "name": "Bad"}]})
        self.assertEqual(status, 400)
        self.assertIn("invalid persona id", payload["error"])

    def test_valid_ids_are_accepted(self):
        status, _ = call(self.server, "/api/shows", "POST",
                         {"show": {"id": "s_ok_123", "name": "Fine"}})
        self.assertEqual(status, 200)

    def test_shows_require_auth(self):
        status, _ = call(self.server, "/api/shows", "POST", {"show": {"name": "x"}}, auth=False)
        self.assertEqual(status, 401)


class ScheduleReplaceTests(MockTestCase):
    def test_put_replaces_the_whole_grid(self):
        grid = {str(day): ["s_mock01"] * 24 for day in range(7)}
        status, _ = call(self.server, "/api/schedule", "PUT", {"schedule": grid})
        self.assertEqual(status, 200)
        self.assertEqual(self.station.schedule["schedule"], grid)

    def test_grid_must_have_seven_days_of_24(self):
        status, payload = call(self.server, "/api/schedule", "PUT",
                               {"schedule": {"0": ["x"] * 24}})
        self.assertEqual(status, 400)
        self.assertIn("days", payload["error"])

        status, payload = call(self.server, "/api/schedule", "PUT",
                               {"schedule": {str(d): ["x"] * 23 for d in range(7)}})
        self.assertEqual(status, 400)
        self.assertIn("24", payload["error"])

    def test_schedule_requires_auth(self):
        status, _ = call(self.server, "/api/schedule", "PUT",
                         {"schedule": {}}, auth=False)
        self.assertEqual(status, 401)


class RequestLogTests(MockTestCase):
    def test_every_request_is_recorded_with_body_and_auth(self):
        call(self.server, "/api/settings", auth=False)
        call(self.server, "/api/settings", "POST", {"stream": {"maxListeners": 2}})
        call(self.server, "/api/shows", "POST", {"show": {"name": "Logged"}})

        log = self.station.requests
        self.assertEqual([entry["method"] for entry in log], ["GET", "POST", "POST"])
        self.assertFalse(log[0]["authorised"])
        self.assertTrue(log[1]["authorised"])
        self.assertEqual(log[1]["body"], {"stream": {"maxListeners": 2}})
        self.assertEqual(log[2]["body"], {"show": {"name": "Logged"}})

    def test_log_records_the_path_for_each_route(self):
        call(self.server, "/api/schedule")
        call(self.server, "/api/schedule", "PUT",
             {"schedule": {str(d): ["s_mock01"] * 24 for d in range(7)}})
        self.assertEqual([entry["path"] for entry in self.station.requests],
                         ["/api/schedule", "/api/schedule"])


class IsolationTests(unittest.TestCase):
    def test_each_server_gets_its_own_state(self):
        first = mock_station.start_mock()
        self.addCleanup(first.stop)
        second = mock_station.start_mock()
        self.addCleanup(second.stop)
        call(first, "/api/settings", "POST", {"station": "Changed"})
        self.assertEqual(first.station.settings["station"], "Changed")
        self.assertNotEqual(second.station.settings["station"], "Changed")

    def test_seed_can_be_supplied_in_process(self):
        seed = {
            "settings": {"station": "Seeded", "timezone": "UTC", "locale": "en",
                         "personas": [], "skills": {}},
            "schedule": {"shows": [], "schedule": {}, "override": None},
        }
        server = mock_station.start_mock(seed=seed)
        self.addCleanup(server.stop)
        status, payload = call(server, "/api/now-playing")
        self.assertEqual(status, 200)
        self.assertEqual(payload["dj"]["station"], "Seeded")


if __name__ == "__main__":
    unittest.main()
