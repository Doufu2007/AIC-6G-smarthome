"""Bounded, server-owned request goals shared by planners and executors.

The parser proves complete coverage only for its explicit grammar. Unknown text
is never treated as an empty goal set or as permission for a model's guess.
"""
from __future__ import annotations

import math
import re

ROOMS = ('客厅', '卧室', '厨房', '浴室', '书房', '室外')
ALIASES = {'起居室': '客厅', '主卧': '卧室', '卫生间': '浴室', '厕所': '浴室',
           'WC': '浴室', '办公室': '书房', '户外': '室外'}
BLOCKED = re.compile(r'如果|假如|只有|只要|除非|一旦|否则|不要|不许|禁止|请勿|取消|停止|(?<!分)别|教程|故事|示例|解释|如何|怎么')
_DIGITS = '零一二三四五六七八九'
_PERCENT_WORDS = {_DIGITS[n]: n for n in range(10)}
_PERCENT_WORDS.update({('' if n // 10 == 1 else _DIGITS[n // 10]) + '十' +
                      ('' if n % 10 == 0 else _DIGITS[n % 10]): n for n in range(10, 100)})
_PERCENT_WORDS['一百'] = 100


def canonical_text(command):
    for alias, room in ALIASES.items():
        command = command.replace(alias, room)
    return command.strip().strip('。！!，,？? ')


def request_goals(command):
    """Return the entire ordered explicit plan, or None when not understood."""
    if not isinstance(command, str):
        return None
    text = canonical_text(command)
    # These two bounded read forms contain no conditional device mutation.
    text = re.sub(r'^如果方便[，,]\s*', '', text)
    negative_read = re.fullmatch(r'不要打开(客厅|卧室|厨房|浴室|书房|室外)(?:的)?灯[，,]只(?:查看|查询)(温度|湿度)', text)
    if negative_read:
        text = '查看' + negative_read.group(1) + negative_read.group(2)
    if not text or BLOCKED.search(text):
        return None
    shared_verb = re.fullmatch(r'分别(打开|关闭|拉开|拉上)(.+和.+)', text)
    if shared_verb:
        text = '，'.join(shared_verb.group(1) + noun for noun in shared_verb.group(2).split('和'))
    parts = re.split(r'(\s*(?:，|,|。|；|;|然后|接着|并且|同时|并|再|以及)+\s*)', text)
    clauses = parts[::2]
    if any(not clause for clause in clauses):
        return None
    goals, unordered_targets = [], {}
    for index, clause in enumerate(clauses):
        if index and re.search(r'然后|接着|再', parts[index * 2 - 1]):
            unordered_targets = {}
        goal = _single_goal(clause)
        if goal is None:
            return None
        effective = action_goal(goal)
        key = effective[:2]
        if key in unordered_targets and unordered_targets[key] != effective:
            # Differing targets for one device require explicit sequencing.
            return None
        unordered_targets[key] = effective
        goals.append(goal)
    return goals if 1 <= len(goals) <= 16 else None


def _single_goal(clause):
    cmd = re.sub(r'^(?:(?:请|麻烦)(?:你)?)?(?:帮我)?(?:把|将)?', '', clause)
    for room in ROOMS:
        target = re.escape(room) + r'(?:的)?'
        for noun, name, verbs in (
            (r'(?:灯光?|灯光亮度|灯亮度)', 'light_control', {'打开': 'on', '开启': 'on', '开': 'on', '关闭': 'off', '关掉': 'off', '关': 'off'}),
            (r'窗帘', 'curtain_control', {'打开': 'open', '拉开': 'open', '开启': 'open', '关闭': 'close', '拉上': 'close', '关上': 'close'}),
        ):
            choices = '|'.join(verbs)
            match = re.fullmatch(rf'(?:({choices}){target}{noun}|{target}{noun}({choices}))', cmd)
            if match:
                return {'name': name, 'params': {'location': room, 'action': verbs[match.group(1) or match.group(2)]}}
        match = re.fullmatch(target + r'(灯光?(?:亮度)?|窗帘(?:位置)?)(?:设置为|设为|调到|调至|调成|调整到|调整为|设置成)(?:(\d+)[%％]|百分之([零一二三四五六七八九十百]+))', cmd)
        level = (int(match.group(2)) if match and match.group(2) else
                 _PERCENT_WORDS.get(match.group(3)) if match else None)
        if level is not None and 0 <= level <= 100:
            curtain = match.group(1).startswith('窗帘')
            return {'name': 'curtain_control' if curtain else 'light_control',
                    'params': {'location': room, 'position' if curtain else 'brightness': level}}
        if re.fullmatch(r'(?:查看|查询|读取|请问)?' + target +
                        r'(?:现在|当前)?(?:的)?(?:温度和湿度|温度|湿度|室温)(?:是多少|多少度|怎么样)?', cmd):
            return {'name': 'read_temperature', 'params': {'location': room}}
    return None


def action_goal(action):
    """Canonical effective action; schema validity remains the device rail's job."""
    if not isinstance(action, dict) or set(action) != {'name', 'params'}:
        return None
    params, name = action['params'], action['name']
    if not isinstance(params, dict) or params.get('location') not in ROOMS:
        return None
    room = params['location']
    if name == 'read_temperature':
        return (name, room) if set(params) == {'location'} else None
    if name not in {'light_control', 'curtain_control'}:
        return None
    field, off, on = ('brightness', 'off', 'on') if name == 'light_control' else ('position', 'close', 'open')
    if set(params) - {'location', 'action', field}:
        return None
    operation = params.get('action', 'set' if field in params else None)
    if operation not in {off, on, 'set'}:
        return None
    level = params.get(field, 0 if operation == off else 100)
    if type(level) is not int or not 0 <= level <= 100:
        return None
    return name, room, (0 if operation == off else level)


def validate_goal_plan(command, actions, *, goals=None):
    if goals is None:
        goals = request_goals(command)
    if goals is None:
        return 'REQUEST_GOAL_UNRESOLVED'
    expected = [action_goal(goal) for goal in goals]
    actual = [action_goal(action) for action in actions]
    if None in actual or actual != expected:
        return 'REQUEST_GOAL_PLAN_MISMATCH'
    return None


def accept_goals(goals, state, execution_complete):
    """Accept all requested final targets using only the executor's fresh state."""
    if not execution_complete or not goals or not isinstance(state, dict):
        return False
    # Earlier intermediate targets are verified by the ordered execution batch;
    # the last target for each device is its required final state.
    final = {}
    for action in goals:
        goal = action_goal(action)
        if goal is None:
            return False
        final[goal[:2]] = goal
    for goal in final.values():
        name, room = goal[:2]
        group = {'light_control': 'lights', 'curtain_control': 'curtains', 'read_temperature': 'temperature'}[name]
        observed = state.get(group, {}).get(room)
        if not isinstance(observed, dict):
            return False
        if name == 'read_temperature':
            if any(type(observed.get(k)) not in (float, int) or not math.isfinite(observed[k]) for k in ('temp', 'humidity')):
                return False
        else:
            field, flag = ('brightness', 'is_on') if name == 'light_control' else ('position', 'is_open')
            if type(observed.get(field)) is not int or observed[field] != goal[2] or observed.get(flag) is not (goal[2] > 0):
                return False
    return True
