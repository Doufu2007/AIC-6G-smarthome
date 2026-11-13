import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


PANEL = Path(__file__).resolve().parents[1] / 'web_panel.html'


class HintParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hints = []
        self._active = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag != 'span':
            return
        onclick = dict(attrs).get('onclick', '')
        match = re.fullmatch(r"sendCmd\('([^']*)'\)", onclick)
        if match:
            self._active = match.group(1)
            self._parts = []

    def handle_data(self, data):
        if self._active is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'span' and self._active is not None:
            self.hints.append((self._active, ''.join(self._parts).strip()))
            self._active = None
            self._parts = []


class DemoPanelContractTests(unittest.TestCase):
    def test_quick_hints_match_rules_demo_capabilities(self):
        parser = HintParser()
        parser.feed(PANEL.read_text(encoding='utf-8'))
        self.assertEqual(
            {command for command, _ in parser.hints},
            {
                '将客厅灯光调到60%',
                '查看客厅的温度和湿度',
                '太暗了',
                '关闭燃气报警器',
            },
        )

    def test_initial_and_empty_states_do_not_claim_a_request_is_waiting(self):
        html = PANEL.read_text(encoding='utf-8')
        self.assertIn('<div class="log-step">演示说明</div>', html)
        self.assertNotIn('<div class="log-step">等待网关确认</div>', html)
        self.assertIn('当前没有待确认请求', html)

    def test_recovered_request_explains_that_new_command_was_not_sent(self):
        html = PANEL.read_text(encoding='utf-8')
        self.assertIn('当前输入暂不发送', html)
        self.assertIn('当前输入尚未发送', html)
        self.assertIn('请再次点击“发送”', html)


if __name__ == '__main__':
    unittest.main()
