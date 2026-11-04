#!/usr/bin/env python3
"""Create source-bound frames/audio aids. These never imply audiovisual review."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--file',required=True);p.add_argument('--out',required=True)
    p.add_argument('--token',required=True);p.add_argument('--block-id',required=True)
    p.add_argument('--version',required=True);p.add_argument('--frames',type=int,default=16)
    p.add_argument('--start',type=float,default=0);p.add_argument('--end',type=float)
    p.add_argument('--audio',action='store_true');p.add_argument('--ffmpeg',default='ffmpeg')
    a=p.parse_args();source=Path(a.file).resolve();out=Path(a.out).resolve()
    if not source.is_file():raise SystemExit('源文件不存在')
    if not 2<=a.frames<=240:raise SystemExit('帧数需2–240；按需分段，不自动声称连续观看')
    ff=shutil.which(a.ffmpeg)
    if not ff:raise SystemExit('需要ffmpeg；请配置--ffmpeg可执行路径')
    out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()):raise SystemExit('输出目录非空；使用新的版本目录，避免混入旧帧')
    probe=subprocess.run([ff,'-hide_banner','-i',str(source)],capture_output=True,text=True,timeout=60)
    match=re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)',probe.stderr)
    if not match or 'Video:' not in probe.stderr:raise SystemExit('未取得视频时长，不能推测')
    hour,minute,second=map(float,match.groups());duration=hour*3600+minute*60+second
    end=duration if a.end is None else min(a.end,duration)
    if not 0<=a.start<end:raise SystemExit('时间范围无效')
    has_audio='Audio:' in probe.stderr
    h=hashlib.sha256()
    with source.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    meta={'token':a.token,'block_id':a.block_id,'path':str(source),'filename':source.name,
          'version':a.version,'sha256':h.hexdigest(),'duration_seconds':duration,'has_audio':has_audio,
          'review_method':'prepared_frames_only_not_yet_viewed','continuous_viewed':False,
          'audio_listened':False,'frames':[]}
    for i in range(a.frames):
        t=a.start+(max(a.start,end-0.1)-a.start)*i/(a.frames-1)
        target=out/f'frame-{i:03d}.png'
        subprocess.run([ff,'-v','error','-ss',str(t),'-i',str(source),'-frames:v','1','-y',str(target)],check=True,capture_output=True,timeout=120)
        if not target.is_file() or not target.stat().st_size:raise SystemExit('未导出所需帧，不登记完成')
        meta['frames'].append({'requested_time_seconds':round(t,3),'path':str(target)})
    if a.audio and has_audio:
        target=out/'audio.wav'
        subprocess.run([ff,'-v','error','-i',str(source),'-vn','-acodec','pcm_s16le','-y',str(target)],check=True,capture_output=True,timeout=180)
        meta['audio_path']=str(target)
    (out/'media.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2))
    print(json.dumps({'sha256':meta['sha256'],'duration_seconds':duration,'frames':len(meta['frames']),
                      'has_audio':has_audio,'reviewed':False,'manifest':str(out/'media.json')}))


if __name__=='__main__':main()
