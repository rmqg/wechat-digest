import copy
import unittest
import urllib.error

import model_fallback


class ProviderFallbackPolicyTests(unittest.TestCase):
    def config(self):
        return {'base_url': 'https://primary.invalid/v1', 'model': 'primary', 'api_key': 'primary-secret',
                'response_format': 'json_schema', 'fallbacks': [
                    {'base_url': 'https://fallback.invalid/v1', 'model': 'backup', 'api_key': 'backup-secret'}]}

    def test_ordered_providers_keep_independent_credentials_without_mutating_input(self):
        config = self.config()
        before = copy.deepcopy(config)
        rows = model_fallback.providers(config)
        self.assertEqual([x['model'] for x in rows], ['primary', 'backup'])
        self.assertEqual([x['api_key'] for x in rows], ['primary-secret', 'backup-secret'])
        self.assertEqual(rows[1]['response_format'], 'json_schema')
        self.assertEqual(config, before)
        self.assertNotIn('fallbacks', rows[0])

    def test_missing_fallback_key_does_not_reuse_the_primary_secret(self):
        config = self.config()
        del config['fallbacks'][0]['api_key']
        with self.assertRaises(ValueError) as result:
            model_fallback.providers(config)
        self.assertNotIn('primary-secret', str(result.exception))

    def test_provider_backoff_changes_on_key_rotation_but_public_identity_does_not(self):
        provider = model_fallback.providers(self.config())[0]
        rotated = {**provider, 'api_key': 'rotated-secret'}
        self.assertNotEqual(model_fallback.provider_key(provider), model_fallback.provider_key(rotated))
        self.assertEqual(model_fallback.identity(provider), model_fallback.identity(rotated))
        self.assertNotIn('api_key', model_fallback.identity(provider))

    def test_only_unavailable_and_transport_failures_can_switch_providers(self):
        for status in (401, 408, 425, 429, 500, 502, 503, 504, 529):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('https://provider.invalid', status, 'upstream', {}, None)
                self.assertTrue(model_fallback.is_fallback_error(error))
        for status in (400, 403, 404, 422):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('https://provider.invalid', status, 'upstream', {}, None)
                self.assertFalse(model_fallback.is_fallback_error(error))
        self.assertTrue(model_fallback.is_fallback_error(urllib.error.URLError('connection unavailable')))
        self.assertTrue(model_fallback.is_fallback_error(TimeoutError()))
        self.assertFalse(model_fallback.is_fallback_error(ValueError('refusal or malformed structured response')))


if __name__ == '__main__':
    unittest.main()
