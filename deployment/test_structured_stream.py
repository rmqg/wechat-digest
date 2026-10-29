import io
import json
import unittest

from structured import CompletionReadError, parse_object, read_completion


class EventResponse(io.BytesIO):
    headers = {'Content-Type': 'text/event-stream; charset=utf-8'}


def event(content=None, finish=None):
    delta = {} if content is None else {'content': content}
    return ('data: ' + json.dumps({'model': 'served', 'choices': [{'delta': delta, 'finish_reason': finish}]}, ensure_ascii=False) + '\n\n').encode()


class CompletionStreamTests(unittest.TestCase):
    def test_split_escaped_json_and_unicode_survive_heartbeat_events(self):
        text = json.dumps({'description': '带有“引号”和换行\n的图', 'facts': ['一条事实']}, ensure_ascii=False)
        data = b': keepalive\n\n' + event(text[:17]) + event(text[17:31]) + event(text[31:]) + event(finish='stop') + b'data: [DONE]\n\n'
        result = read_completion(EventResponse(data))
        self.assertEqual(parse_object(result['choices'][0]['message']['content']), {'description': '带有“引号”和换行\n的图', 'facts': ['一条事实']})

    def test_parseable_json_without_completion_signal_is_not_success(self):
        with self.assertRaises(CompletionReadError) as raised:
            read_completion(EventResponse(event('{"description":"partial"}') + b'data: [DONE]\n\n'))
        self.assertEqual(raised.exception.category, 'stream_missing_finish')

    def test_stream_transport_failure_preserves_partial_evidence_but_never_returns_it(self):
        class Broken(EventResponse):
            def readline(self, size=-1):
                result = super().readline(size)
                if not result:
                    raise OSError('connection lost')
                return result
        with self.assertRaises(CompletionReadError) as raised:
            read_completion(Broken(event('{"description":')))
        self.assertEqual(raised.exception.category, 'stream_transport')
        self.assertEqual(raised.exception.response['choices'][0]['message']['content'], '{"description":')

    def test_invalid_json_envelope_has_a_distinct_diagnostic_category(self):
        with self.assertRaises(CompletionReadError) as raised:
            read_completion(io.BytesIO(b'<html>upstream failed</html>'))
        self.assertEqual(raised.exception.category, 'malformed_response')


if __name__ == '__main__':
    unittest.main()
