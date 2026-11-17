"""Check update authorization and the request handoff without executing deployment."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app import updater


class UpdateRequestTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.state = root / 'state.json'
        self.request = root / 'update.request'
        for name, value in [('STATE_FILE', self.state), ('REQUEST_FILE', self.request)]:
            patcher = patch.object(updater, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_requires_configured_admin_token(self):
        with patch.dict(os.environ, {'UPDATE_ADMIN_TOKEN': 'test-only-update-token'}):
            for token in (None, '', 'wrong'):
                with self.assertRaises(HTTPException) as error:
                    updater.verify_admin_token(token)
                self.assertEqual(error.exception.status_code, 403)
            updater.verify_admin_token('test-only-update-token')
        with patch.dict(os.environ, {'UPDATE_ADMIN_TOKEN': ''}):
            with self.assertRaises(HTTPException):
                updater.verify_admin_token('test-only-update-token')

    def test_disabled_deployment_does_not_queue_an_update(self):
        with patch.dict(os.environ, {'UPDATE_ENABLED': 'false'}):
            with self.assertRaises(HTTPException) as error:
                updater.start_update()
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.request.exists())

    def test_enabled_deployment_writes_request_and_checking_state(self):
        with patch.dict(os.environ, {'UPDATE_ENABLED': 'true'}):
            state = updater.start_update()
        self.assertEqual(state['status'], 'checking')
        self.assertTrue(self.request.read_text().strip())
        self.assertEqual(updater.read_state()['status'], 'checking')

    def test_running_update_is_not_overwritten(self):
        with patch.dict(os.environ, {'UPDATE_ENABLED': 'true'}):
            updater.start_update()
            original = self.request.read_text()
            with self.assertRaises(HTTPException) as error:
                updater.start_update()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.request.read_text(), original)

    def test_request_write_failure_is_reported_as_failed(self):
        self.request.mkdir()
        with patch.dict(os.environ, {'UPDATE_ENABLED': 'true'}):
            with self.assertRaises(HTTPException) as error:
                updater.start_update()
        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(updater.read_state()['status'], 'failed')
