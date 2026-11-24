"""A narrow, transparent grounding guard for room and requested brightness.

This is not a semantic oracle. It checks explicit supported rooms, device
types and integer brightness targets; all monitoring arms share it.
"""
from __future__ import annotations
from dataclasses import replace
import re
from .protocol import LOCATIONS, validate_plan


def _integer(raw):
    if raw.isascii() and raw.isdecimal():return int(raw)
    digits={c:i for i,c in enumerate('零一二三四五六七八九')};digits['两']=2
    if raw in ('一百','百'):return 100
    if raw in digits:return digits[raw]
    if raw.count('十')==1:
        a,b=raw.split('十')
        if (not a or a in digits) and (not b or b in digits):return (digits[a] if a else 1)*10+(digits[b] if b else 0)
    return None


def _target_matches(text):
    numeral=r'([0-9零一二三四五六七八九十百两]+)'
    return re.finditer(r'(?:调亮到|调暗到|调到|调成|调为|设为|设置为|设置到|调整到|调节到|亮度为|亮度到|百分之)\s*'+numeral+r'(?![A-Za-z0-9.零一二三四五六七八九十百两])%?',text)

def explicit_brightness_targets(text):
    targets=[]
    for match in _target_matches(text):
        value=_integer(match.group(1))
        if value is not None and 0<=value<=100:targets.append(value)
    return targets

def _room_targets(text,mentioned):
    bound={}
    for clause in re.split(r'[，,；;。]|然后|接着|随后|并且|同时',text):
        matches=list(_target_matches(clause))
        rooms=[room for room in LOCATIONS if room in clause]
        for match in matches:
            value=_integer(match.group(1))
            if value is None or not 0<=value<=100:continue
            candidates=rooms
            if len(matches)>1:
                preceding=[(clause.rfind(room,0,match.start()),room) for room in rooms]
                preceding=[item for item in preceding if item[0]>=0]
                candidates=[max(preceding)[1]] if preceding else []
            if not candidates and len(mentioned)==1:candidates=list(mentioned)
            for room in candidates:bound.setdefault(room,set()).add(value)
    return bound


class GroundedBackend:
    def __init__(self,backend):self.backend=backend;self.name=backend.name
    def __getattr__(self,key):return getattr(self.backend,key)
    def infer(self,task,timeout_s):
        result=self.backend.infer(task,timeout_s)
        if not result.ok:return result
        try:plan=validate_plan(result.payload,task)
        except (ValueError,KeyError,TypeError):return result
        if plan.result_kind!='action':return result
        text=task.text;mentioned={room for room in LOCATIONS if room in text}
        targets=explicit_brightness_targets(text);bound=_room_targets(text,mentioned);reasons=[]
        if not targets and len(mentioned)==1 and task.brightness is not None:
            targets=[task.brightness];bound={next(iter(mentioned)):{task.brightness}}
        brightness_requested=bool(re.search(r'亮度|调亮|调暗',text)) or task.intent=='set_brightness'
        for action in plan.actions:
            if action.location not in mentioned:reasons.append('action_room_not_explicitly_requested')
            device_pattern=(r'灯|照明' if action.type in {'light_on','light_off','set_brightness'}
                else r'窗帘' if action.type.startswith('curtain_') else r'门|锁')
            if not re.search(device_pattern,text):reasons.append('action_device_type_not_explicitly_requested')
            if action.type=='set_brightness':
                if not targets:reasons.append('no_explicit_supported_brightness_target')
                elif action.brightness not in bound.get(action.location,set()):reasons.append('brightness_differs_from_explicit_room_target')
            elif action.type=='light_on' and brightness_requested:
                effective_brightness=100 if action.brightness is None else action.brightness
                if not targets:reasons.append('no_explicit_supported_brightness_target')
                elif effective_brightness not in bound.get(action.location,set()):reasons.append('brightness_differs_from_explicit_room_target')
        if not reasons:return result
        payload={'result_kind':'clarification','actions':[],
                 'explanation':'模型计划与明确地点或亮度参数不一致，请确认地点和0到100的整数亮度。'}
        raw=dict(result.raw_response or {})
        raw['grounding_v6']={'intervened':True,'reasons':sorted(set(reasons)),
                            'original_payload':result.payload,'action_count_after_guard':0,
                            'scope':'explicit room, device type and integer brightness only; no full semantic guarantee'}
        return replace(result,payload=payload,raw_response=raw)
