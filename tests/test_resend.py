from dataclasses import replace
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

from kapwatch.config import Config
from kapwatch.notify import DeliveryError, Resend, email_sender


class ResendTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(Path('/tmp'), email_enabled=True, email_provider='resend',
            email_from='KAP SmartMail <onboarding@resend.dev>', email_to='owner@example.invalid',
            resend_api_key='re_test_key_not_a_real_secret')

    def test_provider_does_not_require_smtp(self):
        self.config.validate_delivery()
        self.assertIsInstance(email_sender(self.config), Resend)
        with self.assertRaises(ValueError):
            replace(self.config, resend_api_key='').validate_delivery()

    def test_send_uses_authenticated_api_and_stable_idempotency(self):
        requests = []
        def reply(request, **kwargs):
            requests.append(request)
            return io.BytesIO(b'{"id":"accepted-id"}')
        with patch('urllib.request.urlopen', side_effect=reply):
            self.assertEqual(Resend(self.config).send(self.config.email_to, '[P0 KAP]\nKaynak cümle: örnek'), 'accepted-id')
            Resend(self.config).send(self.config.email_to, '[P0 KAP]\nKaynak cümle: örnek')
        self.assertEqual(requests[0].get_header('Idempotency-key'), requests[1].get_header('Idempotency-key'))
        self.assertEqual(json.loads(requests[0].data)['to'], [self.config.email_to])

    def test_different_disclosures_have_different_idempotency(self):
        keys=[]
        def reply(request, **kwargs):
            keys.append(request.get_header('Idempotency-key'))
            return io.BytesIO(b'{"id":"accepted-id"}')
        with patch('urllib.request.urlopen', side_effect=reply):
            for identifier in [10,11]:
                Resend(self.config).send(self.config.email_to, '[P0 KAP]\nBildirim: '+str(identifier))
        self.assertNotEqual(keys[0], keys[1])

    def test_rate_limit_and_error_text_never_leak_key(self):
        error=urllib.error.HTTPError('https://api.resend.com/emails',429,'rate limit',
            {'Retry-After':'120'},io.BytesIO(b'sensitive response'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(DeliveryError) as caught:
                Resend(self.config).send(self.config.email_to,'test')
        self.assertEqual(caught.exception.retry_after,120)
        self.assertNotIn(self.config.resend_api_key,str(caught.exception))
        self.assertNotIn('sensitive',str(caught.exception))

    def test_rejected_own_address_rule_is_not_success(self):
        error=urllib.error.HTTPError('https://api.resend.com/emails',403,'denied',{},io.BytesIO())
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(DeliveryError) as caught:
                Resend(self.config).send(self.config.email_to,'test')
        self.assertEqual(caught.exception.retry_after,3600)

    def test_response_without_id_is_not_success(self):
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"error":"rejected"}')):
            with self.assertRaises(DeliveryError):
                Resend(self.config).send(self.config.email_to,'test')


if __name__=='__main__':
    unittest.main()
