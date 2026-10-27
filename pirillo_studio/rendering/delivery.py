"""One final manifest from the existing measured master publication transaction."""
from contextlib import ExitStack

from ..spine.files import SpineError, filehash
from . import records, request
from .render import _render, approval, fresh

MANIFEST = 'records/final_manifest.json'


def finalize(video):
    """Publish every settled native master, then atomically bind the complete delivered set."""
    if video.stage != 'masters' or not video.native:
        raise SpineError('final manifest: native masters required')
    at = MANIFEST
    try:
        with records.writer(video, 'final-manifest'), ExitStack() as writers:
            records.path(video, MANIFEST, writable=True)
            inputs = request.Inputs(video)
            at = 'records/shorts_spec.json'
            spec = inputs.read(at)
            outputs = ['long', 'vertical'] + [f"short:{entry['id']}" for entry in spec['shorts'] if entry.get('full') is not True]
            # Resolve before effects; resolve again under the same writers individual renders use.
            for output in outputs:
                at = output
                req = request.resolve(video, video.stage, output)
                writers.enter_context(records.writer(video, req['output_id']))
            result, bindings, accepted = {'members': [], 'outputs': []}, {}, []
            for output in outputs:
                at = output
                req = request.resolve(video, video.stage, output)
                if 'draft-match' not in req['settings']['checks']:
                    raise SpineError('draft-match reading required')
                record = _render(video, req)
                if record['state'] != 'delivered':
                    reason = record['reason'] if record['state'] == 'waiting_owner' else record['error']
                    raise SpineError(f"{record['state']}: {reason}")
                accepted.append(record)
                caption = records.read(video, req['caption_binding']['record'])
                members = [{'path': record['file'], 'sha256': record['file_sha256']},
                           {'path': caption['sidecar']['file'], 'sha256': caption['sidecar']['sha256']}]
                evidence = {'output': output, 'key': record['key'], 'record': records.name(req),
                            'record_sha256': filehash(records.path(video, records.name(req))),
                            'readings': record['readings'], 'readings_sha256': record['readings_sha256']}
                result['members'].extend(members)
                result['outputs'].append(evidence)
                bindings.update({m['path']: m['sha256'] for m in members})
                bindings.update({evidence['record']: evidence['record_sha256'],
                                 evidence['readings']: evidence['readings_sha256']})
            # Later renders must not invalidate an earlier output's approval, request or artifacts.
            for record in accepted:
                req = record['request']
                at = req['output']
                approved, _, acceptance, reason = approval(video, req)
                if not approved:
                    raise SpineError(reason)
                fresh(video, req, acceptance)
            for name, sha in {**inputs.hashes, **bindings}.items():
                if sha is None or filehash(records.path(video, name)) != sha:
                    raise SpineError(f'{name}: changed before final publication')
            at = MANIFEST
            if records.read(video, MANIFEST) != result:
                records.write(video, MANIFEST, result)
            return result
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise SpineError(f'final manifest: {at}: {error}') from None
