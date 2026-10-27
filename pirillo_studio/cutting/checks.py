"""Declared-read cut checks and content-bound requests and locks (docs/cutting.md section 2)."""
import math
import re

from ..spine.files import SpineError, canon, digest, inside
from . import plan as io


def _flag(plan, rule, span, measure, at=None):
    at = span['src'][0] if at is None else at
    tolerance = io.settings()['edges']['tolerance_s']
    for named in plan['named']:
        if (named['rule'] == rule and isinstance(named.get('why'), str) and named['why'].strip()
                and abs(named['at_src'] - at) <= tolerance):
            return []
    return [{'rule': rule, 'span': span['id'], 'at_src': at, 'measure': measure}]


def _ordered(plan):
    return sorted(plan['spans'], key=lambda s: s['order'])


def _contains(plan, words, anchor):
    try:
        chosen = io.selected(words, anchor)
    except SpineError:
        return None
    wanted = {w['id'] for w in chosen}
    return next((s for s in plan['spans'] if s['words']
                 and wanted <= {w['id'] for w in io.selected(words, s['words'])}), None)


def own_words(plan, words):
    """Reads plan and transcript only; source coverage and the two span variants."""
    io.validate_plan(plan, words)
    flags, intervals = [], []
    ordered = _ordered(plan)
    fps = io.number(plan['measurements']['fps'], 'frame rate')
    if fps <= 0:
        raise SpineError('frame rate must be positive')
    for i, span in enumerate(ordered):
        a, b = span['src']
        intervals.append((a, b))
        chosen = io.selected(words, span['words']) if span['words'] else []
        heard = [w for w in words if w['start'] < b and w['end'] > a]
        reason = None
        if span['enter'] == 'pop':
            expression = span['expression']
            if (chosen or heard or not i or not expression['seen'].strip()
                    or not ordered[i-1]['words'] or ordered[i-1]['words'][-1] != expression['after_word']):
                reason = 'expression must follow its word, contain no speech and name what is seen'
        elif not chosen or {w['id'] for w in heard} != {w['id'] for w in chosen}:
            reason = 'source interval does not contain exactly its declared words'
        elif b - a < io.settings()['shots']['min_frames'] / fps:
            reason = 'speech shot is shorter than the frame minimum'
        if reason:
            flags += _flag(plan, 'own-words', span, reason)
    intervals += [tuple(r['src']) for r in plan['removed']]
    end = 0
    for a, b in sorted(intervals):
        if a != end or b <= a or a < 0:
            flags += _flag(plan, 'own-words', ordered[0], f'source coverage gap or overlap at {end}: {a}, {b}')
        end = b
    if end != plan['source_duration_s']:
        flags += _flag(plan, 'own-words', ordered[-1], 'kept plus removed do not tile the source')
    return flags


def edges(plan, words, measurements):
    """Reads plan, words and source audio through the same measure used to settle."""
    from .settle import edge_bounds
    flags = []
    ordered = _ordered(plan)
    tolerance = io.settings()['edges']['tolerance_s']
    for i, span in enumerate(ordered):
        if span['words']:
            if i < len(ordered)-1 and span.get('finished') is not True:
                flags += _flag(plan, 'edges', span, 'mid-sentence splice needs a named read', span['src'][1])
            expected = edge_bounds(span, words, measurements, i == len(ordered)-1, plan['end_hold_s'])
            if any(abs(a-b) > tolerance for a, b in zip(span['src'], expected)):
                flags += _flag(plan, 'edges', span, {'actual': span['src'], 'measured': expected})
    return flags


def takes(plan, words):
    """One detector follows kept speech in story order, so the later take wins across spans too."""
    flags, config = [], io.settings()['takes']
    chosen, owners, times, offset = [], [], [], 0
    for span in _ordered(plan):
        for word in io.selected(words, span['words']) if span['words'] else []:
            chosen.append(word)
            owners.append(span)
            times.append(offset + word['start'] - span['src'][0])
        offset += span['src'][1] - span['src'][0]
    tokens = [re.sub(r'\W+', '', w['text']).casefold() for w in chosen]
    for i, word in enumerate(chosen):
        labels = [kind for kind in ('false_start', 'repeat', 'stumble', 'filler', 'flub')
                  if word.get(kind) is True or word.get('kind') == kind or kind in word.get('flags', [])]
        for n in range(1, min(config['repeat_words'], len(tokens)-i) + 1):
            for j in range(i+n, len(tokens)-n+1):
                delay = times[j] - times[i]
                if delay > config['repeat_window_s']:
                    break
                if tokens[i:i+n] == tokens[j:j+n] and all(tokens[i:i+n]):
                    if n <= config['stumble_words'] and j == i+n and delay <= config['stumble_window_s']:
                        labels.append('stumble')
                    elif n >= config['repeat_words']:
                        labels.append('repeat')
        if labels:
            flags += _flag(plan, 'takes', owners[i], {'word': word['id'], 'kinds': sorted(set(labels))}, word['start'])
    return flags


def jcuts(plan, words, measurements):
    """Reads plan, transcript and measured source motion; never adjusts a splice."""
    flags, cfg = [], io.settings()['jcuts']
    fps = io.number(measurements['fps'], 'frame rate')
    if fps <= 0:
        raise SpineError('frame rate must be positive')
    ordered = _ordered(plan)
    for i, span in enumerate(ordered):
        if span['enter'] != 'jcut':
            continue
        lead = io.number(span['lead_s'], 'J-cut lead')
        reasons = []
        if not i:
            reasons.append('opening span cannot carry a J-cut')
        if lead * fps < cfg['min_frames'] or lead > cfg['max_lead_s']:
            reasons.append('lead outside frame bounds')
        if (span['src'][1]-span['src'][0]-lead)*fps < io.settings()['shots']['min_frames']:
            reasons.append('too little incoming picture after lead')
        if i:
            previous = ordered[i-1]
            end = previous['src'][1]
            if end + lead > plan['source_duration_s']:
                reasons.append('extension leaves source')
            intruders = [w['id'] for w in words if min(w['end'], end+lead)-max(w['start'], end) > cfg['word_overlap_s']]
            if intruders and not span.get('seen_words') == intruders:
                reasons.append({'unseen_words_in_extension': intruders})
            if lead > previous['src'][1]-previous['src'][0]:
                reasons.append('lead exceeds outgoing take')
        motion = [r for r in measurements['motion'] if span['src'][0] <= r['time'] <= span['src'][0]+lead
                  and io.number(r['value'], 'motion') >= cfg['motion_threshold']]
        if not motion or span.get('lead_from') != 'motion onset':
            reasons.append('lead has no measured motion onset')
        elif abs(lead-(motion[0]['time']-span['src'][0]+cfg['motion_after_frames']/fps)) > 1/fps:
            reasons.append('lead does not follow measured motion onset')
        if reasons:
            flags += _flag(plan, 'jcuts', span, reasons)
    return flags


def shots(plan):
    """Reads the plan's shot and finished-line decisions only."""
    flags, cfg = [], io.settings()['shots']
    ordered = _ordered(plan)
    for i, span in enumerate(ordered):
        previous = ordered[i-1] if i else None
        if previous and previous['shot'] == span['shot']:
            flags += _flag(plan, 'shots', span, 'neighbours share a shot size')
        if previous and previous['enter'] == span['enter'] == 'push':
            flags += _flag(plan, 'shots', span, 'adjacent pushes')
        if span['enter'] == 'pop' and (not cfg['pop_min_s'] <= span['src'][1]-span['src'][0] <= cfg['pop_max_s']
                or span['shot'] not in ('face', 'mouth') or not previous or previous.get('finished') is not True
                or previous['words'][-1] != span['expression']['after_word']):
            flags += _flag(plan, 'shots', span, 'pop needs a finished line, punched shot and permitted hold')
    return flags


def cards(plan, words, job, overlay, callouts):
    """Reads only plan, words, job and the two declared overlay plans."""
    flags = []
    for record, field in ((overlay, 'cards'), (callouts, 'callouts')):
        if record['plan'] != plan['key']:
            flags += _flag(plan, 'cards', plan['spans'][0], f'{field} belongs to another plan')
        for item in record[field]:
            if (field == 'callouts' or item.get('kind') != 'cover') and not _contains(plan, words, item['anchor']):
                flags += _flag(plan, 'cards', plan['spans'][0], f'{field} {item["id"]} anchor leaves kept span')
    for span in plan['spans']:
        vo = any(a < span['src'][1] and b > span['src'][0] for a, b in job['voice_over'])
        hidden = vo or 'patch' in span or span.get('hide_picture') is True
        covers = [c for c in overlay['cards'] if c.get('kind') == 'cover' and c.get('covers') == span['id']]
        if hidden and (span.get('hide_picture') is not True or not covers):
            flags += _flag(plan, 'cards', span, 'voice-over or patch requires hidden picture and full-span cover')
    return flags


def patches(plan, patch_records, voice):
    """Reads patch bytes and voice picks; no media or capability operation is performed."""
    flags = []
    for span in plan['spans']:
        if 'patch' not in span:
            continue
        reference = span['patch']
        entry = patch_records[reference['record']]
        patch = entry['record']
        rate, count = io.number(patch['sample_rate'], 'patch rate'), io.number(patch['sample_count'], 'patch samples')
        duration = io.number(patch['duration_s'], 'patch duration')
        own = io.selected(io.words(patch), reference['words'])
        picks = [p for p in voice['picks'] if p['id'] == patch['owner_pick'] and p['take'] == patch['take']
                 and p['approved'] is True and p['capability'] == 'voice' and p['kind'] == 'speech' and p['cites']]
        tolerance = io.settings()['edges']['tolerance_s']
        reasons = []
        if entry['sha'] != reference['sha']:
            reasons.append('patch hash changed')
        if rate <= 0 or count <= 0 or count != int(count) or not math.isclose(count/rate, duration, rel_tol=0, abs_tol=1e-9):
            reasons.append('patch rate and sample count disagree')
        if rate != plan['measurements']['sample_rate']:
            reasons.append('patch rate differs from source')
        if not own or abs(own[-1]['end']-duration) > tolerance:
            reasons.append('patch take does not end at its own end')
        if any(io.number(patch['joins'][side], 'patch join') < io.settings()['patches']['join_clearance_s']
               for side in ('before_s', 'after_s')):
            reasons.append('patch join is not clear on both sides')
        if not picks:
            reasons.append('patch has no approved voice speech pick with cites')
        if reasons:
            flags += _flag(plan, 'patches', span, reasons)
    return flags


def cold_open(plan):
    """Reads only the plan, including its word-order evidence."""
    if 'cold_open' not in plan or plan['cold_open'] is None:
        return []
    opening = plan['cold_open']
    span = next((s for s in plan['spans'] if s['id'] == opening['span']), None)
    if span is None:
        return _flag(plan, 'cold-open', plan['spans'][0], 'cold open names no kept span')
    ids = span['word_ids']
    endpoints = opening['words']
    if (len(endpoints) != 2 or any(w not in ids for w in endpoints)
            or ids.index(endpoints[0]) > ids.index(endpoints[-1]) or span['enter'] == 'push'
            or (span['enter'] == 'jcut' and span['word_times'][endpoints[0]][0] < span['src'][0]+span['lead_s'])):
        return _flag(plan, 'cold-open', span, 'cold open crosses a span, shot or held frames')
    return []


def shorts(maker, spec, plan, words):
    """Reads both shorts plans, cut and transcript; pieces are verified against the shared settler."""
    from .settle import short_pieces
    flags, cfg = [], io.settings()['shorts']
    anchor = plan['spans'][0]
    if spec['plan'] != plan['key'] or len([short for short in spec['shorts'] if short.get('full') is True]) != 1:
        flags += _flag(plan, 'shorts', anchor, 'shorts require the current plan and exactly one full vertical')
    if [s['id'] for s in maker['shorts']] != [s['id'] for s in spec['shorts']]:
        flags += _flag(plan, 'shorts', anchor, 'maker and settled short identities differ')
    decisions = [{k: v for k, v in short.items() if k != 'pieces'} for short in spec['shorts']]
    if decisions != maker['shorts']:
        flags += _flag(plan, 'shorts', anchor, 'settled shorts changed the maker decisions')
    for short in spec['shorts']:
        reason = None
        if not isinstance(short['point'], str) or not short['point'].strip():
            reason = 'short needs a point'
        elif short.get('full') is True:
            if any(k in short for k in ('spans', 'pieces', 'opens_on', 'ends_on', 'stands_alone')):
                reason = 'full vertical cannot override the story'
        else:
            ranges = short['spans']
            if not ranges or any(not _contains(plan, words, r) for r in ranges):
                reason = 'short words leave kept spans'
            elif short['opens_on'] != ranges[0][0] or short['ends_on'] != ranges[-1][-1]:
                reason = 'short does not open or end on its declared word'
            else:
                first = io.selected(words, ranges[0])[0]['text'].strip('.,!?').casefold()
                try:
                    if io.short_context(short['stands_alone']) is None and first in cfg['backrefs']:
                        reason = 'short needs subject context for its opening'
                except SpineError as exc:
                    reason = str(exc)
                expected = short_pieces(ranges, words, plan['measurements'])
                length = sum(b-a for a, b in short['pieces'])
                if short['pieces'] != expected or not cfg['min_s'] <= length <= cfg['max_s']:
                    reason = 'short pieces differ from its words or fall outside duration band'
        if reason:
            flags += _flag(plan, 'shorts', anchor, {'short': short['id'], 'reason': reason})
    return flags


_BASE = ('records/cut_plan2.json', 'records/words.json', 'source.mp4', 'records/job.json',
         'records/overlay_plan.json', 'records/bed.json', 'records/callouts_plan.json')
_SHORTS = ('records/shorts_plan.json', 'records/shorts_spec.json')
_ERRORS = (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError)


def _key(reads):
    return digest(canon({'reads': reads, 'settings': io.settings()}))


def check(video):
    """Write one auditable verdict, including a blocked record for an unreadable declared input."""
    with io.writer(video):
        result = verdict(video)
        io.write(video, 'records/checks/cut.json', result)
        return result


def verdict(video, overlays=True):
    """check's body, the caller holding the video's writer; overlays False judges the settled plan alone, before
    the overlays stage writes cards, bed and shorts (docs/cutting.md section 3)."""
    reads, results, plan_key = {}, {}, None
    running = "inputs"
    try:
        plan = io.read(video, 'records/cut_plan2.json')
        plan_key = plan['key']
        if plan_key != io.settle_key(plan['made_from'], plan['plan_version'], plan['measurement_key']):
            raise SpineError('settled plan key does not match its inputs and settings')
        paths = set(_BASE if overlays else _BASE[:4]) | set(plan['made_from'])
        if overlays and any(inside(video.folder, p).exists() for p in _SHORTS):
            paths.update(_SHORTS)
        patched = [s['patch']['record'] for s in plan['spans'] if 'patch' in s]
        if patched:
            paths.update(patched)
            paths.add('records/voice.json')
        reads = io.hashes(video, sorted(paths))
        if any(reads[p] != value for p, value in plan['made_from'].items()):
            raise SpineError('settled plan inputs changed; settle the current inputs')
        if reads.get('records/shorts_spec.json'):
            spec, made = io.read(video, 'records/shorts_spec.json'), {_SHORTS[0]: reads[_SHORTS[0]]}
            if (spec['made_from'], spec['plan'], spec['key']) != (made, plan_key, io.shorts_key(plan_key, made)):
                raise SpineError('records/shorts_spec.json: stale; settle the current shorts plan')
        words = io.words(io.read(video, 'records/words.json'))
        job = io.read(video, 'records/job.json')
        measurements = io.measurements(video, io.read(video, 'records/probe.json'))
        if digest(canon(measurements)) != plan['measurement_key']:
            raise SpineError('source measurements changed; settle the current readings')
        running = 'own-words'
        results['own-words'] = own_words(plan, words)
        running = 'edges'
        results['edges'] = edges(plan, words, measurements)
        running = 'takes'
        results['takes'] = takes(plan, words)
        running = 'jcuts'
        results['jcuts'] = jcuts(plan, words, measurements)
        running = 'shots'
        results['shots'] = shots(plan)
        if overlays:
            running = 'cards'
            overlay = io.read(video, 'records/overlay_plan.json')
            callouts = io.read(video, 'records/callouts_plan.json')
            bed = io.read(video, 'records/bed.json')
            if bed['plan'] != plan_key or not bed['request'].strip() or not bed['tempo_from'].strip():
                raise SpineError('bed does not name the current plan and request')
            results['cards'] = cards(plan, words, job, overlay, callouts)
        running = 'patches'
        patch_records = {p: {'record': io.read(video, p), 'sha': reads[p]} for p in patched}
        results['patches'] = patches(plan, patch_records, io.read(video, 'records/voice.json')) if patched else []
        running = 'cold-open'
        results['cold-open'] = cold_open(plan)
        running = 'shorts'
        results['shorts'] = shorts(io.read(video, _SHORTS[0]), io.read(video, _SHORTS[1]), plan, words) if _SHORTS[0] in reads else []
        running = 'input freshness'
        if reads != io.hashes(video, sorted(paths)):
            raise SpineError('cutting inputs changed during check')
        detail = [flag for flags in results.values() for flag in flags]
        state = 'fail' if detail else 'pass'
    except Exception as error:
        state, detail = 'blocked', f'{running} check could not run: {error}'
    result = {'ok': state == 'pass', 'state': state,
              'next_action': 'request_arc' if state == 'pass' else 'revise' if state == 'fail' else 'supply_inputs',
              'detail': detail, 'plan': plan_key, 'reads': reads, 'key': _key(reads), 'checks': results}
    return result


def _fresh(video, required):
    try:
        return _fresh_inputs(video, required)
    except _ERRORS as error:
        raise SpineError(f'cut check cannot authorize: {error}') from None


def _fresh_inputs(video, required):
    record = io.read(video, 'records/checks/cut.json')
    if record['state'] != 'pass' or record['ok'] is not True:
        raise SpineError('cut check is not a pass')
    if not set(required) <= set(record['reads']):
        raise SpineError('cut check does not cover every required arc input')
    reads = io.hashes(video, sorted(record['reads']))
    if reads != record['reads'] or _key(reads) != record['key']:
        raise SpineError('stale cut check: its inputs changed')
    plan = io.read(video, 'records/cut_plan2.json')
    if plan['key'] != record['plan']:
        raise SpineError('cut check belongs to another plan')
    for p in _SHORTS:
        if inside(video.folder, p).exists() and p not in reads:
            raise SpineError('shorts input appeared after the cut check')
    return record, plan


def request_arc(video):
    """The host submits this content-keyed task to the spine; cutting writes no request record."""
    with io.writer(video):
        record, plan = _fresh(video, (*_BASE, *_SHORTS))
        task = {'key': digest(canon({'plan': plan['key'], 'check': record['key']})),
                'plan': plan['key'], 'check': record['key'], 'touched': plan['touched']}
        result = video.request_arc(task)
        if not isinstance(result, dict) or result.get('state') != 'waiting_owner':
            raise SpineError('arc adapter did not return waiting_owner')
        return result


def story_map(record, plan, decision_hash, blocks):
    """The locked record, less locked_at, for this check record, plan, approved decision and story blocks."""
    order = [s['id'] for s in _ordered(plan)]
    if [s for block in blocks for s in block['spans']] != order or any(not b['why'].strip() for b in blocks):
        raise SpineError('story blocks do not cover the plan in story order')
    return {'plan': plan['key'], 'check': record['key'],
            'arc_decision': f'records/arc_decision.json#{decision_hash}',
            'reads': {p: record['reads'][p] for p in (*_BASE[4:], *_SHORTS)}, 'blocks': blocks, 'tail': order[-1]}


def lock(video):
    """Only a current, explicit approval locks; rejection carries the owner's words back to the maker."""
    with io.writer(video):
        identities = io.hashes(video, ['records/arc_decision.json', 'records/arc.json', 'records/checks/cut.json'])
        record, plan = _fresh(video, (*_BASE, *_SHORTS))
        decision = io.read(video, 'records/arc_decision.json')
        arc = io.read(video, 'records/arc.json')
        if (decision['plan'] != plan['key'] or decision['check'] != record['key']
                or arc['plan'] != plan['key'] or arc['check'] != record['key'] or decision['arc'] != arc['key']):
            raise SpineError('arc decision is stale')
        if decision['decision'] == 'reject':
            return {'ok': False, 'state': 'fail', 'next_action': 'revise', 'detail': decision['words']}
        if decision['decision'] != 'approve':
            raise SpineError('arc decision is not explicit approval')
        output = story_map(record, plan, identities['records/arc_decision.json'], decision['blocks'])
        path = inside(video.folder, 'records/story_map.json')
        if path.exists():
            previous = io.read(video, 'records/story_map.json')
            if all(previous.get(k) == value for k, value in output.items()):
                return previous
        output['locked_at'] = video.now()
        if (io.hashes(video, record['reads']) != record['reads']
                or io.hashes(video, identities) != identities):
            raise SpineError('cut or approval changed while locking')
        io.write(video, 'records/story_map.json', output)
        return output
