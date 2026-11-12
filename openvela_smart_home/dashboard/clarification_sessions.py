"""Ephemeral, isolated clarification slots; client context never grants trust."""
import re
import threading
from time import monotonic as _monotonic

from target_contract import BLOCKED, ROOMS, canonical_text


class ClarificationSessions:
    def __init__(self, ttl_s=300, max_sessions=1024):
        self.ttl_s, self.max_sessions = ttl_s, max_sessions
        self._pending = {}
        self._lock = threading.RLock()

    def resolve(self, command, session_id):
        """Return (canonical command or None, clarification response or None)."""
        text = canonical_text(command)
        valid_session = isinstance(session_id, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', session_id))
        key = session_id if valid_session else None
        now = _monotonic()
        with self._lock:
            for expired in [k for k, v in self._pending.items() if now - v['updated'] >= self.ttl_s]:
                del self._pending[expired]
            if text in {'取消', '停止', '算了', '不用了'}:
                if key:
                    self._pending.pop(key, None)
                return None, {'answer': '已取消待澄清请求。', 'actions': [], 'cancelled': True,
                              'semantic_complete': False, 'task_complete': False, 'execution_complete': False}
            pending = self._pending.get(key) if key else None
            # Accept only slot-shaped replies. A new independent command clears
            # the prior pending intent, so it cannot unexpectedly resume later.
            slot_reply = bool(re.fullmatch(r'(?:(?:客厅|卧室|厨房|浴室|书房|室外)(?:的)?(?:灯光?)?)?(?:(?:调到|调至|设为|设置为)?\d+[%％])?', text))
            fuzzy = bool(re.fullmatch(r'(?:请)?(?:把)?(?:(?:客厅|卧室|厨房|浴室|书房|室外)(?:的)?)?(?:灯光?)?'
                r'(?:有点暗|太暗(?:了)?|调亮一点|亮一点|有点亮|太亮(?:了)?|调暗一点|暗一点)', text))
            if pending and not slot_reply and not fuzzy:
                self._pending.pop(key, None)
                pending = None
            if pending and fuzzy:
                # A new relative request starts a new intent; a prior numeric
                # answer must not silently become the new request's value.
                self._pending.pop(key, None)
                pending = None
            if not pending and not fuzzy:
                return command, None
            values = dict(pending or {'device': 'light_control'})
            rooms = [room for room in ROOMS if room in text]
            numbers = re.findall(r'(?<![\d.\-])(\d+)[%％]', text)
            if len(rooms) == 1:
                values['room'] = rooms[0]
            if len(numbers) == 1 and 0 <= int(numbers[0]) <= 100:
                values['value'] = int(numbers[0])
            values['updated'] = now
            if key:
                if key not in self._pending and len(self._pending) >= self.max_sessions:
                    self._pending.pop(min(self._pending, key=lambda k: self._pending[k]['updated']))
                self._pending[key] = values
            if 'room' in values and 'value' in values:
                if key:
                    self._pending.pop(key, None)
                return f"把{values['room']}灯光设为{values['value']}%", None
            missing = [slot for slot in ('room', 'value') if slot not in values]
            question = '请指定房间和目标亮度百分比。' if len(missing) == 2 else ('请指定房间。' if missing == ['room'] else '请指定目标亮度百分比（0到100%）。')
            if not key:
                question += '当前未提供会话ID，请在同一条请求中写明完整目标。'
            return None, {'answer': question, 'actions': [], 'clarification_required': True,
                          'missing_slots': missing, 'pending_intent': {'device': 'light_control',
                          **{k: values[k] for k in ('room', 'value') if k in values}},
                          'semantic_complete': False, 'task_complete': False, 'execution_complete': False,
                          'plan_valid': False, 'interface_ok': True}
