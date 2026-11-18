#!/usr/bin/env python3
"""소개 영상 v5(약 5분) — scenes.html 을 장면 PNG 로, motion.html 을 motion.mp4 로 찍고, 자막 SRT 와 CapCut 초안을 만든다.

    python3 -m venv ~/.capvenv && ~/.capvenv/bin/pip install pycapcut==0.0.4 playwright imageio-ffmpeg
    ~/.capvenv/bin/python design/intro-video/build.py            # 찍기 + 초안
    ~/.capvenv/bin/python design/intro-video/build.py --no-draft # 찍기 + SRT 만

모션그래픽은 설치된 Chrome 으로 프레임을 하나씩 찍어(Playwright) ffmpeg(imageio-ffmpeg 동봉)로 묶는다.

장면 순서·길이·자막은 아래 SCENES 한 곳에서 바꾼다. 왼쪽 위 표기(PART N)는 이 순서대로 자동으로 붙는다.
다시 돌리면 같은 이름의 초안을 덮어쓴다(CapCut 에서 손으로 고친 내용도 사라진다 — 손볼 거면 CapCut 안에서 복제해 두고 고친다).
이전 버전은 그대로 둔다 — v1 은 out/v1/, v2 는 out/ 바로 아래, v3·v4 는 out/v3/·out/v4/ 파일을 CapCut 이 가리킨다.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "v5"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DRAFTS = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"
NAME = "MeshSense_소개_v5"
FPS = 30

# (scenes.html?s= 장면, 길이 초, 자막) — 순서대로 이어 붙인다. "ch1".."ch7" 은 파트 표지.
# 32·33 은 여는 장면(같은 방, 빈 방 → 움직임), 1 은 제목, 34 는 맺는 장면.
# "m" 은 motion.mp4 의 다음 구간이다 — 길이는 motion.html 의 구간 길이로 그대로 넘어간다.
# 자막은 한 줄 24자 안팎, 두 줄까지. 길면 "||" 로 나눈다 — 장면 길이를 똑같이 나눠 차례로 띄운다.
SCENES = [
    (32, 2.5, ""),
    (33, 3.5, "사람이 들어오자 불이 켜집니다\n이 방에 카메라는 없습니다"),
    (1, 3.0, ""),
    (12, 7.0, "방에 사람이 있는지 알고 싶지만\n카메라는 영상 유출이 걱정됩니다||MeshSense는 Wi-Fi 신호만으로\n방의 상태를 알아냅니다"),
    (13, 6.0, "알아내는 것은 세 가지\n빈 방 · 정지 · 움직임||사람 수와 위치는\n알아내지 않습니다"),
    (14, 5.5, "신호의 원리부터 서비스까지\n일곱 파트로 따라갑니다"),

    ("ch1", 2.5, ""),
    (2, 5.0, "Wi-Fi 전파는 벽과 사람에 부딪히며\n여러 길로 수신기에 닿습니다"),
    (3, 7.0, "수신 칩은 주파수 52개마다\n얼마나 세게, 얼마나 늦게 왔는지 잽니다||이 측정값이 CSI\n(채널 상태 정보)입니다"),
    (11, 8.0, "CSI에는 세기(진폭)와\n밀린 정도(위상)가 담깁니다||위상은 보드끼리 시계가 달라 매번 틀어집니다\n실시간 모델은 진폭을 씁니다"),
    (4, 5.0, "사람이 움직이면 전파의 길이 바뀌어\nCSI가 크게 흔들립니다"),
    (15, 8.0, "가만히 있는 사람은\n빈 방과 거의 똑같아 보입니다||그래서 정지를 가리려면\n더 오래 지켜봐야 합니다"),

    ("ch2", 2.5, ""),
    (16, 8.0, "보드는 모두 같은 ESP32-S3 다섯 대||송신 1대, 수신 3대,\n모아 주는 싱크 1대로 나눕니다"),
    (5, 5.5, "송신 보드가 1초에 100번 신호를 쏘고\n수신 보드 3대가 매번 CSI를 잽니다"),
    (17, 9.0, "수신 보드는 받을 때마다\n172바이트 프레임을 만듭니다||헤더 44바이트에 버전·번호표·검사값,\n나머지 128바이트가 CSI입니다"),
    (18, 10.0, "학습용 데이터도 실시간과 똑같이\n무선으로 싱크를 거쳐 모읍니다||선이 없어야 보드를 어디든 두고\n집에서도 쉽게 따라 할 수 있습니다"),
    (6, 5.0, "Mac은 프레임을 검사한 뒤\n원본 그대로 저장합니다"),
    (19, 8.0, "보드마다 시계가 달라서\n송신 보드의 번호표(tx_seq)로||세 대의 데이터를\n한 줄로 맞춥니다"),

    ("ch3", 2.5, ""),
    (20, 9.0, "학습 데이터는 한 상태를\n5분씩 찍은 세션입니다||세 상태를 한 묶음으로\n순서를 섞어 찍습니다"),

    ("ch4", 2.5, ""),
    ("m", 4.5, "수신 보드가 뽑은 CSI가\n싱크를 거쳐 Mac 수집기로 갑니다"),
    ("m", 5.0, "세 대의 CSI가 시간 순서로 쌓이고\n15초가 차면 창이 완성됩니다"),
    ("m", 7.0, "창을 51줄로 나누고\n10프레임씩 평균 내 150칸으로||창 평균으로 나눠\n변화만 남깁니다"),
    ("m", 7.5, "같은 CNN 하나가\n51줄을 똑같이 훑습니다||합성곱과 풀링을 거듭하며\n움직임의 무늬를 찾습니다"),
    ("m", 5.0, "줄마다 숫자 32개로 줄이고\n51줄을 모아 96개로 요약합니다"),
    ("m", 6.0, "마지막으로 DNN이 96개를 보고\n빈 방 · 정지 · 움직임을 고릅니다"),

    ("ch5", 2.5, ""),
    (7, 6.0, "0.3초마다 두 모델이 함께 봅니다\n최근 3초와 최근 15초"),
    (23, 8.0, "15초 모델은 창 전체를 요약해서\n움직임을 늦게 알아챕니다||그래서 3초 모델이\n움직임을 먼저 잡습니다"),
    (8, 7.0, "움직임은 3초 모델이 먼저 잡고||아니면 15초 모델이\n빈 방과 정지를 가립니다"),
    (24, 10.5, "상태 기계는 같은 판단이\n충분히 이어질 때만 상태를 바꿉니다||확인하는 동안 안에서는 판단 중이지만\n화면은 이전 상태를 유지합니다"),
    (22, 8.0, "학습에 쓰지 않은 날로 시험했습니다||15초 모델은 창의 94.8%,\n3초 모델은 96.6%를 맞혔습니다"),
    (26, 7.0, "수신 보드 하나라도 3초간 끊기면\n판단을 멈춥니다||모르는 채로\n아는 척하지 않기 위해서입니다"),

    ("ch6", 2.5, ""),
    (27, 9.0, "이 모델로 만든 집 상태 서비스는||조명은 집 안 Mac이 바로 켜고\n알림은 클라우드가 판단합니다"),
    (28, 10.5, "사람이 있으면 불을 켜고\n나가면 끕니다||자는 시간엔 뒤척임에 켜지지 않고\n가만히 10분이면 꺼집니다||규칙과 시간은 사용자가 정합니다\n화면의 값은 예시입니다"),
    (29, 10.0, "오래 움직임이 없으면 본인에게\n답이 없으면 지인에게 문자를 보냅니다||외출 중 움직임과\n집 기기 끊김도 알려 줍니다"),
    (10, 6.5, "같은 판단 위에 돌봄 · 보안 · 절약 같은\n서비스를 더 만들 수 있습니다"),

    ("ch7", 2.5, ""),
    (30, 10.5, "오래 빌려 시험할 수 있던 공간은\n자취방과 학교 활동실 두 곳입니다||연속 운영은 1~2시간까지 해 봤고\n하루 넘게는 아직입니다||누운 자세는 시연에서 인식했지만\n여러 정지 자세는 아직 학습 전입니다"),
    (31, 7.5, "다음은 더 많은 방과 사람,\n배치에서 데이터를 모으는 것입니다||조건이 다양할수록\n모델이 단단해집니다"),
    (34, 5.0, ""),
]


def labels() -> dict:
    """왼쪽 위 표기 — 파트 표지 뒤의 장면은 'PART N', 그 앞(도입)은 비운다. 모션그래픽은 통틀어 하나."""
    out, part = {}, ""
    for s, _, _ in SCENES:
        if str(s).startswith("ch"):
            part = f"PART {s[2:]}"
        else:
            out.setdefault(s, part)
    return out


def captions(dur: float, text: str) -> list:
    """(시작, 길이, 문장) — '||' 로 나눈 자막은 장면 길이를 똑같이 나눠 갖는다."""
    parts = [x for x in text.split("||") if x]
    return [(dur * i / len(parts), dur / len(parts), x) for i, x in enumerate(parts)]


def render():
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("scene_*.png"):              # 장면 수가 줄었을 때 남는 그림을 치운다
        old.unlink()
    no = labels()
    with tempfile.TemporaryDirectory() as prof:     # 사용자의 Chrome 프로필과 섞지 않는다
        for s in dict.fromkeys(s for s, _, _ in SCENES if s != "m"):
            subprocess.run([CHROME, "--headless=new", "--hide-scrollbars", "--force-device-scale-factor=1",
                            f"--user-data-dir={prof}", "--window-size=1920,1080", "--virtual-time-budget=3000",
                            f"--screenshot={OUT / f'scene_{s}.png'}",
                            f"{(HERE / 'scenes.html').as_uri()}?s={s}&no={quote(no.get(s, ''))}"],
                           check=True, capture_output=True)


def render_motion():
    import imageio_ffmpeg
    from playwright.sync_api import sync_playwright

    beats = [d for s, d, _ in SCENES if s == "m"]
    url = f"{(HERE / 'motion.html').as_uri()}?beats={','.join(map(str, beats))}&no={quote(labels()['m'])}"
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(url)
        page.evaluate("ready")                       # 글꼴·사진이 다 올라온 뒤에 찍는다
        for i in range(round(sum(beats) * FPS)):
            page.evaluate(f"render({i / FPS})")
            page.screenshot(path=f"{tmp}/{i:04d}.png")
        browser.close()
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-framerate", str(FPS),
                        "-i", f"{tmp}/%04d.png", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16",
                        str(OUT / "motion.mp4")], check=True)


def write_srt():
    def ts(s):
        return f"{int(s // 3600):02}:{int(s % 3600 // 60):02}:{int(s % 60):02},{round(s % 1 * 1000):03}"
    rows, t = [], 0.0
    for _, dur, text in SCENES:
        for at, d, line in captions(dur, text):
            rows.append(f"{len(rows) + 1}\n{ts(t + at)} --> {ts(t + at + d)}\n{line}\n")
        t += dur
    (OUT / "captions.srt").write_text("\n".join(rows), encoding="utf-8")


def build_draft():
    import pycapcut as cc

    script = cc.DraftFolder(str(DRAFTS)).create_draft(NAME, 1920, 1080, fps=30, allow_replace=True)
    script.add_track(cc.TrackType.video).add_track(cc.TrackType.text)
    motion, m_at = cc.VideoMaterial(str(OUT / "motion.mp4")), 0.0
    t = 0.0
    for i, (scene, dur, text) in enumerate(SCENES, 1):
        span = cc.trange(f"{t}s", f"{dur}s")
        if scene == "m":                             # 모션그래픽은 한 파일을 구간별로 이어 붙인다
            seg = cc.VideoSegment(motion, span, source_timerange=cc.trange(f"{m_at}s", f"{dur}s"))
            m_at += dur
        else:
            seg = cc.VideoSegment(cc.VideoMaterial(str(OUT / f"scene_{scene}.png")), span)
        if i == 1:
            seg.add_animation(cc.IntroType.渐显, "0.6s")
        if i == len(SCENES):
            seg.add_animation(cc.OutroType.渐隐, "0.6s")
        elif not (scene == "m" and SCENES[i][0] == "m"):   # 모션그래픽 안에서는 끊지 않는다
            seg.add_transition(cc.TransitionType.叠化, duration="0.4s")
        script.add_segment(seg)
        for at, d, line in captions(dur, text):
            cap = cc.TextSegment(line, cc.trange(f"{t + at}s", f"{d}s"),
                                 style=cc.TextStyle(size=5.0, color=(1.0, 1.0, 1.0), align=1, auto_wrapping=True),
                                 clip_settings=cc.ClipSettings(transform_y=-0.78),
                                 background=cc.TextBackground(color="#000000", alpha=0.55, round_radius=0.4))
            cap.add_animation(cc.TextIntro.渐显, "0.3s")
            script.add_segment(cap)
        t += dur
    script.save()

    # pycapcut 은 Windows 판 기준이라 메타가 비어 있다 — Mac 판이 목록에 띄울 정보를 채우고,
    # Mac 판이 읽는 draft_info.json 도 같은 내용으로 둔다.
    folder = DRAFTS / NAME
    meta_path = folder / "draft_meta_info.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    now_us = int(time.time() * 1e6)
    meta.update(draft_fold_path=str(folder), draft_name=NAME, draft_root_path=str(DRAFTS),
                draft_id=str(uuid.uuid4()).upper(), tm_duration=int(t * 1e6),
                tm_draft_create=now_us, tm_draft_modified=now_us)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    shutil.copy(folder / "draft_content.json", folder / "draft_info.json")
    return folder


if __name__ == "__main__":
    render()
    render_motion()
    write_srt()
    print("장면:", OUT, f"· {sum(d for _, d, _ in SCENES):.1f}초")
    if "--no-draft" not in sys.argv:
        print("CapCut 초안:", build_draft())
