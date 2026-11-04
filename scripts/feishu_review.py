#!/usr/bin/env python3
"""Source-bound Feishu review comments. Python 3.9+ and authenticated lark-cli.

This is a transport/validation helper, not a keyword-based content judge.
The reviewing agent must supply evidence-grounded issues after reading the BF.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def cli(*args):
    env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1')
    r = subprocess.run(['lark-cli', *args, '--as', 'user'], capture_output=True,
                       text=True, env=env, timeout=180)
    try:
        value = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError('lark-cli没有返回JSON；检查连接和本地认证，不自动重试写入')
    if r.returncode or value.get('ok') is not True:
        raise RuntimeError(json.dumps(value.get('error', {'message': 'CLI failed'}), ensure_ascii=False))
    return value['data']


def paginate(command, *args):
    token = ''
    seen = set()
    out = []
    while True:
        if token in seen:
            raise RuntimeError('分页游标重复；不得把不完整结果用于审核或防重')
        seen.add(token)
        extra = ['--page-token', token] if token else []
        d = cli('drive', command, *args, '--page-size', '100', *extra)
        out.extend(d.get('items', []))
        if not d.get('has_more'):
            return out
        token = d.get('page_token')
        if not token:
            raise RuntimeError('has_more但缺少分页游标')


def comments(url):
    cards = paginate('+list-comments', '--url', url, '--solved-status', 'all', '--need-relation')
    for card in cards:
        if card.get('has_more'):
            replies = paginate('+list-replies', '--url', url, '--comment-id', card['comment_id'])
            card['reply_list'] = {'replies': replies}
            card['has_more'] = False
    return cards


def active_text(el):
    # Deleted text is retained in raw_xml but not silently treated as current copy.
    if el.tag == 'del':
        return ''
    value = el.text or ''
    for child in el:
        value += active_text(child) + (child.tail or '')
    return value


def inspect_document(data):
    doc = data['document']
    xml = doc['content']
    root = ET.fromstring('<root>' + xml + '</root>')
    # Feishu adds comment-refs wrappers and increments revision when we comment.
    # Ignore only that annotation metadata, preserving real text/style/asset edits.
    def normalize(parent):
        for child in list(parent):
            annotation_span = child.tag == 'span' and set(child.attrib) == {'comment-refs'}
            normalize(child)
            child.attrib.pop('comment-refs', None)
            if annotation_span:
                index = list(parent).index(child)
                previous = list(parent)[index - 1] if index else None
                if previous is None:
                    parent.text = (parent.text or '') + (child.text or '')
                else:
                    previous.tail = (previous.tail or '') + (child.text or '')
                nested = list(child)
                for offset, item in enumerate(nested):
                    parent.insert(index + offset, item)
                last = nested[-1] if nested else previous
                if last is None:
                    parent.text = (parent.text or '') + (child.tail or '')
                else:
                    last.tail = (last.tail or '') + (child.tail or '')
                parent.remove(child)
        parent.attrib.pop('comment-refs', None)
    normalize(root)
    blocks = {}
    links = []
    for el in root.iter():
        if el.get('href'):
            links.append({'url': el.get('href'), 'label': active_text(el)})
        if el.get('id'):
            raw = ET.tostring(el, encoding='unicode')
            blocks[el.get('id')] = {
                'tag': el.tag, 'text': active_text(el), 'hash': digest(raw),
                'attributes': dict(el.attrib), 'raw_xml': raw,
            }
    return {'document_id': doc['document_id'], 'revision_id': doc.get('revision_id'),
            'content_hash': digest(ET.tostring(root, encoding='unicode')), 'raw_xml': xml, 'blocks': blocks, 'links': links}


def fetch(url):
    return inspect_document(cli('docs', '+fetch', '--doc', url, '--detail', 'full'))


def snapshot(url):
    d = fetch(url)
    d.update(url=url, fetched_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
             comments=comments(url), comments_complete=True)
    return d


def comment_text(card):
    parts = []
    for reply in card.get('reply_list', {}).get('replies', []):
        for e in reply.get('content', {}).get('elements', []):
            parts.append((e.get('text_run') or {}).get('text', e.get('text', '')))
    return '\n'.join(parts)


def root_comment_text(card):
    replies = card.get('reply_list', {}).get('replies', [])
    if not replies:
        return ''
    return comment_text({'reply_list': {'replies': replies[:1]}})


def anchored_to(card, block_id):
    if card.get('is_whole') is True or (card.get('relation') or {}).get('content_deleted'):
        return False

    def contains(value):
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                return False
            return contains(decoded) if isinstance(decoded, (dict, list)) else False
        if isinstance(value, list):
            return any(contains(v) for v in value)
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('content_anchor_id', 'blockID', 'block_id') and child == block_id:
                    return True
                if key == 'block_ids' and isinstance(child, list) and block_id in child:
                    return True
                if contains(child):
                    return True
        return False

    return contains({'extra': card.get('extra'), 'relation': card.get('relation')})


def matching_comments(cards, issue):
    # The fingerprint stays local. Match the root body and exact anchor after a
    # timeout; a later reply quoting our text must not count as our comment.
    expected = ' '.join(issue['text'].split())
    return [c for c in cards if anchored_to(c, issue['block_id']) and
            (' '.join(root_comment_text(c).split()) == expected or
             issue['marker'] in root_comment_text(c))]


def timestamp(seconds):
    seconds = int(seconds)
    return f'{seconds // 60:02}:{seconds % 60:02}'


def render_comment(issue):
    text = issue.get('comment', issue['change'])
    if not isinstance(text, str) or not text.strip():
        raise ValueError('对外批注需填写白话修稿建议')
    text = text.strip()
    if re.search(r'\[APP(?:审稿|复审):|(?:复核标准|验收标准|依据|BF来源)\s*[:：]|【(?:必须修改|建议优化|待补证据)】|(?:^|\n)\s*(?:原句|原文)\s*[:：]', text):
        raise ValueError('对外批注只写白话建议；不要复述定位原句；依据、标准、分类和审稿标记留在内部')
    if issue['kind'] == 'video':
        text = (f'{issue["media"]["version"]} '
                f'{timestamp(issue["start_seconds"])}–{timestamp(issue["end_seconds"])}：' + text)
    return text


def prepare(snap, request):
    if not snap.get('comments_complete'):
        raise ValueError('需要完整评论及回复')
    for key in ('project', 'audience', 'campaign', 'bf', 'target_version'):
        if not request.get(key):
            raise ValueError('缺少审核上下文：' + key)
    if request['project'] != 'doubao-app-peiyao':
        raise ValueError('项目不匹配：本工具仅用于App/Peiyao')
    if request['audience'] not in ('internal', 'client', 'isolated-test'):
        raise ValueError('需明确对内/甲方/隔离测试稿')
    decision = request.get('decision')
    if decision not in ('pass', 'revise', 'needs_evidence'):
        raise ValueError('需明确pass/revise/needs_evidence；不得为凑意见制造问题')
    scope = request.get('review_scope', [])
    if not scope or any(k not in ('text', 'image', 'video') for k in scope):
        raise ValueError('需明确本轮审核范围text/image/video')
    coverage = request.get('coverage', {})
    for key in ('bf_matched', 'body_read', 'comments_read', 'required_links_checked'):
        if coverage.get(key) is not True:
            if decision != 'needs_evidence':
                raise ValueError('证据覆盖未完成：' + key)
    gaps = request.get('blocking_gaps', [])
    if decision == 'pass':
        if request.get('issues') or gaps:
            raise ValueError('通过应零阻断意见；可选建议另记，不写返工批注')
        if 'video' in scope:
            videos = coverage.get('videos', [])
            if not videos:
                raise ValueError('视频通过需当前视频覆盖记录')
            for media in videos:
                if not media.get('continuous_viewed') or (media.get('has_audio') is not False and media.get('audio_listened') is not True):
                    raise ValueError('抽帧/ASR不能登记完整视频通过')
                block = snap['blocks'].get(media.get('block_id'), {})
                if block.get('tag') != 'source' or block.get('attributes', {}).get('token') != media.get('token'):
                    raise ValueError('视频通过覆盖未绑定当前附件块')
                if not media.get('path') or file_digest(media['path']) != media.get('sha256'):
                    raise ValueError('视频通过覆盖缺少当前文件校验')
        if 'image' in scope and not coverage.get('all_images_readable'):
            raise ValueError('图片通过需逐页确认文字和细节可读')
    if decision == 'revise' and not request.get('issues'):
        raise ValueError('没有修改事项就不要判修改后复审')
    if decision == 'needs_evidence' and not gaps and not request.get('issues'):
        raise ValueError('待补证据需列出实际缺口')
    issues = []
    for original in request.get('issues', []):
        issue = dict(original)
        for key in ('block_id', 'kind', 'category', 'problem', 'change', 'acceptance', 'evidence', 'rule_source'):
            if not issue.get(key):
                raise ValueError('意见缺字段：' + key)
        if issue['kind'] not in ('text', 'video', 'image'):
            raise ValueError('未知审核类型')
        if issue['kind'] not in scope:
            raise ValueError('意见超出本次审核范围')
        if issue['category'] not in ('必须修改', '建议优化', '待补证据'):
            raise ValueError('需区分硬要求、建议和缺口')
        block = snap['blocks'].get(issue['block_id'])
        if not block:
            raise ValueError('目标块不存在')
        if issue['kind'] == 'text':
            if not issue.get('quote') or issue['quote'] not in block['text']:
                raise ValueError('原句不在当前有效文本里')
        if issue['kind'] in ('image', 'video'):
            media = issue.get('media', {})
            for key in ('token', 'sha256', 'path', 'filename', 'version', 'review_method'):
                if media.get(key) in (None, ''):
                    raise ValueError('媒体缺字段：' + key)
            token_key = 'src' if issue['kind'] == 'image' else 'token'
            expected_tag = 'img' if issue['kind'] == 'image' else 'source'
            if block['tag'] != expected_tag or block['attributes'].get(token_key) != media['token']:
                raise ValueError('媒体附件与块不匹配')
            if block['attributes'].get('name') != media['filename']:
                raise ValueError('附件文件名不匹配')
            if file_digest(media['path']) != media['sha256']:
                raise ValueError('媒体文件内容已变化')
        if issue['kind'] == 'video':
            duration = media.get('duration_seconds')
            start, end = issue.get('start_seconds'), issue.get('end_seconds')
            if any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, end, duration)):
                raise ValueError('时间点及时长需为有效秒数')
            if not 0 <= start < end <= duration:
                raise ValueError('视频时间区间越界')
            if issue.get('requires_audio') and media.get('audio_listened') is not True:
                raise ValueError('未实听原音，不能写入音频判断')
        fingerprint = {
            'doc': snap['document_id'], 'block': issue['block_id'], 'kind': issue['kind'],
            'quote': issue.get('quote'), 'problem': issue['problem'],
            'media': issue.get('media', {}).get('sha256'),
            'range': [issue.get('start_seconds'), issue.get('end_seconds')],
        }
        marker = '[APP审稿:' + digest(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False))[:16] + ']'
        issue.update(marker=marker, block_hash=block['hash'], text=render_comment(issue))
        issues.append(issue)
    return {'schema_version': 3, 'url': snap['url'], 'document_id': snap['document_id'],
            'content_hash': snap['content_hash'], 'revision_id': snap['revision_id'],
            'context': {k: request[k] for k in ('project', 'audience', 'campaign', 'bf', 'target_version')},
            'decision': decision, 'review_scope': scope, 'coverage': coverage,
            'blocking_gaps': gaps, 'issues': issues}


def apply_plan(plan, receipt_path):
    # No automatic retries around writes: reconcile server state first on a timeout.
    if plan.get('schema_version') != 3:
        raise ValueError('旧格式方案不可直接写入；请重新生成白话批注方案')
    for issue in plan['issues']:
        if issue['text'] != render_comment(issue):
            raise ValueError('批注正文与当前白话方案不一致；请重新生成方案')
    url = plan['url']
    results = []
    receipt = {'url': url, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'results': results}
    if not plan['issues']:
        receipt.update(status='no_comments_needed', decision=plan.get('decision'),
                       finished_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        save(receipt_path, receipt)
        return receipt
    cards = comments(url)
    for issue in plan['issues']:
        existing = matching_comments(cards, issue)
        if len(existing) > 1:
            raise RuntimeError('同位置已有多条相同意见，先人工核对，不自动写入')
        if existing:
            results.append({'marker': issue['marker'], 'status': 'already_exists',
                            'block_id': issue['block_id'], 'text_hash': digest(issue['text']),
                            'comment_ids': [c['comment_id'] for c in existing]})
            save(receipt_path, receipt)
            continue
        live = fetch(url)
        if live['document_id'] != plan['document_id'] or live['content_hash'] != plan['content_hash']:
            raise RuntimeError('源稿已变化，停止写入。重新读取并重新审核，不自动重定位')
        block = live['blocks'].get(issue['block_id'])
        if not block or block['hash'] != issue['block_hash']:
            raise RuntimeError('定位块已变化')
        m = issue.get('media')
        if m and file_digest(m['path']) != m['sha256']:
            raise RuntimeError('视频文件已变化')
        result = {'marker': issue['marker'], 'status': 'write_started',
                  'block_id': issue['block_id'], 'text_hash': digest(issue['text'])}
        results.append(result)
        save(receipt_path, receipt)
        try:
            result['response'] = cli('drive', '+add-comment', '--doc', url, '--block-id', issue['block_id'],
                                     '--content', json.dumps([{'type': 'text', 'text': issue['text']}], ensure_ascii=False))
            result['status'] = 'write_returned'
            save(receipt_path, receipt)
            cards = comments(url)
            matches = matching_comments(cards, issue)
            if len(matches) != 1:
                raise RuntimeError('读回评论数量异常；不自动重写')
            card = matches[0]
            # A successful API response alone is not proof of correct anchoring.
            if not anchored_to(card, issue['block_id']):
                raise RuntimeError('评论已写入但锚点读回不一致；需要检查，不自动重写')
            result.update(status='verified', comment_id=card['comment_id'], relation=card.get('relation'))
            save(receipt_path, receipt)
        except Exception:
            result['status'] = 'needs_reconciliation'
            save(receipt_path, receipt)
            raise
    receipt['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    save(receipt_path, receipt)
    return receipt


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('snapshot'); p.add_argument('--url', required=True); p.add_argument('--out', required=True)
    p = sub.add_parser('plan'); p.add_argument('--snapshot', required=True); p.add_argument('--issues', required=True); p.add_argument('--out', required=True)
    p = sub.add_parser('apply'); p.add_argument('--plan', required=True); p.add_argument('--receipt', required=True)
    a = ap.parse_args()
    if a.command == 'snapshot':
        result = snapshot(a.url); save(a.out, result)
        print(json.dumps({'document_id': result['document_id'], 'blocks': len(result['blocks']), 'comments': len(result['comments'])}))
    elif a.command == 'plan':
        result = prepare(json.loads(Path(a.snapshot).read_text()), json.loads(Path(a.issues).read_text()))
        save(a.out, result); print(json.dumps({'issues': len(result['issues']), 'out': a.out}))
    else:
        result = apply_plan(json.loads(Path(a.plan).read_text()), a.receipt)
        print(json.dumps({'results': [{k: r[k] for k in ('status', 'comment_id') if k in r} for r in result['results']]}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
